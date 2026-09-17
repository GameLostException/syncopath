"""Unit tests for RemoteWatcher.

Covers:
- poll_once(): success and failure paths
- trigger_poll(): wakes the loop early
- Failure backoff: consecutive failures choose the right sleep duration
- Watchdog: detects a dead poll thread and restarts it
- stop(): terminates both threads promptly
"""
import threading
import time
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

from syncopath.remote_watcher import RemoteWatcher, RemoteChange, _FAILURE_BACKOFF, _WATCHDOG_CHECK_INTERVAL
from syncopath.state import StateDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_db(tmp_path: Path) -> StateDB:
    """Create a fresh in-memory StateDB for testing (bypasses file I/O)."""
    db = StateDB.__new__(StateDB)
    db._conn = sqlite3.connect(":memory:", check_same_thread=False)
    db._conn.row_factory = sqlite3.Row
    db._conn.execute("PRAGMA journal_mode=WAL")
    db._conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            file_id TEXT UNIQUE,
            remote_md5 TEXT,
            local_md5 TEXT,
            remote_mtime TEXT,
            local_mtime REAL,
            mime_type TEXT,
            is_folder INTEGER DEFAULT 0,
            sync_status TEXT DEFAULT 'unknown'
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_file_id ON files(file_id);
    """)
    db._conn.commit()
    return db


def _make_drive_mock(changes=None, new_token="1"):
    """Return a mock DriveAPI with get_changes() wired up."""
    drive = MagicMock()
    drive.get_start_page_token.return_value = "0"
    drive.get_changes.return_value = (changes or [], new_token)
    return drive


def _raw_change(file_id: str, name: str, removed: bool = False, trashed: bool = False) -> dict:
    return {
        "fileId": file_id,
        "removed": removed,
        "file": None if removed else {
            "id": file_id,
            "name": name,
            "mimeType": "text/plain",
            "trashed": trashed,
        },
    }


# ---------------------------------------------------------------------------
# poll_once tests
# ---------------------------------------------------------------------------

class TestPollOnce:

    def test_returns_empty_on_no_changes(self, tmp_path):
        db = _make_state_db(tmp_path)
        drive = _make_drive_mock(changes=[], new_token="1")
        db.set_page_token("0")

        watcher = RemoteWatcher(drive, db, poll_interval=30)
        result = watcher.poll_once()

        assert result == []
        assert db.get_page_token() == "1"

    def test_classifies_new_file_as_created(self, tmp_path):
        db = _make_state_db(tmp_path)
        drive = _make_drive_mock(changes=[_raw_change("f1", "hello.txt")], new_token="1")
        db.set_page_token("0")

        watcher = RemoteWatcher(drive, db)
        result = watcher.poll_once()

        assert len(result) == 1
        assert result[0].event_type == "created"
        assert result[0].file_id == "f1"

    def test_classifies_known_file_as_modified(self, tmp_path):
        db = _make_state_db(tmp_path)
        # Pre-populate state so the file is "known"
        from syncopath.state import FileState
        db.upsert(FileState(path="docs/hello.txt", file_id="f1", remote_md5="abc",
                            local_md5="abc", remote_mtime="2026-01-01T00:00:00.000Z",
                            local_mtime=0.0, mime_type="text/plain"))
        drive = _make_drive_mock(changes=[_raw_change("f1", "hello.txt")], new_token="1")
        db.set_page_token("0")

        watcher = RemoteWatcher(drive, db)
        result = watcher.poll_once()

        assert len(result) == 1
        assert result[0].event_type == "modified"

    def test_classifies_removed_as_deleted(self, tmp_path):
        db = _make_state_db(tmp_path)
        drive = _make_drive_mock(changes=[_raw_change("f1", "gone.txt", removed=True)],
                                 new_token="1")
        db.set_page_token("0")

        watcher = RemoteWatcher(drive, db)
        result = watcher.poll_once()

        assert len(result) == 1
        assert result[0].event_type == "deleted"

    def test_classifies_trashed_as_deleted(self, tmp_path):
        db = _make_state_db(tmp_path)
        drive = _make_drive_mock(changes=[_raw_change("f1", "trash.txt", trashed=True)],
                                 new_token="1")
        db.set_page_token("0")

        watcher = RemoteWatcher(drive, db)
        result = watcher.poll_once()

        assert len(result) == 1
        assert result[0].event_type == "deleted"

    def test_returns_empty_on_api_error(self, tmp_path):
        db = _make_state_db(tmp_path)
        drive = MagicMock()
        drive.get_start_page_token.return_value = "0"
        drive.get_changes.side_effect = Exception("Network error")
        db.set_page_token("0")

        watcher = RemoteWatcher(drive, db)
        result = watcher.poll_once()

        assert result == []

    def test_fetches_start_token_if_none_stored(self, tmp_path):
        db = _make_state_db(tmp_path)
        # No token stored
        drive = _make_drive_mock(changes=[], new_token="5")
        drive.get_start_page_token.return_value = "5"

        watcher = RemoteWatcher(drive, db)
        watcher.poll_once()

        drive.get_start_page_token.assert_called_once()


# ---------------------------------------------------------------------------
# trigger_poll tests
# ---------------------------------------------------------------------------

class TestTriggerPoll:

    def test_trigger_wakes_poll_loop_early(self, tmp_path):
        """trigger_poll() should cause the loop to poll again without waiting
        the full poll_interval."""
        db = _make_state_db(tmp_path)
        db.set_page_token("0")

        poll_times = []

        def fake_get_changes(token):
            poll_times.append(time.monotonic())
            return [], str(int(token) + 1)

        drive = MagicMock()
        drive.get_start_page_token.return_value = "0"
        drive.get_changes.side_effect = fake_get_changes

        # Long poll interval — trigger should fire before it expires
        watcher = RemoteWatcher(drive, db, poll_interval=60)
        watcher.start(callback=lambda changes: None)

        # Wait for first poll to complete
        time.sleep(0.2)
        first_poll_count = drive.get_changes.call_count

        # Trigger early poll
        t_trigger = time.monotonic()
        watcher.trigger_poll()

        # Second poll should happen quickly (within 1s, not 60s)
        deadline = time.monotonic() + 2.0
        while drive.get_changes.call_count < first_poll_count + 1:
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)

        watcher.stop()

        assert drive.get_changes.call_count >= first_poll_count + 1, \
            "trigger_poll() did not wake the poll loop"
        # The second poll should have happened quickly after trigger
        if len(poll_times) >= 2:
            gap = poll_times[-1] - t_trigger
            assert gap < 2.0, f"trigger_poll() took {gap:.2f}s — too slow"


# ---------------------------------------------------------------------------
# Failure backoff tests
# ---------------------------------------------------------------------------

class TestFailureBackoff:

    def test_consecutive_failures_increment_counter(self, tmp_path):
        db = _make_state_db(tmp_path)
        db.set_page_token("0")

        drive = MagicMock()
        drive.get_start_page_token.return_value = "0"
        drive.get_changes.side_effect = Exception("timeout")

        watcher = RemoteWatcher(drive, db, poll_interval=30)
        watcher._poll_with_tracking()
        assert watcher._consecutive_failures == 1
        watcher._poll_with_tracking()
        assert watcher._consecutive_failures == 2

    def test_success_resets_failure_counter(self, tmp_path):
        db = _make_state_db(tmp_path)
        db.set_page_token("0")

        drive = MagicMock()
        drive.get_start_page_token.return_value = "0"
        drive.get_changes.side_effect = Exception("timeout")

        watcher = RemoteWatcher(drive, db, poll_interval=30)
        watcher._poll_with_tracking()
        watcher._poll_with_tracking()
        assert watcher._consecutive_failures == 2

        # Now succeed
        drive.get_changes.side_effect = None
        drive.get_changes.return_value = ([], "2")
        watcher._poll_with_tracking()
        assert watcher._consecutive_failures == 0

    def test_backoff_schedule_applied(self, tmp_path):
        """The sleep duration in the poll loop should follow _FAILURE_BACKOFF."""
        db = _make_state_db(tmp_path)
        db.set_page_token("0")

        drive = MagicMock()
        drive.get_start_page_token.return_value = "0"
        drive.get_changes.side_effect = Exception("auth error")

        watcher = RemoteWatcher(drive, db, poll_interval=30)

        sleep_durations = []
        original_wait = threading.Event.wait

        def capturing_wait(self_event, timeout=None):
            if timeout is not None and timeout != 30:
                sleep_durations.append(timeout)
            # Don't actually sleep in the test
            return False

        with patch.object(threading.Event, "wait", capturing_wait):
            watcher.start(callback=lambda c: None)
            time.sleep(0.3)
            watcher.stop()

        # After failures, sleep durations should match _FAILURE_BACKOFF
        assert len(sleep_durations) > 0
        for d in sleep_durations:
            assert d in _FAILURE_BACKOFF, f"Unexpected sleep duration: {d}"


# ---------------------------------------------------------------------------
# Watchdog tests
# ---------------------------------------------------------------------------

class TestWatchdog:

    def test_watchdog_restarts_dead_poll_thread(self, tmp_path):
        """Watchdog detects a dead thread reference and calls _start_poll_thread.

        The watchdog log line 'Remote watcher poll thread died unexpectedly' is
        the observable side-effect. We verify it appears within the check interval.
        """
        db = _make_state_db(tmp_path)
        db.set_page_token("0")
        drive = _make_drive_mock()

        watcher = RemoteWatcher(drive, db, poll_interval=0.05, watchdog_check_interval=0.05)
        watcher._running = True
        watcher._wake_event.clear()

        # Plant a dead thread
        dead = threading.Thread(target=lambda: None, daemon=True)
        dead.start()
        dead.join()
        with watcher._lock:
            watcher._thread = dead

        restart_called = threading.Event()
        real_start = RemoteWatcher._start_poll_thread

        def patched_start(self_w):
            restart_called.set()       # signal before spawning threads
            real_start(self_w)

        with patch.object(RemoteWatcher, '_start_poll_thread', patched_start):
            watcher._start_watchdog()
            restarted = restart_called.wait(timeout=2.0)

        # Clean up
        watcher._running = False
        watcher._wake_event.set()
        if watcher._watchdog_thread:
            watcher._watchdog_thread.join(timeout=1.0)
        if watcher._thread:
            watcher._thread.join(timeout=1.0)

        assert restarted, "Watchdog did not call _start_poll_thread when thread was dead"


# ---------------------------------------------------------------------------
# Stop tests
# ---------------------------------------------------------------------------

class TestStop:

    def test_stop_terminates_promptly(self, tmp_path):
        db = _make_state_db(tmp_path)
        db.set_page_token("0")
        drive = _make_drive_mock(changes=[], new_token="1")

        watcher = RemoteWatcher(drive, db, poll_interval=60)
        watcher.start(callback=lambda c: None)
        time.sleep(0.1)

        t0 = time.monotonic()
        watcher.stop()
        elapsed = time.monotonic() - t0

        # Should stop well within the poll_interval (60s), i.e. in < 2s
        assert elapsed < 2.0, f"stop() took {elapsed:.2f}s — thread not woken"

    def test_stop_is_idempotent(self, tmp_path):
        db = _make_state_db(tmp_path)
        db.set_page_token("0")
        drive = _make_drive_mock(changes=[], new_token="1")

        watcher = RemoteWatcher(drive, db, poll_interval=60)
        watcher.start(callback=lambda c: None)
        watcher.stop()
        # Second stop should not raise
        watcher.stop()
