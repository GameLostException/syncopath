"""Core sync engine for SyncoPath — handles bidirectional sync logic."""
import hashlib
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from .config import AccountConfig, CONFIG_DIR
from .drive_api import (DriveAPI, GOOGLE_NATIVE_MIMES, GOOGLE_EXPORT_MAP, GOOGLE_SKIP_MIMES,
                        http_status, is_auth_error, is_not_found, is_skip_mime, is_transient,
                        sanitize_filename)
from .state import StateDB, FileState
from .local_watcher import LocalChangeEvent
from .remote_watcher import RemoteChange
from .sync_worker import SyncBatchError
from .trash import move_to_trash

log = logging.getLogger(__name__)

# Deletion states (docs/specs/deletion.md)
PENDING_DELETE = "pending_delete"      # deleted here in safe mode, waiting to be sent to Drive
DELETED_ON_DRIVE = "deleted_on_drive"  # in Drive's bin, kept here until the user deletes it
SHARED_WITH_ME = "Shared with me"      # local folder of files owned by others


class NotEnoughDiskSpace(Exception):
    """The initial sync stopped for lack of space; it resumes on the next attempt."""


class PendingDeletion(NamedTuple):
    path: str
    is_folder: bool
    owned: bool  # False: deleting it only stops syncing it; Drive is not changed


def _top_level(paths: list[str]) -> list[str]:
    """Drop every path that sits inside another path of the list."""
    chosen = set(paths)
    return sorted(p for p in paths
                  if not any(p.startswith(other + "/") for other in chosen if other != p))


