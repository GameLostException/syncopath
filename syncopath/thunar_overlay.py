"""Thunar file overlay integration for SyncoPath.

Uses two mechanisms:
1. libcloudproviders D-Bus API — registers SyncoPath as a cloud provider,
   giving a sidebar entry + folder-level status in Thunar.
2. GIO metadata::emblems — per-file overlay icons (synced/syncing/error).
"""
import logging
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional

import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("CloudProviders", "0.3")
from gi.repository import Gio, GLib, CloudProviders

log = logging.getLogger(__name__)

DBUS_BUS_NAME = "net.syncopath.SyncoPath"
DBUS_OBJECT_PATH = "/net/syncopath/SyncoPath"

# Emblem names (must match installed SVGs in hicolor/scalable/emblems/)
EMBLEM_SYNCED = "syncopath-synced"
EMBLEM_SYNCING = "syncopath-syncing"
EMBLEM_ERROR = "syncopath-error"
EMBLEM_WARNING = "syncopath-warning"
EMBLEM_PENDING = "syncopath-pending"
EMBLEM_UNKNOWN = "syncopath-unknown"


class FileStatus(Enum):
    SYNCED = "synced"
    SYNCING = "syncing"
    ERROR = "error"
    WARNING = "warning"
    PENDING = "pending"
    UNKNOWN = "unknown"
    NONE = "none"


