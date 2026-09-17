"""SyncoPath entry point — The obsessive Google Drive syncer for Linux 🔪📁"""
import argparse
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from .config import AppConfig, AccountConfig, init_config
from .drive_api import DriveAPI
from .state import StateDB
from .sync_engine import SyncEngine
from .local_watcher import LocalWatcher
from .remote_watcher import RemoteWatcher
from .tray import TrayIcon
from .thunar_overlay import ThunarOverlay
from .sync_worker import SyncWorker

log = logging.getLogger("syncopath")


def _format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    elif size_bytes < 1024 * 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024 * 1024):.1f} TB"


class SyncoPathDaemon:
    """Main daemon coordinating all sync accounts."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts: list[dict] = []
        self.tray = TrayIcon(icon_theme=config.icon_theme)
        self._running = False
        self._overlays: list[ThunarOverlay] = []
        self._workers: list[SyncWorker] = []

    def setup(self):
        """Initialize all account sync engines."""
        for acc_config in self.config.accounts:
            acc_config.local_path.mkdir(parents=True, exist_ok=True)

            drive = DriveAPI(acc_config.name, acc_config.client_id, acc_config.client_secret,
                            shared_drive_id=acc_config.shared_drive_id or None,
                            bandwidth_limit_kib=acc_config.bandwidth_limit_kib)
            state_db = StateDB(acc_config.name)
            engine = SyncEngine(acc_config, drive, state_db, safe_mode=self.config.safe_mode,
                                min_free_space_gb=self.config.min_free_space_gb)
            local_watcher = LocalWatcher(
                acc_config.local_path, acc_config.exclude, acc_config.debounce
            )
            remote_watcher = RemoteWatcher(drive, state_db, acc_config.poll_interval)

            # Wire up suppression
            engine.set_watcher_suppress(local_watcher.suppress, local_watcher.unsuppress)
            engine.set_status_callback(self._on_status_change)
            engine.set_progress_callback(self._on_progress_change)

            # Setup Thunar overlay integration
            overlay = ThunarOverlay(acc_config.name, acc_config.local_path)
            overlay.set_state_db(state_db)
            engine.set_overlay(overlay)
            self._overlays.append(overlay)

            self.accounts.append({
                "config": acc_config,
                "drive": drive,
                "state_db": state_db,
                "engine": engine,
                "local_watcher": local_watcher,
                "remote_watcher": remote_watcher,
            })

        # Setup tray callbacks
        self.tray.set_callbacks(
            on_pause=self._pause_all,
            on_resume=self._resume_all,
            on_sync_now=self._sync_now,
            on_quit=self._quit,
            on_open_folder=self._open_folder,
            on_preferences=self._show_preferences,
        )

    def run(self):
        """Start the daemon with GTK main loop."""
        self._running = True

        # Start cloud provider D-Bus exports after GTK main loop starts
        GLib.idle_add(self._start_overlays)

        # Start sync threads
        sync_thread = threading.Thread(target=self._start_sync, daemon=True)
        sync_thread.start()

        # Start tray icon on main thread (GTK requirement)
        self.tray.start()

        # Handle signals via GLib-compatible approach
        def _on_signal(*args):
            GLib.idle_add(self._quit)
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

        # Run GTK main loop
        Gtk.main()

    def _start_sync(self):
        """Start all watchers and perform initial sync if needed."""
        for acc in self.accounts:
            engine = acc["engine"]
            state_db = acc["state_db"]
            local_watcher = acc["local_watcher"]
            remote_watcher = acc["remote_watcher"]
            acc_name = acc["config"].name

            # Show startup activity immediately
            self.tray.update_status("working", f"Starting {acc_name}...")

            # Immediately mark files with their current status (synced or unknown)
            overlay_idx = next(
                (i for i, o in enumerate(self._overlays)
                 if o.account_name == acc_name), None
            )
            if overlay_idx is not None:
                self.tray.update_status("working", f"Restoring file emblems...")
                # Run synchronously — no concurrent threads during startup
                self._overlays[overlay_idx].mark_unknown_files(state_db)

            # Check if initial sync is needed
            if state_db.get_page_token() is None:
                log.info("First run for %s — performing initial sync", acc_name)
                self.tray.update_status("syncing", f"Initial sync for {acc_name}...")
                try:
                    engine.initial_sync()
                except Exception as e:
                    log.error("Initial sync failed for %s: %s", acc_name, e)
                    self.tray.update_status("error", f"Initial sync failed: {e}")
                    continue
            else:
                # Not first run — re-index folders to catch any created while offline
                self.tray.update_status("working", f"Indexing folders...")
                try:
                    engine._index_remote_folders()
                except Exception as e:
                    log.warning("Folder index failed for %s: %s", acc_name, e)

                # Scan for local files added while service was offline
                self.tray.update_status("working", f"Scanning local files...")
                try:
                    engine.scan_local_untracked()
                except Exception as e:
                    log.warning("Local scan failed for %s: %s", acc_name, e)

                # Scan for remote files not downloaded (created while service was offline)
                self.tray.update_status("working", f"Checking for missing remote files...")
                try:
                    engine.scan_remote_undownloaded()
                except Exception as e:
                    log.warning("Remote scan failed for %s: %s", acc_name, e)

            # Create sync worker for this account
            self.tray.update_status("working", f"Starting watchers...")
            worker = SyncWorker()
            worker.set_handlers(
                on_local_changes=engine.handle_local_changes,
                on_remote_changes=engine.handle_remote_changes,
                on_reconcile=engine.reconcile,
                on_folder_index=engine._index_remote_folders,
            )
            worker.start()
            self._workers.append(worker)
            acc["worker"] = worker

            # Start watchers — they enqueue into the worker
            local_watcher.start(worker.enqueue_local)
            remote_watcher.start(worker.enqueue_remote)

            # Schedule initial reconcile (catches error/stale files from previous run)
            worker.enqueue_reconcile()

            # Schedule periodic reconcile every 10 minutes
            def _periodic_reconcile(w=worker):
                while self._running:
                    time.sleep(600)  # 10 minutes
                    if self._running:
                        w.enqueue_reconcile()
            threading.Thread(target=_periodic_reconcile, daemon=True,
                           name="periodic-reconcile").start()

            log.info("Account '%s' syncing: %s", acc_name, acc["config"].local_path)

        self.tray.update_status("idle", "All accounts synced")

    def _on_status_change(self, status: str, detail: str = ""):
        """Handle status change from any sync engine."""
        self.tray.update_status(status, detail)

    def _on_progress_change(self, current: int, total: int, filename: str = "",
                            file_size: int = 0):
        """Handle progress update from sync engine."""
        self.tray.update_progress(current, total, filename, file_size)

    def _start_overlays(self):
        """Start cloud provider D-Bus exports (called from main loop)."""
        for overlay in self._overlays:
            overlay.start()
        return False  # Don't repeat

    def _pause_all(self):
        """Pause all sync operations."""
        for acc in self.accounts:
            acc["remote_watcher"].stop()
            acc["local_watcher"].stop()
        log.info("All sync paused")

    def _resume_all(self):
        """Resume all sync operations."""
        for acc in self.accounts:
            worker = acc.get("worker")
            acc["local_watcher"].start(worker.enqueue_local if worker else None)
            acc["remote_watcher"].start(worker.enqueue_remote if worker else None)
        log.info("All sync resumed")

    def _sync_now(self):
        """Force an immediate sync cycle."""
        for acc in self.accounts:
            worker = acc.get("worker")
            remote_watcher = acc.get("remote_watcher")
            if worker and remote_watcher:
                # Wake the poll loop immediately rather than calling poll_once()
                # directly — this avoids a double-poll race and lets the watcher's
                # own backoff/failure-tracking logic run as normal.
                remote_watcher.trigger_poll()
                # Also enqueue a reconcile to catch any local drift.
                worker.enqueue_reconcile()

    def _open_folder(self):
        """Open the first account's sync folder."""
        if self.accounts:
            path = self.accounts[0]["config"].local_path
            subprocess.Popen(["xdg-open", str(path)])

    def _show_preferences(self):
        """Open the preferences window."""
        from .preferences import show_preferences
        show_preferences(on_save_callback=self._on_prefs_saved)

    def _on_prefs_saved(self):
        """Called when preferences are saved — signal that restart may be needed."""
        log.info("Preferences saved — restart to apply changes")

    def _quit(self, *args):
        """Graceful shutdown."""
        log.info("Shutting down...")
        self._running = False
        for overlay in self._overlays:
            overlay.stop()
        for worker in self._workers:
            worker.stop()
        for acc in self.accounts:
            acc["local_watcher"].stop()
            acc["remote_watcher"].stop()
            acc["state_db"].close()
        GLib.idle_add(Gtk.main_quit)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="SyncoPath 🔪📁 — The obsessive Google Drive syncer"
    )
    parser.add_argument("--init", action="store_true", help="Create default config")
    parser.add_argument("--auth", metavar="ACCOUNT", help="Authenticate an account")
    parser.add_argument("--prefs", action="store_true", help="Open preferences window")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--no-tray", action="store_true", help="Run without tray icon")
    parser.add_argument("--list-shared-drives", action="store_true",
                        help="List available shared drives")
    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # When running manually (not under systemd), also write to a log file so
    # crashes can be investigated without relying on journald.
    # systemd sets JOURNAL_STREAM when it owns our stdout/stderr.
    _log_dir = Path("/var/log/syncopath")
    if "JOURNAL_STREAM" not in os.environ and _log_dir.is_dir() and os.access(_log_dir, os.W_OK):
        _file_handler = logging.handlers.RotatingFileHandler(
            _log_dir / "syncopath.log",
            maxBytes=10 * 1024 * 1024,   # 10 MiB per file
            backupCount=5,
            encoding="utf-8",
        )
        _file_handler.setLevel(level)
        _file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logging.getLogger().addHandler(_file_handler)
        log.info("Logging to %s", _log_dir / "syncopath.log")

    if args.init:
        init_config()
        print("Config initialized. Edit ~/.config/syncopath/config.yaml then run 'syncopath --auth <account>'")
        return

    if args.auth:
        config = AppConfig.load()
        # Find account
        acc = next((a for a in config.accounts if a.name == args.auth), None)
        if not acc:
            print(f"Account '{args.auth}' not found in config. Available: {[a.name for a in config.accounts]}")
            sys.exit(1)
        drive = DriveAPI(acc.name, acc.client_id, acc.client_secret)
        drive.authenticate_interactive()
        print(f"✅ Account '{args.auth}' authenticated successfully!")
        return

    if args.list_shared_drives:
        config = AppConfig.load()
        # Use first account's credentials
        acc = config.accounts[0]
        drive = DriveAPI(acc.name, acc.client_id, acc.client_secret)
        drives = drive.list_shared_drives()
        if not drives:
            print("No shared drives found.")
        else:
            print(f"{'Name':<40} {'ID':<25} {'Size':<12} {'Enabled'}")
            print(f"{'─'*40} {'─'*25} {'─'*12} {'─'*7}")
            enabled_ids = {a.shared_drive_id for a in config.accounts if a.shared_drive_id}
            for d in sorted(drives, key=lambda x: x["name"]):
                enabled = "✅" if d["id"] in enabled_ids else ""
                # Fetch size
                try:
                    usage = drive.get_shared_drive_usage(d["id"])
                    size_str = _format_size(usage)
                except Exception:
                    size_str = "?"
                print(f"{d['name']:<40} {d['id']:<25} {size_str:<12} {enabled}")
            print(f"\nTo sync a shared drive, add it to ~/.config/syncopath/config.yaml:")
            print(f"  - name: shared-<name>")
            print(f"    local_path: /home/boris/shared-drives/<name>")
            print(f"    shared_drive_id: <ID>")
        return

    if args.prefs:
        from .preferences import show_preferences
        show_preferences()
        Gtk.main()
        return

    # Normal daemon mode
    try:
        config = AppConfig.load()
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)

    # Single-instance guard via PID file
    from .config import CONFIG_DIR
    pid_file = CONFIG_DIR / "syncopath.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            # Check if process is still running
            os.kill(old_pid, 0)
            print(f"SyncoPath already running (PID {old_pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # Stale PID file — process is dead
        except PermissionError:
            print(f"SyncoPath already running (PID {old_pid}). Exiting.")
            sys.exit(1)

    # Write our PID
    pid_file.write_text(str(os.getpid()))

    try:
        daemon = SyncoPathDaemon(config)
        daemon.setup()
        daemon.run()
    finally:
        # Clean up PID file on exit
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
