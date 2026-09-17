"""Remote watcher using Google Drive Changes API polling."""
import logging
import threading
import time
from typing import Callable, Optional

from .drive_api import DriveAPI
from .state import StateDB

log = logging.getLogger(__name__)

# Delay (seconds) before each retry after consecutive poll failures.
# Index 0 = after 1st failure, index -1 = cap for all subsequent failures.
_FAILURE_BACKOFF = [30, 60, 120, 300]

# How often the watchdog checks whether the poll thread is still alive (seconds).
_WATCHDOG_CHECK_INTERVAL = 15


class RemoteChange:
    """Represents a remote Drive change."""

    def __init__(self, file_id: str, event_type: str, file_meta: Optional[dict] = None):
        self.file_id = file_id
        self.event_type = event_type  # "modified", "created", "deleted"
        self.file_meta = file_meta  # Drive file metadata
        self.timestamp = time.time()

    def __repr__(self):
        name = self.file_meta.get("name", "?") if self.file_meta else "?"
        return f"RemoteChange({self.event_type}: {name} [{self.file_id}])"


class RemoteWatcher:
    """Polls Google Drive Changes API for remote modifications.

    Design guarantees:
    - Interruptible sleep: poll interval uses threading.Event.wait() so stop()
      and trigger_poll() wake the thread immediately instead of waiting up to
      poll_interval seconds (avoids post-suspend delays).
    - Thread watchdog: a separate watchdog thread restarts the poll loop if it
      ever dies unexpectedly.
    - Failure backoff: consecutive API errors increase inter-poll delay up to
      _POLL_FAILURE_CAP seconds instead of hammering the API on auth failures.
    """

    def __init__(self, drive: DriveAPI, state_db: StateDB, poll_interval: float = 30.0,
                 watchdog_check_interval: float = _WATCHDOG_CHECK_INTERVAL):
        self.drive = drive
        self.state_db = state_db
        self.poll_interval = poll_interval
        self._watchdog_check_interval = watchdog_check_interval

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable] = None

        # Event used as an interruptible sleep — set it to wake the poll loop early.
        self._wake_event = threading.Event()
        # Protects _running flag and thread references across watchdog restarts.
        self._lock = threading.Lock()
        # Consecutive failure counter for backoff logic.
        self._consecutive_failures: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, callback: Callable[[list[RemoteChange]], None]):
        """Start polling in a background thread (with watchdog)."""
        self._callback = callback
        self._running = True
        self._wake_event.clear()

        # Ensure we have a page token
        token = self.state_db.get_page_token()
        if not token:
            token = self.drive.get_start_page_token()
            self.state_db.set_page_token(token)
            log.info("Initialized page token: %s", token)

        self._start_poll_thread()
        self._start_watchdog()
        log.info("Remote watcher started (interval=%ds)", self.poll_interval)

    def stop(self):
        """Stop polling and watchdog."""
        with self._lock:
            self._running = False
        # Wake the sleeping poll thread so it exits promptly.
        self._wake_event.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=5)
        if self._thread:
            self._thread.join(timeout=5)

    def trigger_poll(self):
        """Wake the poll loop immediately (e.g. on manual sync request)."""
        self._wake_event.set()

    def poll_once(self) -> list[RemoteChange]:
        """Run a single poll cycle. Returns list of changes.

        Note: this method does not update the consecutive-failure counter used
        for backoff — it is intended for external callers (e.g. tests) that need
        a synchronous poll. The internal poll loop uses _poll_with_tracking().
        """
        token = self.state_db.get_page_token()
        if not token:
            token = self.drive.get_start_page_token()

        try:
            raw_changes, new_token = self.drive.get_changes(token)
        except Exception as e:
            log.error("Failed to poll remote changes: %s", e)
            return []

        self.state_db.set_page_token(new_token)
        return self._parse_changes(raw_changes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_changes(self, raw_changes: list[dict]) -> list[RemoteChange]:
        """Convert raw Drive API change dicts into RemoteChange objects."""
        changes = []
        for change in raw_changes:
            file_id = change["fileId"]
            removed = change.get("removed", False)
            file_meta = change.get("file")

            if removed or (file_meta and file_meta.get("trashed")):
                changes.append(RemoteChange(file_id, "deleted", file_meta))
            elif file_meta:
                existing = self.state_db.get_by_file_id(file_id)
                if existing:
                    changes.append(RemoteChange(file_id, "modified", file_meta))
                else:
                    changes.append(RemoteChange(file_id, "created", file_meta))

        if changes:
            log.info("Remote poll: %d change(s) detected", len(changes))
            for c in changes:
                log.debug("  %s", c)

        return changes

    def _start_poll_thread(self):
        """Spawn (or re-spawn) the poll loop thread."""
        self._wake_event.clear()
        t = threading.Thread(target=self._poll_loop, daemon=True, name="remote-watcher")
        t.start()
        with self._lock:
            self._thread = t

    def _start_watchdog(self):
        """Spawn a watchdog that restarts the poll thread if it dies."""
        wt = threading.Thread(target=self._watchdog_loop, daemon=True,
                              name="remote-watcher-watchdog")
        wt.start()
        self._watchdog_thread = wt

    def _watchdog_loop(self):
        """Monitor the poll thread and restart it if it dies unexpectedly."""
        while True:
            with self._lock:
                if not self._running:
                    break
                thread = self._thread

            # Check periodically with a fixed short interval rather than joining
            # with a long timeout — this bounds restart latency regardless of
            # the configured poll_interval.
            if thread is not None:
                thread.join(timeout=self._watchdog_check_interval)

            with self._lock:
                if not self._running:
                    break
                # If thread is dead but we're still supposed to be running, restart.
                if self._thread is not None and not self._thread.is_alive():
                    log.warning("Remote watcher poll thread died unexpectedly — restarting")
                    self._start_poll_thread()

        log.debug("Remote watcher watchdog exited")

    def _poll_loop(self):
        """Background polling loop with interruptible sleep and failure backoff."""
        log.debug("Poll loop started")
        while True:
            with self._lock:
                if not self._running:
                    break

            # --- Poll ---
            changes = self._poll_with_tracking()
            if changes and self._callback:
                try:
                    self._callback(changes)
                except Exception as e:
                    log.error("Error processing remote changes: %s", e)

            # --- Interruptible sleep ---
            # Use failure backoff if consecutive errors are accumulating,
            # otherwise use the configured poll_interval.
            if self._consecutive_failures > 0:
                backoff_idx = min(self._consecutive_failures - 1, len(_FAILURE_BACKOFF) - 1)
                sleep_duration = _FAILURE_BACKOFF[backoff_idx]
                log.debug("Poll backoff: sleeping %ds (failure #%d)",
                          sleep_duration, self._consecutive_failures)
            else:
                sleep_duration = self.poll_interval

            # threading.Event.wait() returns immediately if set, otherwise waits.
            # This means stop() and trigger_poll() both wake us up right away.
            self._wake_event.wait(timeout=sleep_duration)
            self._wake_event.clear()

        log.debug("Poll loop exited")

    def _poll_with_tracking(self) -> list[RemoteChange]:
        """Poll for changes and track consecutive failures for backoff."""
        try:
            token = self.state_db.get_page_token()
            if not token:
                token = self.drive.get_start_page_token()

            raw_changes, new_token = self.drive.get_changes(token)
            self.state_db.set_page_token(new_token)

            if self._consecutive_failures > 0:
                log.info("Remote poll recovered after %d consecutive failure(s)",
                         self._consecutive_failures)
            self._consecutive_failures = 0

            return self._parse_changes(raw_changes)

        except Exception as e:
            self._consecutive_failures += 1
            log.error("Failed to poll remote changes (failure #%d): %s",
                      self._consecutive_failures, e)
            return []