def md5_file(path: Path) -> Optional[str]:
    """Compute MD5 hash of a local file."""
    if not path.exists() or path.is_dir():
        return None
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_to_timestamp(iso_str: str) -> float:
    """Convert ISO 8601 timestamp to epoch seconds."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.timestamp()


class SyncEngine:
    """Bidirectional sync engine for one account."""

    # Where local deletions go; replaced in tests so the real trash is never touched
    _trash_local = staticmethod(move_to_trash)

    def __init__(self, config: AccountConfig, drive: DriveAPI, state_db: StateDB, safe_mode: bool = True,
                 min_free_space_gb: int = 5):
        self.config = config
        self.drive = drive
        self.state_db = state_db
        self.local_root = config.local_path
        self._safe_mode = safe_mode
        self._min_free_space_bytes = min_free_space_gb * 1024 * 1024 * 1024
        self._disk_full_paused = False
        # Callback to suppress local watcher during downloads
        self._suppress_fn = None
        self._unsuppress_fn = None
        # Status callback for tray icon
        self._status_callback = None
        # Progress callback for tray icon
        self._progress_callback = None
        # Thunar overlay integration
        self._overlay = None
        # Called when the lists of pending deletions may have changed
        self._deletions_callback = None

    def set_watcher_suppress(self, suppress_fn, unsuppress_fn):
        """Set functions to suppress/unsuppress local watcher events."""
        self._suppress_fn = suppress_fn
        self._unsuppress_fn = unsuppress_fn

    def _unsuppress_later(self, *rel_paths: str):
        """Re-enable local events for paths we just wrote, after the watcher's debounce."""
        if self._unsuppress_fn:
            delay = self.config.debounce + 1
            for rel_path in rel_paths:
                threading.Timer(delay, self._unsuppress_fn, args=[rel_path]).start()

    def _root_ids(self) -> set:
        """IDs that mean 'the top of this sync root'."""
        ids = {self.config.remote_id, self.drive.get_real_root_id(), "root",
               self.drive.shared_drive_id}
        ids.discard(None)
        return ids

    def set_status_callback(self, callback):
        """Set callback for sync status updates: callback(status, detail)."""
        self._status_callback = callback

    def set_progress_callback(self, callback):
        """Set callback for progress updates: callback(current, total, filename)."""
        self._progress_callback = callback

    def set_deletions_callback(self, callback):
        """callback() is called whenever pending deletions may have changed."""
        self._deletions_callback = callback

    def _notify_deletions(self):
        if self._deletions_callback:
            self._deletions_callback()

    def set_overlay(self, overlay):
        """Set Thunar overlay integration."""
        self._overlay = overlay

    def _exceeds_max_size(self, size_bytes: int, rel_path: str = "") -> bool:
        """Check if a file exceeds the max file size limit.

        Returns True if the file should be excluded from sync.
        When excluded, marks it with 'size_excluded' status in state DB.
        """
        if self.config.max_file_size_mb <= 0:
            return False  # No limit configured
        max_bytes = self.config.max_file_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            if rel_path:
                log.info("Size excluded (%d MB > %d MB limit): %s",
                         size_bytes // (1024 * 1024),
                         self.config.max_file_size_mb, rel_path)
                if self._overlay:
                    self._overlay.set_file_warning(rel_path)
            return True
        return False

    def _check_disk_space(self) -> bool:
        """Check if there's enough free disk space. Returns True if OK."""
        try:
            usage = shutil.disk_usage(str(self.local_root))
            if usage.free < self._min_free_space_bytes:
                free_gb = usage.free / (1024 * 1024 * 1024)
                min_gb = self._min_free_space_bytes / (1024 * 1024 * 1024)
                if not self._disk_full_paused:
                    self._disk_full_paused = True
                    log.error(
                        "DISK SPACE LOW: %.1f GB free, minimum %.0f GB required. Sync paused!",
                        free_gb, min_gb
                    )
                    self._notify_status("error",
                        f"Disk space low: {free_gb:.0f} GB free (min {min_gb:.0f} GB)")
                return False
            # Recovered
            if self._disk_full_paused:
                self._disk_full_paused = False
                log.info("Disk space recovered, resuming sync")
            return True
        except Exception:
            return True  # If we can't check, don't block

    def _notify_status(self, status: str, detail: str = ""):
        if self._status_callback:
            self._status_callback(status, detail)
        if self._overlay:
            self._overlay.set_account_status(status, detail)

    def initial_sync(self):
        """Perform full initial sync — downloads everything from remote."""
        from .config import validate_sync_dir_empty

        # A brand-new sync root must be empty, so we never mix unrelated files in.
        # An initial sync that was interrupted is resumed instead.
        if self.state_db.get_meta("initial_sync") is None:
            validate_sync_dir_empty(self.local_root)
        self.state_db.set_meta("initial_sync", "started")

        # Take the change cursor BEFORE listing: edits made while we download
        # are then replayed by the Changes API instead of being skipped.
        start_token = self.drive.get_start_page_token()

        self._notify_status("syncing", "Initial sync...")
        log.info("Starting initial sync for %s", self.config.name)

        # Disable per-file overlays during bulk initial sync (too slow for 6000+ files)
        overlay_backup = self._overlay
        self._overlay = None

        files = self.drive.list_folder_recursive(
            self.config.remote_id,
            include_shared_with_me=self.config.include_shared_with_me)
        log.info("Found %d remote files", len(files))

        # Also index all folders into state DB for path resolution
        self._index_remote_folders()

        total_files = len(files)
        synced_count = 0

        for f in files:
            rel_path = f["_path"]
            mime_type = f["mimeType"]

            # Skip non-exportable types (Google native + third-party SDK)
            if is_skip_mime(mime_type):
                synced_count += 1
                continue

            # Skip files exceeding size limit
            file_size = int(f.get("size", 0) or 0)
            if self._exceeds_max_size(file_size, rel_path):
                synced_count += 1
                continue

            # Handle Google Docs export naming
            if mime_type in GOOGLE_NATIVE_MIMES:
                _, ext = GOOGLE_EXPORT_MAP[mime_type]
                if not rel_path.endswith(ext):
                    rel_path += ext

            local_path = self.local_root / rel_path
            existing_state = self.state_db.get_by_file_id(f["id"])

            # Skip if already synced and unchanged
            if existing_state and local_path.exists():
                local_hash = md5_file(local_path)
                if local_hash == existing_state.local_md5:
                    synced_count += 1
                    continue

            # Report progress
            synced_count += 1
            if self._progress_callback:
                file_size = int(f.get("size", 0) or 0)
                self._progress_callback(synced_count, total_files, rel_path, file_size)

            if not self._check_disk_space():
                # Not complete: no page token, so the next attempt resumes here
                self._overlay = overlay_backup
                raise NotEnoughDiskSpace(
                    f"Not enough free disk space; initial sync stopped at {rel_path}")

            # Download
            try:
                self._download_file(f, rel_path)
            except Exception as e:
                # _download_file handles warning/error classification internally
                # and no longer re-raises for unsyncable files
                log.warning("Skipping file %s: %s", rel_path, e)
                continue

        self.state_db.set_page_token(start_token)

        # Restore overlay and bulk-mark all files as synced (in background)
        self._overlay = overlay_backup
        if self._overlay:
            threading.Thread(
                target=self._overlay.mark_all_synced, daemon=True
            ).start()

        self._notify_status("idle", "Initial sync complete")
        log.info("Initial sync complete for %s", self.config.name)

    def reconcile(self):
        """Reconcile local state with remote — fix discrepancies.

        This is called by "Sync Now" when there are no pending changes.
        It handles:
        - Files marked unknown/error → verify and mark synced or re-download
        - Files marked synced but missing locally → re-download
        - Path mismatches (renames not propagated) → detected via fresh path resolution
        """
        self._notify_status("syncing", "Reconciling...")

        try:
            if self._sync_root_looks_unmounted():
                return
            self._reconcile_deletion_marks()
            all_states = self.state_db.get_all()

            # Phase 1: Fix unknown/error files
            stale = [s for s in all_states
                     if s.sync_status in ("unknown", "error")
                     and not s.is_folder]

            # Phase 2: Find synced files missing locally
            missing = [s for s in all_states
                       if s.sync_status == "synced"
                       and not s.is_folder
                       and not (self.local_root / s.path).exists()]

            total = stale + missing
            if not total:
                log.info("Reconcile: all files up to date")
                return

            log.info("Reconcile: %d stale + %d missing = %d files to process",
                     len(stale), len(missing), len(total))

            for i, state in enumerate(total):
                local_path = self.local_root / state.path

                if self._progress_callback:
                    self._progress_callback(i + 1, len(total), state.path, 0)

                if local_path.exists():
                    local_hash = md5_file(local_path)

                    # Error row with a local edit that never reached Drive: upload it.
                    # A failed upload keeps the hash of the last synced content, or has
                    # no file_id when the file was new. A failed download (including rows
                    # from older versions, which stored local_md5=NULL) is neither.
                    pending_upload = state.local_md5 is not None or not state.file_id
                    if (state.sync_status == "error" and pending_upload
                            and local_hash != state.local_md5):
                        try:
                            self._upload_local_file(state.path)
                        except Exception as e:
                            log.warning("Reconcile: re-upload failed %s: %s", state.path, e)
                        continue

                    # For error files: verify hash matches remote before marking synced
                    if state.sync_status == "error" and state.remote_md5:
                        if local_hash != state.remote_md5:
                            # Local content doesn't match remote — re-download
                            log.info("Reconcile: hash mismatch for %s, re-downloading",
                                     state.path)
                            try:
                                meta = self.drive.get_file_metadata(state.file_id)
                                if meta and not meta.get("trashed"):
                                    if not is_skip_mime(meta["mimeType"]):
                                        self._download_file(meta, state.path)
                                        continue
                            except Exception as e:
                                log.warning("Reconcile: re-download failed %s: %s",
                                            state.path, e)
                                continue

                    # File exists and hash OK (or unknown status) — mark synced
                    state.local_md5 = local_hash
                    state.local_mtime = local_path.stat().st_mtime
                    state.sync_status = "synced"
                    self.state_db.upsert(state)

                    if self._overlay:
                        self._overlay.set_file_synced(state.path)
                else:
                    # File missing locally — re-download from remote
                    try:
                        meta = self.drive.get_file_metadata(state.file_id)
                        if meta and not meta.get("trashed"):
                            # Check if file was renamed — resolve fresh path
                            fresh_path = self._resolve_remote_path(meta)
                            mime_type = meta["mimeType"]
                            if mime_type in GOOGLE_NATIVE_MIMES:
                                _, ext = GOOGLE_EXPORT_MAP[mime_type]
                                if fresh_path and not fresh_path.endswith(ext):
                                    fresh_path += ext

                            if fresh_path and fresh_path != state.path:
                                # Path changed — update state and check new location
                                self.state_db.delete_by_path(state.path)
                                state.path = fresh_path
                                local_path = self.local_root / fresh_path

                                if local_path.exists():
                                    # File exists at new path — just update state
                                    local_hash = md5_file(local_path)
                                    state.local_md5 = local_hash
                                    state.local_mtime = local_path.stat().st_mtime
                                    state.sync_status = "synced"
                                    self.state_db.upsert(state)
                                    log.info("Reconcile: re-linked %s", fresh_path)
                                    continue

                            if is_skip_mime(meta["mimeType"]):
                                # Can't download this type — remove from tracking
                                self.state_db.delete_by_path(state.path)
                                continue

                            if state.sync_status == "synced":
                                # Synced before, gone now, no event seen: it was deleted
                                # while we were not watching (spec AC-15)
                                self._handle_local_deletion(state.path)
                                continue

                            self._download_file(meta, state.path)
                        else:
                            # File trashed on remote — remove from state
                            self.state_db.delete_by_path(state.path)
                    except Exception as e:
                        if is_not_found(e):
                            # Access revoked or file permanently deleted
                            log.info("Reconcile: access lost for %s, removing",
                                     state.path)
                            self.state_db.delete_by_path(state.path)
                        else:
                            log.warning("Reconcile: failed to recover %s: %s",
                                        state.path, e)

            log.info("Reconcile complete: %d files processed", len(total))
        except Exception as e:
            log.error("Reconcile failed: %s", e)
        finally:
            self._notify_status("idle", "Reconcile complete")

    def _sync_root_looks_unmounted(self) -> bool:
        """A missing or empty sync folder while files are tracked: never treat that
        as 'the user deleted everything' (spec AC-14)."""
        tracked = any(not s.is_folder and s.sync_status != PENDING_DELETE
                      for s in self.state_db.get_all())
        if not tracked:
            return False
        if self.local_root.is_dir() and any(self.local_root.iterdir()):
            return False
        log.error("Sync folder %s is missing or empty — is the disk mounted? "
                  "No deletion will be propagated.", self.local_root)
        self._notify_status("error", f"Sync folder missing or empty: {self.local_root}")
        return True

    def _reconcile_deletion_marks(self):
        """Clear marks the user undid without us seeing it (spec AC-11)."""
        for state in self.state_db.get_by_status(PENDING_DELETE):
            if not state.is_folder and (self.local_root / state.path).exists():
                self._restore_local(state.path)
        for state in self.state_db.get_by_status(DELETED_ON_DRIVE):
            try:
                meta = self.drive.get_file_metadata(state.file_id)
            except Exception as e:
                log.debug("Reconcile: cannot check %s: %s", state.path, e)
                continue
            if not meta.get("trashed"):
                log.info("Restored on Drive: %s", state.path)
                self.state_db.set_sync_status(state.path, "synced")
                if self._overlay and not state.is_folder:
                    self._overlay.set_file_synced(state.path)
        self._notify_deletions()

    def _get_remote_paths_cached(self) -> dict:
        """Get remote file paths, using cache if fresh (< 5 minutes).

        Returns dict of {path: file_metadata}.
        """
        import json

        cache_file = CONFIG_DIR / f"remote_cache_{self.config.name}.json"
        cache_ttl = 300  # 5 minutes

        # Try loading cache
        if cache_file.exists():
            cache_age = time.time() - cache_file.stat().st_mtime
            if cache_age < cache_ttl:
                try:
                    cached = json.loads(cache_file.read_text())
                    log.debug("Using cached remote file list (%ds old)", int(cache_age))
                    return cached
                except (json.JSONDecodeError, KeyError):
                    pass  # Corrupt cache, re-fetch

        # Fetch fresh from API
        log.info("Fetching remote file list from Drive API...")
        remote_files = self.drive.list_folder_recursive(
            self.config.remote_id, include_shared_with_me=False)
        remote_paths = {}
        for f in remote_files:
            path = f["_path"]
            mime = f["mimeType"]
            if is_skip_mime(mime):
                continue
            if mime in GOOGLE_NATIVE_MIMES:
                _, ext = GOOGLE_EXPORT_MAP[mime]
                if not path.endswith(ext):
                    path += ext
            remote_paths[path] = {
                "id": f["id"],
                "mimeType": f["mimeType"],
                "md5Checksum": f.get("md5Checksum"),
                "modifiedTime": f.get("modifiedTime", ""),
                "size": f.get("size", 0),
            }

        # Save cache
        try:
            cache_file.write_text(json.dumps(remote_paths))
        except Exception:
            pass  # Non-critical

        return remote_paths

    def scan_local_untracked(self):
        """Scan local directory for files not tracked in state DB.

        Only uploads files that are confirmed to NOT exist on remote.
        Files that exist on remote but aren't tracked get their state DB
        entry created without re-uploading. Shared files are never uploaded.
        """
        self._notify_status("working", "Scanning for new local files...")
        log.info("Scanning for untracked local files...")

        # Build set of remote paths for quick lookup (owned files only)
        # Use cached list if available and fresh (< 5 minutes old)
        remote_paths = self._get_remote_paths_cached()

        # Scan local files
        to_upload = []
        to_track = []

        for local_path in self.local_root.rglob("*"):
            if local_path.is_dir():
                continue
            rel_path = str(local_path.relative_to(self.local_root))

            # Skip excluded patterns
            from fnmatch import fnmatch
            name = local_path.name
            skip = False
            for pattern in self.config.exclude:
                if fnmatch(name, pattern) or fnmatch(rel_path, pattern):
                    skip = True
                    break
            if skip or name.startswith(".") or name.endswith("~"):
                continue

            # Skip conflict copies
            if ".conflict" in name:
                continue

            # Skip "Shared with me" folder
            if rel_path.startswith("Shared with me/"):
                continue

            # Already tracked?
            state = self.state_db.get_by_path(rel_path)
            if state:
                continue

            # Check if file exists on remote (already synced, just not tracked)
            if rel_path in remote_paths:
                to_track.append((rel_path, remote_paths[rel_path]))
            else:
                to_upload.append(rel_path)

        # Track files that already exist on remote (no upload needed)
        for rel_path, remote_meta in to_track:
            local_path = self.local_root / rel_path
            self.state_db.upsert(FileState(
                path=rel_path,
                file_id=remote_meta["id"],
                remote_md5=remote_meta.get("md5Checksum"),
                local_md5=md5_file(local_path),
                remote_mtime=remote_meta.get("modifiedTime", ""),
                local_mtime=local_path.stat().st_mtime,
                mime_type=remote_meta["mimeType"],
                sync_status="synced",
            ))

        if to_track:
            log.info("Re-linked %d files already on remote", len(to_track))

        if not to_upload:
            log.info("No new local files to upload")
            return

        log.info("Uploading %d new local files", len(to_upload))
        self._notify_status("syncing", f"Uploading {len(to_upload)} new file(s)...")

        for i, rel_path in enumerate(to_upload):
            try:
                if self._progress_callback:
                    fpath = self.local_root / rel_path
                    file_size = fpath.stat().st_size if fpath.exists() else 0
                    self._progress_callback(i + 1, len(to_upload), rel_path, file_size)
                self._upload_local_file(rel_path)
            except Exception as e:
                log.warning("Failed to upload untracked file %s: %s", rel_path, e)

        self._notify_status("idle", f"Uploaded {len(to_upload)} new files")
        log.info("Untracked scan complete: %d uploaded, %d re-linked",
                 len(to_upload), len(to_track))

    def scan_remote_undownloaded(self):
        """Scan remote for files that exist on Drive but not locally.

        This catches files created while the service was stopped — they won't
        appear in the Changes API since they were created before the current
        page token. Downloads any missing files.
        """
        self._notify_status("working", "Checking for missing remote files...")
        log.info("Scanning for remote files not downloaded locally...")

        # Get fresh remote file list
        remote_paths = self._get_remote_paths_cached()

        to_download = []
        for rel_path, meta in remote_paths.items():
            local_path = self.local_root / rel_path

            # Skip if already exists locally
            if local_path.exists():
                continue

            # Skip if already in state DB (might be an error state)
            state = self.state_db.get_by_file_id(meta["id"])
            if state:
                continue

            # Skip files exceeding size limit
            file_size = int(meta.get("size", 0) or 0)
            if self._exceeds_max_size(file_size, rel_path):
                continue

            to_download.append((rel_path, meta))

        if not to_download:
            log.info("No missing remote files to download")
            return

        log.info("Downloading %d missing remote files", len(to_download))
        self._notify_status("syncing", f"Downloading {len(to_download)} missing file(s)...")

        for i, (rel_path, meta) in enumerate(to_download):
            try:
                if not self._check_disk_space():
                    log.warning("Sync paused due to low disk space")
                    break

                if self._progress_callback:
                    file_size = int(meta.get("size", 0) or 0)
                    self._progress_callback(i + 1, len(to_download), rel_path, file_size)

                # Build full metadata for download
                full_meta = self.drive.get_file_metadata(meta["id"])
                self._download_file(full_meta, rel_path)
            except Exception as e:
                log.warning("Failed to download missing file %s: %s", rel_path, e)

        self._notify_status("idle", f"Downloaded {len(to_download)} missing files")
        log.info("Remote scan complete: %d files downloaded", len(to_download))

    def handle_local_changes(self, events: list[LocalChangeEvent]):
        """Process local filesystem changes — upload/delete on remote."""
        self._notify_status("syncing", f"Uploading {len(events)} change(s)...")

        # Mark all changed files as pending
        if self._overlay:
            for event in events:
                if event.event_type in ("created", "modified"):
                    self._overlay.set_file_pending(event.path)

        failed, errors = [], []
        try:
            for i, event in enumerate(events):
                try:
                    if self._progress_callback:
                        file_size = 0
                        fpath = self.local_root / event.path
                        if fpath.exists():
                            file_size = fpath.stat().st_size
                        self._progress_callback(i + 1, len(events), event.path, file_size)
                    self._handle_local_event(event)
                except Exception as e:
                    log.error("Error handling local event %s: %s", event, e)
                    failed.append(event)
                    errors.append(e)
        finally:
            self._notify_status("idle")
        if failed:
            raise SyncBatchError(failed, errors)

    def handle_remote_changes(self, changes: list[RemoteChange]):
        """Process remote Drive changes — download/delete locally."""
        self._notify_status("syncing", f"Downloading {len(changes)} change(s)...")

        # Sort: process folder changes first (shallowest first for correct ordering)
        folder_changes = [c for c in changes
                          if c.file_meta and c.file_meta.get("mimeType") == "application/vnd.google-apps.folder"]
        file_changes = [c for c in changes
                        if not c.file_meta or c.file_meta.get("mimeType") != "application/vnd.google-apps.folder"]

        # Process folders first (FIX-09: ordering by depth)
        all_ordered = folder_changes + file_changes

        # Mark known paths as pending
        if self._overlay:
            for change in all_ordered:
                if change.event_type in ("created", "modified"):
                    state = self.state_db.get_by_file_id(change.file_id)
                    if state:
                        self._overlay.set_file_pending(state.path)

        failed, errors = [], []
        try:
            for i, change in enumerate(all_ordered):
                try:
                    if self._progress_callback:
                        fname = ""
                        file_size = 0
                        if change.file_meta:
                            fname = change.file_meta.get("name", "")
                            file_size = int(change.file_meta.get("size", 0) or 0)
                        self._progress_callback(i + 1, len(all_ordered), fname, file_size)
                    self._handle_remote_event(change)
                except Exception as e:
                    log.error("Error handling remote change %s: %s", change, e)
                    failed.append(change)
                    errors.append(e)
        finally:
            self._notify_status("idle")
        if failed:
            raise SyncBatchError(failed, errors)

    def _handle_local_event(self, event: LocalChangeEvent):
        """Handle a single local filesystem event."""
        if event.event_type == "created" or event.event_type == "modified":
            self._upload_local_file(event.path)
        elif event.event_type == "deleted":
            self._handle_local_deletion(event.path)
        elif event.event_type == "moved":
            self._handle_local_move(event.path, event.dest_path)

    def _handle_remote_event(self, change: RemoteChange):
        """Handle a single remote change event."""
        if change.event_type == "deleted":
            self._delete_local_file(change.file_id)
        elif change.event_type in ("created", "modified"):
            self._download_or_conflict(change)

    def _upload_local_file(self, rel_path: str):
        """Upload a new or modified local file to Drive."""
        local_path = self.local_root / rel_path
        if not local_path.exists() or local_path.is_dir():
            return

        # Skip files exceeding size limit
        if local_path.exists():
            file_size = local_path.stat().st_size
            if self._exceeds_max_size(file_size, rel_path):
                return

        # Don't upload new files into "Shared with me" — those are from others' drives
        # (Updates to existing tracked shared files are OK via file_id)
        state = self.state_db.get_by_path(rel_path)
        if rel_path.startswith("Shared with me/") and not state:
            log.debug("Ignoring new file in 'Shared with me': %s", rel_path)
            return

        local_hash = md5_file(local_path)

        # Back after a local deletion that was never sent to Drive: sync it again
        if state and state.sync_status == PENDING_DELETE:
            self._restore_local(rel_path)
            state.sync_status = "synced"

        # Skip if unchanged from last sync
        if state and local_hash == state.local_md5:
            return

        # Skip if local matches remote (file was just downloaded, no actual change)
        if state and state.remote_md5 and local_hash == state.remote_md5:
            # Update local_md5 to match (was likely out of date)
            state.local_md5 = local_hash
            state.local_mtime = local_path.stat().st_mtime
            self.state_db.upsert(state)
            return

        # Mark file as syncing in Thunar
        if self._overlay:
            self._overlay.set_file_syncing(rel_path)

        # Determine parent folder ID on Drive
        parent_id = self._ensure_remote_dirs(rel_path)

        # Upload (update if exists, create if new)
        file_id = state.file_id if state else None
        try:
            result = self.drive.upload_file(local_path, parent_id, file_id)

            # Update state
            self.state_db.upsert(FileState(
                path=rel_path,
                file_id=result["id"],
                remote_md5=result.get("md5Checksum"),
                local_md5=local_hash,
                remote_mtime=result["modifiedTime"],
                local_mtime=local_path.stat().st_mtime,
                mime_type="application/octet-stream",
                sync_status="synced",
            ))
            log.info("Uploaded: %s", rel_path)

            # Mark file as synced in Thunar
            if self._overlay:
                self._overlay.set_file_synced(rel_path)
        except Exception as e:
            # Classify: unsyncable (warning) vs actual failure (error)
            if self._is_upload_unsyncable(e, local_path):
                log.warning("Unsyncable (upload) %s: %s", rel_path, e)
                self.state_db.upsert(FileState(
                    path=rel_path,
                    file_id=file_id,
                    remote_md5=None,
                    local_md5=local_hash,
                    remote_mtime="",
                    local_mtime=local_path.stat().st_mtime,
                    mime_type="application/octet-stream",
                    sync_status="warning",
                ))
                if self._overlay:
                    self._overlay.set_file_warning(rel_path)
            else:
                # Record the failure so reconcile can retry it: keep the hash of the
                # last synced content, so "local differs from local_md5" stays true.
                self.state_db.upsert(FileState(
                    path=rel_path,
                    file_id=file_id,
                    remote_md5=state.remote_md5 if state else None,
                    local_md5=state.local_md5 if state else None,
                    remote_mtime=state.remote_mtime if state else "",
                    local_mtime=state.local_mtime if state else 0,
                    mime_type=state.mime_type if state else "application/octet-stream",
                    sync_status="error",
                ))
                if self._overlay:
                    self._overlay.set_file_error(rel_path)
                raise

    # ─── Deletions (docs/specs/deletion.md) ─────────────────────────────

    def _initial_sync_done(self) -> bool:
        """Deletions only propagate once the account has been fully synced (AC-10)."""
        return self.state_db.get_page_token() is not None

    @staticmethod
    def _owned(rel_path: str) -> bool:
        return not (rel_path == SHARED_WITH_ME or rel_path.startswith(SHARED_WITH_ME + "/"))

    def _handle_local_deletion(self, rel_path: str):
        """A synced file or folder disappeared locally."""
        if not self._initial_sync_done():
            return
        state = self.state_db.get_by_path(rel_path)
        if not state or state.sync_status == PENDING_DELETE:
            return
        # No "unmounted?" check here: a live delete event is a real deletion (an
        # unmount sends no delete events). Startup/reconcile check it (AC-14).
        if self._safe_mode:
            self.state_db.set_sync_status_tree(rel_path, PENDING_DELETE)
            log.info("Deleted locally, waiting for confirmation: %s", rel_path)
            self._notify_deletions()
        else:
            self._delete_on_drive(state)

    def _forget(self, rel_path: str, reason: str):
        log.info("%s: %s", reason, rel_path)
        self.state_db.delete_tree(rel_path)

    def _delete_on_drive(self, state: FileState):
        """Move a locally deleted item to Drive's bin, unless that would lose something."""
        rel_path = state.path
        if not self._owned(rel_path):
            self._forget(rel_path, "Not yours on Drive, stopped syncing")  # AC-19
            return
        try:
            meta = self.drive.get_file_metadata(state.file_id)
        except Exception as e:
            if is_not_found(e):
                self._forget(rel_path, "Already gone from Drive")
                return
            raise
        if meta.get("trashed"):
            self._forget(rel_path, "Already in Drive's bin")
            return
        if not state.is_folder and self._changed_on_drive(state, meta):
            # AC-16: the Drive version is newer than what was deleted — bring it back
            log.warning("Changed on Drive since last sync, downloading instead of "
                        "deleting: %s", rel_path)
            self.state_db.set_sync_status_tree(rel_path, "synced")
            self._download_file(meta, rel_path)
            return
        try:
            self.drive.delete_file(state.file_id)
        except Exception as e:
            if http_status(e) == 403:  # someone else's file in your folder
                self._forget(rel_path, "Drive refused the bin (not yours), stopped syncing")
                return
            raise
        self._forget(rel_path, "Moved to Drive's bin")

    @staticmethod
    def _changed_on_drive(state: FileState, meta: dict) -> bool:
        if meta.get("md5Checksum") and state.remote_md5:
            return meta["md5Checksum"] != state.remote_md5
        return bool(state.remote_mtime) and meta.get("modifiedTime") != state.remote_mtime

    def _restore_local(self, rel_path: str):
        """The user put back something they had deleted: it is synced again (AC-11)."""
        self.state_db.set_sync_status(rel_path, "synced")
        parts = Path(rel_path).parts
        for depth in range(1, len(parts)):
            parent = self.state_db.get_by_path("/".join(parts[:depth]))
            if parent and parent.sync_status == PENDING_DELETE:
                self.state_db.set_sync_status(parent.path, "synced")
        log.info("Restored locally, syncing again: %s", rel_path)
        self._notify_deletions()

    def pending_local_deletions(self) -> list[PendingDeletion]:
        """Items deleted here in safe mode, not yet sent to Drive (folders listed once)."""
        rows = {s.path: s for s in self.state_db.get_by_status(PENDING_DELETE)}
        return [PendingDeletion(p, rows[p].is_folder, self._owned(p))
                for p in _top_level(list(rows))]

    def deleted_on_drive(self) -> list[str]:
        """Items in Drive's bin that are still here (folders listed once)."""
        return _top_level([s.path for s in self.state_db.get_by_status(DELETED_ON_DRIVE)])

    def flush_local_deletions(self):
        """Send every pending local deletion to Drive's bin (the user confirmed)."""
        failed = []
        for item in self.pending_local_deletions():
            state = self.state_db.get_by_path(item.path)
            if state is None:
                continue
            if (self.local_root / item.path).exists():
                self._restore_local(item.path)
                continue
            try:
                self._delete_on_drive(state)
            except Exception as e:
                log.error("Could not move %s to Drive's bin: %s", item.path, e)
                failed.append(item.path)  # stays listed (spec UC-1 error flow)
        self._notify_deletions()
        if failed:
            self._notify_status("error", f"{len(failed)} deletion(s) not sent to Drive")

    # Drive → local

    def _locally_edited(self, state: FileState) -> bool:
        local_path = self.local_root / state.path
        if state.local_md5 is not None:
            return md5_file(local_path) != state.local_md5
        return state.local_mtime > 0 and local_path.stat().st_mtime > state.local_mtime

    def _split_for_drive_deletion(self, root_path: str):
        """(rows still here, edited files among them) for a subtree deleted on Drive.
        Rows of items no longer here are forgotten."""
        present, edited = [], []
        for row in self.state_db.get_tree(root_path):
            if not (self.local_root / row.path).exists():
                self.state_db.delete_by_path(row.path)
            elif not row.is_folder and self._locally_edited(row):
                edited.append(row)
            else:
                present.append(row)
        return present, edited

    def _keep_edited_as_new(self, edited: list[FileState]):
        """AC-17: an edit made here is not lost because Drive deleted the file."""
        for row in edited:
            log.warning("Deleted on Drive but changed here, uploading as a new file: %s",
                        row.path)
            self.state_db.delete_by_path(row.path)
            self._upload_local_file(row.path)

    def _trash_and_forget(self, rel_path: str) -> bool:
        local_path = self.local_root / rel_path
        if self._suppress_fn:
            self._suppress_fn(rel_path)
        try:
            self._trash_local(local_path)
        except Exception as e:
            # Never fall back to a permanent deletion (spec "Must not")
            log.error("Could not move %s to the trash: %s", rel_path, e)
            self.state_db.set_sync_status(rel_path, "error")
            if self._overlay:
                self._overlay.set_file_error(rel_path)
            return False
        finally:
            self._unsuppress_later(rel_path)
        self.state_db.delete_tree(rel_path)
        log.info("Moved to the trash: %s", rel_path)
        return True

    def _remove_locally(self, root_path: str):
        """Move what Drive deleted to the desktop trash, keeping local edits."""
        present, edited = self._split_for_drive_deletion(root_path)
        if not present and not edited:
            return
        if not edited:
            self._trash_and_forget(root_path)  # a folder goes as one item
            return
        for row in present:
            if row.is_folder:
                self.state_db.delete_by_path(row.path)  # recreated on upload
            else:
                self._trash_and_forget(row.path)
        self._keep_edited_as_new(edited)

    def _delete_local_file(self, file_id: str):
        """A synced file or folder was moved to Drive's bin (or deleted there)."""
        if not self._initial_sync_done():
            return
        state = self.state_db.get_by_file_id(file_id)
        if not state:
            return
        if self._safe_mode:
            present, edited = self._split_for_drive_deletion(state.path)
            for row in present:
                self.state_db.set_sync_status(row.path, DELETED_ON_DRIVE)
                if self._overlay and not row.is_folder:
                    self._overlay.set_file_deleted_on_drive(row.path)
            if present:
                log.info("Deleted on Drive, kept here until you delete it: %s", state.path)
            self._keep_edited_as_new(edited)
        else:
            self._remove_locally(state.path)
        self._notify_deletions()

    def flush_drive_deletions(self):
        """Move every item deleted on Drive to the desktop trash (the user confirmed)."""
        for rel_path in self.deleted_on_drive():
            self._remove_locally(rel_path)
        self._notify_deletions()

    def _handle_local_move(self, old_path: str, new_path: str):
        """Handle a local file rename/move."""
        state = self.state_db.get_by_path(old_path)
        if not state:
            # Treat as new file creation
            self._upload_local_file(new_path)
            return

        old_parent_dir = str(Path(old_path).parent)
        new_parent_dir = str(Path(new_path).parent)
        new_name = Path(new_path).name

        # Build update body
        body = {}
        add_parents = None
        remove_parents = None

        # Always update name if changed
        if Path(old_path).name != new_name:
            body["name"] = new_name

        # If parent directory changed, update parents on Drive
        if old_parent_dir != new_parent_dir:
            # Get old parent ID
            old_parent_state = self.state_db.get_by_path(old_parent_dir) if old_parent_dir != "." else None
            old_parent_id = old_parent_state.file_id if old_parent_state else self.config.remote_id

            # Ensure new parent exists and get its ID
            new_parent_id = self._ensure_remote_dirs(new_path)

            if old_parent_id != new_parent_id:
                add_parents = new_parent_id
                remove_parents = old_parent_id

        self.drive.move_file(state.file_id, body=body or None,
                             add_parents=add_parents, remove_parents=remove_parents)
        self.state_db.rename(old_path, new_path)
        if state.is_folder:
            self.state_db.rename_prefix(old_path, new_path)
        log.info("Moved remote: %s -> %s", old_path, new_path)

    def _download_or_conflict(self, change: RemoteChange):
        """Download a remote file, handling conflicts."""
        meta = change.file_meta
        if not meta:
            return

        # Handle folders (renames/moves/creation)
        if meta["mimeType"] == "application/vnd.google-apps.folder":
            self._handle_remote_folder_change(change)
            return

        # Filter "Shared with me" files if setting is off
        if not self.config.include_shared_with_me:
            # Check if it's an existing tracked file (allow updates to already-synced files)
            state = self.state_db.get_by_file_id(change.file_id)
            if not state:
                # New file — check full ownership chain (not just ownedByMe on file)
                if self._is_shared_file(meta):
                    log.debug("Skipping shared-with-me file: %s", meta.get("name"))
                    return

        # Skip non-exportable types
        if is_skip_mime(meta["mimeType"]):
            return

        # Skip files exceeding size limit
        remote_size = int(meta.get("size", 0) or 0)
        if self._exceeds_max_size(remote_size, meta.get("name", "")):
            return

        # Build local path from Drive path
        state = self.state_db.get_by_file_id(change.file_id)

        if state:
            # Check if file was renamed/moved on remote
            new_rel_path = self._resolve_remote_path(meta)
            mime_type = meta["mimeType"]
            if mime_type in GOOGLE_NATIVE_MIMES:
                _, ext = GOOGLE_EXPORT_MAP[mime_type]
                if new_rel_path and not new_rel_path.endswith(ext):
                    new_rel_path += ext

            if new_rel_path and new_rel_path != state.path:
                # File was renamed/moved on remote — propagate locally
                old_local = self.local_root / state.path
                new_local = self.local_root / new_rel_path

                if old_local.exists():
                    new_local.parent.mkdir(parents=True, exist_ok=True)
                    if self._suppress_fn:
                        self._suppress_fn(state.path)
                        self._suppress_fn(new_rel_path)
                    old_local.rename(new_local)
                    log.info("Renamed file: %s -> %s", state.path, new_rel_path)
                    self._unsuppress_later(state.path, new_rel_path)

                # Update state DB
                self.state_db.delete_by_path(state.path)
                state.path = new_rel_path
                self.state_db.upsert(state)

            rel_path = state.path
        else:
            # New file — need to resolve path from parents
            rel_path = self._resolve_remote_path(meta)
            if not rel_path:
                log.warning("Cannot resolve path for: %s", meta.get("name"))
                return

        # Handle Google Docs export naming
        mime_type = meta["mimeType"]
        if mime_type in GOOGLE_NATIVE_MIMES:
            _, ext = GOOGLE_EXPORT_MAP[mime_type]
            if not rel_path.endswith(ext):
                rel_path += ext

        local_path = self.local_root / rel_path

        # Conflict detection: local file changed since last sync?
        if local_path.exists() and state:
            local_hash = md5_file(local_path)
            local_changed = False

            if state.local_md5 is not None:
                # Normal file: compare hash
                local_changed = (local_hash != state.local_md5)
            else:
                # Google Doc (no md5): compare local mtime
                local_changed = (local_path.stat().st_mtime > state.local_mtime
                                 and state.local_mtime > 0)

            if local_changed:
                # Local was modified — check if remote also changed
                remote_mtime = iso_to_timestamp(meta["modifiedTime"])
                local_mtime = local_path.stat().st_mtime

                if remote_mtime > local_mtime:
                    # Remote wins — save local as conflict
                    self._save_conflict(local_path)
                    log.warning("Conflict (remote wins): %s", rel_path)
                else:
                    # Local wins — push the local version (Drive keeps the
                    # overwritten one in the file's revision history)
                    log.warning("Conflict (local wins): %s", rel_path)
                    self._upload_local_file(rel_path)
                    return

        # Download
        self._download_file(meta, rel_path)

    def _download_file(self, meta: dict, rel_path: str):
        """Download a file from Drive and update state."""
        local_path = self.local_root / rel_path
        mime_type = meta["mimeType"]

        # Mark file as syncing in Thunar
        if self._overlay:
            self._overlay.set_file_syncing(rel_path)

        # Suppress local watcher for this path
        if self._suppress_fn:
            self._suppress_fn(rel_path)

        try:
            self.drive.download_file(meta["id"], mime_type, local_path)

            # Update state
            self.state_db.upsert(FileState(
                path=rel_path,
                file_id=meta["id"],
                remote_md5=meta.get("md5Checksum"),
                local_md5=md5_file(local_path),
                remote_mtime=meta["modifiedTime"],
                local_mtime=local_path.stat().st_mtime,
                mime_type=mime_type,
                sync_status="synced",
            ))
            log.debug("Downloaded: %s", rel_path)

            # Mark file as synced in Thunar
            if self._overlay:
                self._overlay.set_file_synced(rel_path)
        except Exception as e:
            # Classify the error: unsyncable (warning) vs actual failure (error)
            if self._is_unsyncable_error(e):
                log.warning("Unsyncable file %s: %s", rel_path, e)
                self.state_db.upsert(FileState(
                    path=rel_path,
                    file_id=meta["id"],
                    remote_md5=meta.get("md5Checksum"),
                    local_md5=None,
                    remote_mtime=meta["modifiedTime"],
                    local_mtime=0,
                    mime_type=mime_type,
                    sync_status="warning",
                ))
                if self._overlay:
                    self._overlay.set_file_warning(rel_path)
            else:
                log.error("Failed to download %s: %s", rel_path, e)
                # Keep the hash of what is on disk, so reconcile knows the local
                # copy is unchanged and re-downloads rather than uploads it.
                previous = self.state_db.get_by_path(rel_path)
                self.state_db.upsert(FileState(
                    path=rel_path,
                    file_id=meta["id"],
                    remote_md5=meta.get("md5Checksum"),
                    local_md5=previous.local_md5 if previous else None,
                    remote_mtime=meta["modifiedTime"],
                    local_mtime=previous.local_mtime if previous else 0,
                    mime_type=mime_type,
                    sync_status="error",
                ))
                if self._overlay:
                    self._overlay.set_file_error(rel_path)
                if is_transient(e):
                    raise
        finally:
            # Wait out the watcher's debounce so our own write is not seen as an edit
            self._unsuppress_later(rel_path)

    @staticmethod
    def _is_unsyncable_error(error: Exception) -> bool:
        """Determine if an error means the file is permanently unsyncable.

        Returns True for conditions that won't resolve by retrying:
        - Export size limit exceeded (Google Docs too large to export)
        - File not downloadable (unsupported type)
        - Forbidden due to file restrictions
        - File name too long or contains invalid characters
        """
        error_str = str(error).lower()
        unsyncable_indicators = [
            "exportsizelimitexceeded",
            "this file is too large to be exported",
            "filenotdownloadable",
            "cannotdownloadfile",
            "file name too long",
            "invalid argument",
            "is not exportable",
            "only files with binary content can be downloaded",
        ]
        return any(indicator in error_str for indicator in unsyncable_indicators)

    @staticmethod
    def _is_upload_unsyncable(error: Exception, local_path: Path) -> bool:
        """Determine if an upload error means the file is permanently unsyncable.

        Returns True for:
        - File name too long for Drive (> 255 chars)
        - File contains characters forbidden by Drive
        - File is not readable (permission denied on local side)
        - File is a special file (socket, fifo, device)
        """
        if is_transient(error) or is_auth_error(error):
            return False  # retried, and recorded for reconcile
        error_str = str(error).lower()
        unsyncable_indicators = [
            "invalid",
            "file name too long",
            "notfound",  # parent folder gone
        ]
        # Local file problems: unreadable, special file, too long a name, ...
        if isinstance(error, OSError):
            return True
        # Check filename length (Drive limit is 255 chars)
        if len(local_path.name) > 255:
            return True
        return any(indicator in error_str for indicator in unsyncable_indicators)

    def _save_conflict(self, path: Path):
        """Save a conflict copy of a local file."""
        stem = path.stem
        suffix = path.suffix
        conflict_path = path.with_name(f"{stem}.conflict{suffix}")
        counter = 1
        while conflict_path.exists():
            conflict_path = path.with_name(f"{stem}.conflict-{counter}{suffix}")
            counter += 1
        path.rename(conflict_path)
        log.info("Saved conflict: %s", conflict_path.name)

    def _index_remote_folders(self):
        """Index all remote folders into state DB for reliable path resolution.

        This allows _resolve_remote_path to map parent IDs to local paths
        without querying the API for each parent in the chain.
        """
        self._notify_status("working", "Indexing folders...")
        real_root_id = self.drive.get_real_root_id()

        # Get all folders via the thread-safe DriveAPI method
        extra_kwargs = {}
        if self.drive.shared_drive_id:
            extra_kwargs["corpora"] = "drive"
            extra_kwargs["driveId"] = self.drive.shared_drive_id
        all_folders = self.drive.list_folders(extra_kwargs or None)

        # Build folder tree
        id_to_folder = {f["id"]: f for f in all_folders}
        root_id = self.drive.shared_drive_id or real_root_id

        def build_folder_path(folder):
            parts = [folder["name"]]
            current = folder
            while True:
                parents = current.get("parents", [])
                if not parents:
                    return None  # Orphan
                parent_id = parents[0]
                if parent_id == root_id:
                    break
                if parent_id not in id_to_folder:
                    return None  # Parent outside our tree
                current = id_to_folder[parent_id]
                parts.insert(0, current["name"])
            return "/".join(parts)

        indexed = 0
        for folder in all_folders:
            path = build_folder_path(folder)
            if path:
                self.state_db.upsert(FileState(
                    path=path,
                    file_id=folder["id"],
                    remote_md5=None,
                    local_md5=None,
                    remote_mtime="",
                    local_mtime=0,
                    mime_type="application/vnd.google-apps.folder",
                    is_folder=True,
                    sync_status="synced",
                ))
                indexed += 1

        log.info("Indexed %d remote folders into state DB", indexed)

    def _handle_remote_folder_change(self, change):
        """Handle a remote folder rename or move."""
        meta = change.file_meta
        if not meta:
            return

        file_id = change.file_id
        state = self.state_db.get_by_file_id(file_id)

        # Filter shared folders if setting is off
        if not self.config.include_shared_with_me and not state:
            if self._is_shared_file(meta):
                log.debug("Skipping shared folder: %s", meta.get("name"))
                return

        if not state:
            # New folder — index it
            rel_path = self._resolve_remote_path(meta)
            if rel_path:
                local_dir = self.local_root / rel_path
                local_dir.mkdir(parents=True, exist_ok=True)
                self.state_db.upsert(FileState(
                    path=rel_path,
                    file_id=file_id,
                    remote_md5=None,
                    local_md5=None,
                    remote_mtime=meta.get("modifiedTime", ""),
                    local_mtime=0,
                    mime_type="application/vnd.google-apps.folder",
                    is_folder=True,
                    sync_status="synced",
                ))
                log.info("Created local folder: %s", rel_path)
            return

        # Existing folder — check for rename/move
        new_path = self._resolve_remote_path(meta)
        if not new_path:
            return

        old_path = state.path
        if state.sync_status == DELETED_ON_DRIVE:  # restored from Drive's bin
            self.state_db.set_sync_status(old_path, "synced")
            self._notify_deletions()
        if new_path == old_path:
            return  # No change

        # Rename/move the local folder
        old_local = self.local_root / old_path
        new_local = self.local_root / new_path

        if old_local.exists():
            new_local.parent.mkdir(parents=True, exist_ok=True)
            if self._suppress_fn:
                self._suppress_fn(old_path)
                self._suppress_fn(new_path)

            old_local.rename(new_local)
            log.info("Renamed folder: %s -> %s", old_path, new_path)
            self._unsuppress_later(old_path, new_path)

        # Update state DB: folder itself
        self.state_db.upsert(FileState(
            path=new_path,
            file_id=file_id,
            remote_md5=None,
            local_md5=None,
            remote_mtime=meta.get("modifiedTime", ""),
            local_mtime=0,
            mime_type="application/vnd.google-apps.folder",
            is_folder=True,
            sync_status="synced",
        ))
        # Delete old path entry
        if old_path != new_path:
            self.state_db.delete_by_path(old_path)

        # Update all children paths in state DB
        self.state_db.rename_prefix(old_path, new_path)

    def _ensure_remote_dirs(self, rel_path: str) -> str:
        """Ensure parent directories exist on Drive. Returns parent folder ID."""
        parts = Path(rel_path).parts[:-1]  # All dirs except the filename
        parent_id = self.config.remote_id

        for depth, part in enumerate(parts, start=1):
            # Check if folder exists in state
            dir_path = "/".join(parts[:depth])
            state = self.state_db.get_by_path(dir_path)
            if state and state.sync_status != DELETED_ON_DRIVE:
                parent_id = state.file_id
            else:
                # Check on Drive
                folder_id = self.drive.find_folder(part, parent_id)
                if folder_id:
                    parent_id = folder_id
                else:
                    parent_id = self.drive.create_folder(part, parent_id)

                self.state_db.upsert(FileState(
                    path=dir_path,
                    file_id=parent_id,
                    remote_md5=None,
                    local_md5=None,
                    remote_mtime="",
                    local_mtime=0,
                    mime_type="application/vnd.google-apps.folder",
                    is_folder=True,
                    sync_status="synced",
                ))

        return parent_id

    def _resolve_remote_path(self, meta: dict) -> Optional[str]:
        """Resolve the local relative path of a remote file from its parent chain.

        Returns None when the chain does not lead into this sync root (unless
        "Shared with me" is synced, in which case it goes under that folder).
        Raises on transient API errors — never returns a guessed, partial path.
        """
        parts = [sanitize_filename(meta["name"])]
        current = meta
        root_ids = self._root_ids()
        seen = set()

        while True:
            parents = current.get("parents") or []
            if not parents:
                return self._outside_root_path(parts)
            parent_id = parents[0]
            if parent_id in root_ids:
                return "/".join(parts)
            if parent_id in seen:
                return None  # cycle
            seen.add(parent_id)

            # Known folder: its stored path is the full prefix
            state = self.state_db.get_by_file_id(parent_id)
            if state:
                return "/".join(state.path.split("/") + parts)

            try:
                parent_meta = self.drive.get_file_metadata(parent_id)
            except Exception as e:
                if is_not_found(e):  # the chain leaves what we can see
                    return self._outside_root_path(parts)
                raise
            parts.insert(0, sanitize_filename(parent_meta["name"]))
            current = parent_meta

    def _outside_root_path(self, parts: list[str]) -> Optional[str]:
        """Path for a file whose parent chain does not reach our root."""
        if self.config.include_shared_with_me:
            return "/".join(["Shared with me"] + parts)
        return None

    def _is_shared_file(self, meta: dict) -> bool:
        """Check if a file is shared with us (not owned by us).

        Walks the parent chain to detect files inside shared folders.
        Returns True if the file or any parent is not owned by us.

        IMPORTANT: We must always verify ownership via API, not trust the state DB.
        The state DB tracks files we've synced, but doesn't reliably indicate ownership
        because folders can be indexed via Changes API even if they're shared.
        """
        # Quick check: if file itself is not owned, it's shared
        if meta.get("ownedByMe") is False:
            return True

        # Walk parent chain to check ownership via API
        current = meta
        root_ids = self._root_ids()
        checked = set()

        while True:
            parents = current.get("parents", [])
            if not parents:
                # Orphan — treat as shared (safer default)
                return True
            parent_id = parents[0]

            # Reached our root — not shared
            if parent_id in root_ids:
                return False

            # Prevent infinite loop
            if parent_id in checked:
                return True
            checked.add(parent_id)

            # Always query Drive for parent metadata to verify ownership
            # Do NOT use state DB as a shortcut — it doesn't track ownership reliably
            try:
                parent_meta = self.drive.get_file_metadata(parent_id)
                if parent_meta.get("ownedByMe") is False:
                    return True
                current = parent_meta
            except Exception as e:
                if is_not_found(e):  # outside what we can see: not ours
                    return True
                raise  # transient: retry later rather than silently skip
