"""Core sync engine for SyncoPath — handles bidirectional sync logic."""
import hashlib
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import AccountConfig, CONFIG_DIR
from .drive_api import DriveAPI, GOOGLE_NATIVE_MIMES, GOOGLE_EXPORT_MAP, GOOGLE_SKIP_MIMES, is_skip_mime, sanitize_filename
from .state import StateDB, FileState
from .local_watcher import LocalChangeEvent
from .remote_watcher import RemoteChange

log = logging.getLogger(__name__)


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

    def set_watcher_suppress(self, suppress_fn, unsuppress_fn):
        """Set functions to suppress/unsuppress local watcher events."""
        self._suppress_fn = suppress_fn
        self._unsuppress_fn = unsuppress_fn

    def set_status_callback(self, callback):
        """Set callback for sync status updates: callback(status, detail)."""
        self._status_callback = callback

    def set_progress_callback(self, callback):
        """Set callback for progress updates: callback(current, total, filename)."""
        self._progress_callback = callback

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

        # On first sync, ensure target directory is empty to prevent mixing files
        if not any(self.local_root.iterdir()) if self.local_root.exists() else True:
            pass  # Good — empty or non-existent
        else:
            # Directory has contents — check if we have state (resuming) vs truly new
            if self.state_db.get_page_token() is None:
                validate_sync_dir_empty(self.local_root)

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

            # Download
            try:
                if not self._check_disk_space():
                    log.warning("Sync paused due to low disk space at file: %s", rel_path)
                    break
                self._download_file(f, rel_path)
            except Exception as e:
                # _download_file handles warning/error classification internally
                # and no longer re-raises for unsyncable files
                log.warning("Skipping file %s: %s", rel_path, e)
                continue

        # Get page token for future change tracking
        token = self.drive.get_start_page_token()
        self.state_db.set_page_token(token)

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

                            self._download_file(meta, state.path)
                        else:
                            # File trashed on remote — remove from state
                            self.state_db.delete_by_path(state.path)
                    except Exception as e:
                        err_str = str(e)
                        if "404" in err_str or "notFound" in err_str:
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
        finally:
            self._notify_status("idle")

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
        finally:
            self._notify_status("idle")

    def _handle_local_event(self, event: LocalChangeEvent):
        """Handle a single local filesystem event."""
        if event.event_type == "created" or event.event_type == "modified":
            self._upload_local_file(event.path)
        elif event.event_type == "deleted":
            self._delete_remote_file(event.path)
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
                    file_id=file_id or "",
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
                # Mark file as error in Thunar
                if self._overlay:
                    self._overlay.set_file_error(rel_path)
                raise

    def _delete_remote_file(self, rel_path: str):
        """Delete a file from Drive (trash it)."""
        if self._safe_mode:
            log.info("SAFE MODE: would delete remote %s — skipped", rel_path)
            return

        state = self.state_db.get_by_path(rel_path)
        if not state:
            return

        # Only delete remotely if the file hasn't been modified on remote
        try:
            remote_meta = self.drive.get_file_metadata(state.file_id)
            if remote_meta.get("md5Checksum") != state.remote_md5:
                log.warning("Remote file modified, skipping delete: %s", rel_path)
                return
        except Exception:
            pass  # File might already be gone

        try:
            self.drive.delete_file(state.file_id)
            log.info("Trashed remote: %s", rel_path)
        except Exception as e:
            log.warning("Failed to trash remote %s: %s", rel_path, e)

        self.state_db.delete_by_path(rel_path)

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

        try:
            kwargs = {"fileId": state.file_id, "supportsAllDrives": True}
            if body:
                kwargs["body"] = body
            if add_parents:
                kwargs["addParents"] = add_parents
            if remove_parents:
                kwargs["removeParents"] = remove_parents

            self.drive.move_file(state.file_id, body=body or None,
                                   add_parents=add_parents, remove_parents=remove_parents)
            self.state_db.rename(old_path, new_path)
            log.info("Moved remote: %s -> %s", old_path, new_path)
        except Exception as e:
            log.error("Failed to move remote: %s", e)

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
                    if self._unsuppress_fn:
                        debounce = self.config.debounce + 1
                        threading.Timer(debounce, self._unsuppress_fn, args=[state.path]).start()
                        threading.Timer(debounce, self._unsuppress_fn, args=[new_rel_path]).start()

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
                    # Local wins — skip download, will upload on next local event
                    log.warning("Conflict (local wins): %s", rel_path)
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
                self.state_db.upsert(FileState(
                    path=rel_path,
                    file_id=meta["id"],
                    remote_md5=meta.get("md5Checksum"),
                    local_md5=None,
                    remote_mtime=meta["modifiedTime"],
                    local_mtime=0,
                    mime_type=mime_type,
                    sync_status="error",
                ))
                if self._overlay:
                    self._overlay.set_file_error(rel_path)
        finally:
            # Unsuppress after download completes — wait for debounce window
            # so inotify doesn't pick up the just-written file as a new change
            if self._unsuppress_fn:
                debounce = self.config.debounce + 1  # slightly more than watcher debounce
                threading.Timer(debounce, self._unsuppress_fn, args=[rel_path]).start()

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
        error_str = str(error).lower()
        unsyncable_indicators = [
            "invalid",
            "file name too long",
            "notfound",  # parent folder gone
        ]
        # Permission errors on the local file itself
        if isinstance(error, PermissionError):
            return True
        if isinstance(error, OSError) and "errno" in error_str:
            return True
        # Check filename length (Drive limit is 255 chars)
        if len(local_path.name) > 255:
            return True
        return any(indicator in error_str for indicator in unsyncable_indicators)

    def _delete_local_file(self, file_id: str):
        """Delete a local file that was trashed on remote."""
        if self._safe_mode:
            log.info("SAFE MODE: would delete local file (id=%s) — skipped", file_id)
            return

        state = self.state_db.get_by_file_id(file_id)
        if not state:
            return

        local_path = self.local_root / state.path
        if local_path.exists():
            # Only delete if unmodified locally
            local_hash = md5_file(local_path)
            if local_hash != state.local_md5:
                log.warning("Local file modified, keeping: %s", state.path)
                self.state_db.delete_by_file_id(file_id)
                return

            if self._suppress_fn:
                self._suppress_fn(state.path)
            local_path.unlink()
            log.info("Deleted local: %s", state.path)
            if self._unsuppress_fn:
                threading.Timer(2.0, self._unsuppress_fn, args=[state.path]).start()

        self.state_db.delete_by_file_id(file_id)

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

            if self._unsuppress_fn:
                threading.Timer(2.0, self._unsuppress_fn, args=[old_path]).start()
                threading.Timer(2.0, self._unsuppress_fn, args=[new_path]).start()

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

        for part in parts:
            # Check if folder exists in state
            dir_path = str(Path(*parts[: parts.index(part) + 1]))
            state = self.state_db.get_by_path(dir_path)
            if state:
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
        """Resolve the relative path for a remote file from its parents."""
        parts = [sanitize_filename(meta["name"])]
        current = meta

        # Get the real root ID (resolves 'root' alias to actual folder ID)
        real_root_id = self.drive.get_real_root_id()
        root_ids = {self.config.remote_id, real_root_id, "root"}

        while True:
            parents = current.get("parents", [])
            if not parents:
                break
            parent_id = parents[0]
            if parent_id in root_ids:
                break

            # Check state DB first
            state = self.state_db.get_by_file_id(parent_id)
            if state:
                # Use the full stored path (not just the name)
                parts = state.path.split("/") + parts
                break

            # Query Drive for parent name
            try:
                parent_meta = self.drive.get_file_metadata(parent_id)
                parts.insert(0, sanitize_filename(parent_meta["name"]))
                current = parent_meta
            except Exception:
                break

        return "/".join(parts)

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
        real_root_id = self.drive.get_real_root_id()
        root_ids = {self.config.remote_id, real_root_id, "root"}
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
            except Exception:
                # Can't verify — assume shared (safer)
                return True