class ThunarOverlay:
    """Manages Thunar overlay icons via libcloudproviders + GIO emblems."""

    # Debounce Thunar refreshes: don't refresh more than once per 5 seconds
    _last_refresh_time = 0.0
    _refresh_pending = False

    def __init__(self, account_name: str, sync_path: Path):
        self.account_name = account_name
        # D-Bus object paths can't have hyphens — sanitize
        self._dbus_account_id = account_name.replace("-", "_")
        self.sync_path = sync_path
        self._provider_exporter: Optional[CloudProviders.ProviderExporter] = None
        self._account_exporter: Optional[CloudProviders.AccountExporter] = None
        self._bus_owner_id: int = 0
        self._connected = False
        self._state_db = None  # Set later for dir emblem updates

    def set_state_db(self, state_db):
        """Set state DB reference for directory emblem computation."""
        self._state_db = state_db

    def start(self):
        """Register as a cloud provider on the session bus."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._on_bus_acquired(bus, DBUS_BUS_NAME)
            self._connected = True
            log.info("Cloud provider D-Bus registered")
        except Exception as e:
            log.warning("Failed to register cloud provider: %s", e)

    def stop(self):
        """Unregister from D-Bus."""
        self._provider_exporter = None
        self._account_exporter = None
        self._connected = False
        log.info("Cloud provider D-Bus unregistered")

    def _on_bus_acquired(self, connection: Gio.DBusConnection, name: str):
        """Called when we acquire the session bus connection."""
        self._provider_exporter = CloudProviders.ProviderExporter.new(
            connection, DBUS_BUS_NAME, DBUS_OBJECT_PATH
        )
        self._provider_exporter.set_name("SyncoPath")

        self._account_exporter = CloudProviders.AccountExporter.new(
            self._provider_exporter, self._dbus_account_id
        )
        self._account_exporter.set_name(f"SyncoPath ({self.account_name})")
        self._account_exporter.set_path(str(self.sync_path))
        self._account_exporter.set_status(CloudProviders.AccountStatus.IDLE)
        self._account_exporter.set_status_details("Synced")

        # Set icon
        icon = Gio.ThemedIcon.new("folder-remote")
        self._account_exporter.set_icon(icon)

        log.info("Cloud provider exported: %s -> %s", self.account_name, self.sync_path)

    def _on_name_acquired(self, connection: Gio.DBusConnection, name: str):
        """Called when our D-Bus name is acquired."""
        self._connected = True
        log.debug("D-Bus name acquired: %s", name)

    def _on_name_lost(self, connection: Gio.DBusConnection, name: str):
        """Called when our D-Bus name is lost."""
        self._connected = False
        log.warning("D-Bus name lost: %s", name)

    def set_account_status(self, status: str, detail: str = ""):
        """Update the overall account sync status shown in Thunar sidebar.

        Args:
            status: One of "idle", "syncing", "error"
            detail: Human-readable status message
        """
        if not self._account_exporter:
            return

        status_map = {
            "idle": CloudProviders.AccountStatus.IDLE,
            "syncing": CloudProviders.AccountStatus.SYNCING,
            "error": CloudProviders.AccountStatus.ERROR,
        }
        cp_status = status_map.get(status, CloudProviders.AccountStatus.IDLE)
        self._account_exporter.set_status(cp_status)
        if detail:
            self._account_exporter.set_status_details(detail)

    def set_file_status(self, rel_path: str, status: FileStatus):
        """Set the overlay emblem on a specific file.

        Args:
            rel_path: Path relative to sync root
            status: FileStatus enum value
        """
        abs_path = self.sync_path / rel_path
        if not abs_path.exists():
            return

        emblem_map = {
            FileStatus.SYNCED: EMBLEM_SYNCED,
            FileStatus.SYNCING: EMBLEM_SYNCING,
            FileStatus.ERROR: EMBLEM_ERROR,
            FileStatus.WARNING: EMBLEM_WARNING,
            FileStatus.PENDING: EMBLEM_PENDING,
            FileStatus.UNKNOWN: EMBLEM_UNKNOWN,
            FileStatus.NONE: None,
        }
        emblem = emblem_map.get(status)

        try:
            if emblem:
                subprocess.run(
                    ["gio", "set", "-t", "stringv", str(abs_path),
                     "metadata::emblems", f"emblem-{emblem}"],
                    capture_output=True, timeout=5,
                )
            else:
                subprocess.run(
                    ["gio", "set", str(abs_path), "-t", "unset",
                     "metadata::emblems"],
                    capture_output=True, timeout=5,
                )
            # Schedule a debounced Thunar refresh
            self._schedule_thunar_refresh()
        except Exception as e:
            log.debug("Failed to set emblem on %s: %s", rel_path, e)

    def set_file_synced(self, rel_path: str):
        """Mark a file as synced (green checkmark)."""
        self.set_file_status(rel_path, FileStatus.SYNCED)
        self._update_parent_dirs(rel_path)

    def set_file_syncing(self, rel_path: str):
        """Mark a file as currently syncing (blue arrows)."""
        self.set_file_status(rel_path, FileStatus.SYNCING)
        self._update_parent_dirs(rel_path)

    def set_file_pending(self, rel_path: str):
        """Mark a file as pending sync (grey clock)."""
        self.set_file_status(rel_path, FileStatus.PENDING)
        self._update_parent_dirs(rel_path)

    def set_file_error(self, rel_path: str):
        """Mark a file as having a sync error (red circle, white cross).
        Used for actual failures: network errors, permission denied, etc.
        """
        self.set_file_status(rel_path, FileStatus.ERROR)
        self._update_parent_dirs(rel_path)

    def set_file_warning(self, rel_path: str):
        """Mark a file as unsyncable (warning triangle).
        Used for files that cannot be synced: too large for export,
        unsupported type, bad filename, inaccessible, etc.
        """
        self.set_file_status(rel_path, FileStatus.WARNING)
        self._update_parent_dirs(rel_path)

    def clear_file_status(self, rel_path: str):
        """Remove overlay from a file."""
        self.set_file_status(rel_path, FileStatus.NONE)
        self._update_parent_dirs(rel_path)

    def _update_parent_dirs(self, rel_path: str):
        """Update parent directory emblems after a file status change."""
        if self._state_db and "/" in rel_path:
            self.update_dir_emblems_for_path(rel_path, self._state_db)

    def _schedule_thunar_refresh(self):
        """Schedule a debounced Thunar refresh (max once per 5 seconds)."""
        import time
        now = time.time()
        if now - ThunarOverlay._last_refresh_time < 5.0:
            # Already refreshed recently, schedule one for later if not pending
            if not ThunarOverlay._refresh_pending:
                ThunarOverlay._refresh_pending = True
                import threading
                threading.Timer(5.0, self._do_thunar_refresh).start()
            return
        self._do_thunar_refresh()

    def _do_thunar_refresh(self):
        """Send F5 to all Thunar windows to refresh their view."""
        import time
        ThunarOverlay._last_refresh_time = time.time()
        ThunarOverlay._refresh_pending = False
        try:
            subprocess.run(
                ["xdotool", "search", "--name", "Thunar", "key", "--window", "%@", "F5"],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass  # xdotool not installed or no Thunar windows

    def mark_all_synced(self):
        """Mark all files in the sync folder as synced.
        Used after initial sync completes.
        """
        count = 0
        for path in self.sync_path.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                rel = str(path.relative_to(self.sync_path))
                self.set_file_synced(rel)
                count += 1
                if count % 100 == 0:
                    log.debug("Marked %d files as synced", count)
        log.info("Marked %d files as synced", count)
        # All files synced → all dirs synced
        self._update_all_dir_emblems({})

    def mark_unknown_files(self, state_db):
        """Apply emblems based on DB status. Untracked files get 'unknown'.

        Args:
            state_db: StateDB instance to check tracked files against.
        """
        # Get all known statuses from DB in one query
        known_statuses = state_db.get_all_paths_with_status()

        status_to_file_status = {
            "synced": FileStatus.SYNCED,
            "syncing": FileStatus.SYNCING,
            "pending": FileStatus.PENDING,
            "error": FileStatus.ERROR,
            "warning": FileStatus.WARNING,
            "unknown": FileStatus.UNKNOWN,
        }

        # Track per-file status for directory computation
        file_statuses: dict[str, FileStatus] = {}

        count_unknown = 0
        count_restored = 0
        for path in self.sync_path.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                rel = str(path.relative_to(self.sync_path))
                db_status = known_statuses.get(rel)
                if db_status:
                    fs = status_to_file_status.get(db_status, FileStatus.UNKNOWN)
                    self.set_file_status(rel, fs)
                    file_statuses[rel] = fs
                    count_restored += 1
                else:
                    self.set_file_status(rel, FileStatus.UNKNOWN)
                    file_statuses[rel] = FileStatus.UNKNOWN
                    count_unknown += 1

        log.info("Startup emblems: %d restored from DB, %d marked unknown",
                 count_restored, count_unknown)

        # Now compute and set directory emblems
        self._update_all_dir_emblems(file_statuses)

    def _update_all_dir_emblems(self, file_statuses: dict):
        """Compute and set emblems for all directories based on their contents.

        Priority order (highest to lowest):
        1. ERROR - if any file in the tree has a sync error
        2. WARNING - if any file in the tree is unsyncable
        3. UNKNOWN - if any file in the tree is unknown
        4. SYNCING - if any file in the tree is syncing
        5. PENDING - if any file in the tree is pending
        6. SYNCED - only if ALL files in the tree are synced
        """
        # Collect all directories
        dirs = set()
        for path in self.sync_path.rglob("*"):
            if path.is_dir() and not path.name.startswith("."):
                dirs.add(path)

        # Priority: lower index = higher priority
        priority = [FileStatus.ERROR, FileStatus.WARNING, FileStatus.UNKNOWN,
                    FileStatus.SYNCING, FileStatus.PENDING, FileStatus.SYNCED]

        count = 0
        for dir_path in sorted(dirs):
            dir_status = self._compute_dir_status(dir_path, file_statuses, priority)
            rel = str(dir_path.relative_to(self.sync_path))
            self.set_file_status(rel, dir_status)
            count += 1

        log.info("Set emblems on %d directories", count)

    def _compute_dir_status(self, dir_path: Path, file_statuses: dict,
                            priority: list) -> FileStatus:
        """Compute a directory's status from all files in its tree."""
        worst = FileStatus.SYNCED  # Assume best case
        worst_idx = priority.index(FileStatus.SYNCED)

        for path in dir_path.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                rel = str(path.relative_to(self.sync_path))
                status = file_statuses.get(rel, FileStatus.UNKNOWN)
                try:
                    idx = priority.index(status)
                except ValueError:
                    idx = 1  # Treat unknown statuses as UNKNOWN priority
                if idx < worst_idx:
                    worst = status
                    worst_idx = idx
                    if worst_idx == 0:
                        break  # Can't get worse than ERROR

        return worst

    def update_dir_emblems_for_path(self, rel_path: str, state_db):
        """Update directory emblems for all parent directories of a file.
        Called after a file's status changes.
        """
        # Rebuild file_statuses for affected directories
        file_path = Path(rel_path)
        parts = file_path.parts[:-1]  # parent directories

        known_statuses = state_db.get_all_paths_with_status()
        status_to_file_status = {
            "synced": FileStatus.SYNCED,
            "syncing": FileStatus.SYNCING,
            "pending": FileStatus.PENDING,
            "error": FileStatus.ERROR,
            "warning": FileStatus.WARNING,
            "unknown": FileStatus.UNKNOWN,
        }

        priority = [FileStatus.ERROR, FileStatus.WARNING, FileStatus.UNKNOWN,
                    FileStatus.SYNCING, FileStatus.PENDING, FileStatus.SYNCED]

        # Walk up from the file to the root, updating each parent dir
        for i in range(len(parts)):
            dir_rel = str(Path(*parts[:i + 1]))
            dir_abs = self.sync_path / dir_rel

            if not dir_abs.is_dir():
                continue

            # Scan all files in this directory tree
            worst = FileStatus.SYNCED
            worst_idx = priority.index(FileStatus.SYNCED)

            for path in dir_abs.rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    frel = str(path.relative_to(self.sync_path))
                    db_status = known_statuses.get(frel)
                    if db_status:
                        fs = status_to_file_status.get(db_status, FileStatus.UNKNOWN)
                    else:
                        fs = FileStatus.UNKNOWN
                    try:
                        idx = priority.index(fs)
                    except ValueError:
                        idx = 1
                    if idx < worst_idx:
                        worst = fs
                        worst_idx = idx
                        if worst_idx == 0:
                            break

            self.set_file_status(dir_rel, worst)
