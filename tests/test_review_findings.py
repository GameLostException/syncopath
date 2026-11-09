"""Regression tests for the findings of the 2026-11-09 code review.

Each test reproduces one reported symptom. They were written failing, before the fix.
"""
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncopath.drive_api import DriveAPI
from syncopath.local_watcher import LocalChangeEvent, LocalWatcher
from syncopath.remote_watcher import RemoteChange
from syncopath.state import FileState
from syncopath.sync_engine import SyncEngine
from syncopath.sync_worker import SyncWorker

RESET = ConnectionResetError(104, "Connection reset by peer")


def fail_first(obj, name, times=1, exc=RESET):
    """Make obj.name raise `exc` on its first `times` calls. Returns a mutable control dict."""
    real = getattr(obj, name)
    ctl = {"calls": 0, "times": times}

    def wrapper(*args, **kwargs):
        ctl["calls"] += 1
        if ctl["calls"] <= ctl["times"]:
            raise exc
        return real(*args, **kwargs)

    setattr(obj, name, wrapper)
    return ctl


def remote_changes_since(env, token):
    changes, _ = env["drive"].get_changes(token)
    state_db = env["state_db"]
    return [RemoteChange(c["fileId"],
                         "modified" if state_db.get_by_file_id(c["fileId"]) else "created",
                         c.get("file"))
            for c in changes if not c.get("removed")]


def run_worker(env, enqueue, until, timeout=5.0):
    """Run a real SyncWorker (zero backoff) on the engine until `until()` holds."""
    engine = env["engine"]
    worker = SyncWorker()
    worker._backoff_schedule = [0]
    worker.set_handlers(on_local_changes=engine.handle_local_changes,
                        on_remote_changes=engine.handle_remote_changes,
                        on_reconcile=engine.reconcile)
    worker.start()
    try:
        enqueue(worker)
        deadline = time.time() + timeout
        while time.time() < deadline and not until():
            time.sleep(0.05)
        return until()
    finally:
        worker.stop(timeout=2)


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        time.sleep(0.05)
    return predicate()


# ─── Finding 1–3: failed changes are retried, not dropped ──────────────────

class TestFailedChangesAreRetried:

    def test_remote_change_survives_transient_metadata_error(self, setup):
        env = setup
        drive, root = env["drive"], env["local_root"]
        folder = drive.add_folder("docs")
        env["engine"].initial_sync()
        token = env["state_db"].get_page_token()

        file_id = drive.add_file("new.txt", parent_id=folder, content=b"hello")
        drive.simulate_remote_change(file_id)
        changes = remote_changes_since(env, token)
        fail_first(drive, "get_file_metadata")

        assert run_worker(env, lambda w: w.enqueue_remote(changes),
                          until=lambda: (root / "docs" / "new.txt").exists())

    def test_local_upload_survives_transient_error(self, setup):
        env = setup
        drive, root = env["drive"], env["local_root"]
        file_id = drive.add_file("a.txt", content=b"v1")
        env["engine"].initial_sync()
        (root / "a.txt").write_bytes(b"v2")
        fail_first(drive, "upload_file")

        assert run_worker(env,
                          lambda w: w.enqueue_local([LocalChangeEvent("modified", "a.txt")]),
                          until=lambda: drive._files[file_id].content == b"v2")

    def test_reconcile_retries_an_upload_that_failed(self, setup):
        env = setup
        drive, root = env["drive"], env["local_root"]
        file_id = drive.add_file("a.txt", content=b"v1")
        env["engine"].initial_sync()
        (root / "a.txt").write_bytes(b"v2")

        ctl = fail_first(drive, "upload_file", times=1000)
        try:
            env["engine"].handle_local_changes([LocalChangeEvent("modified", "a.txt")])
        except Exception:
            pass  # the failure may surface; what matters is that it is not forgotten
        ctl["times"] = 0  # network is back

        env["engine"].reconcile()
        assert drive._files[file_id].content == b"v2"

    def test_connection_reset_is_not_permanently_unsyncable(self, tmp_path):
        assert SyncEngine._is_upload_unsyncable(RESET, tmp_path / "x.txt") is False


# ─── Finding 4: "local wins" must reach Drive ──────────────────────────────

class TestConflictLocalWins:

    def test_local_wins_uploads_the_local_version(self, setup):
        env = setup
        drive, root, state_db = env["drive"], env["local_root"], env["state_db"]
        file_id = drive.add_file("mine.txt", content=b"base")
        env["engine"].initial_sync()
        token = state_db.get_page_token()

        (root / "mine.txt").write_bytes(b"my newer edit")
        f = drive._files[file_id]
        f.content, f.md5, f.modified_time = b"old remote", "different", "2020-01-01T00:00:00.000Z"
        drive._changes.append({"fileId": file_id, "removed": False, "file": drive._file_to_meta(f)})

        env["engine"].handle_remote_changes(remote_changes_since(env, token))

        assert (root / "mine.txt").read_bytes() == b"my newer edit"
        assert drive._files[file_id].content == b"my newer edit"


# ─── Finding 5: page token of the initial sync ────────────────────────────

class TestInitialSyncPageToken:

    def test_remote_edit_during_initial_sync_is_not_skipped(self, setup):
        env = setup
        drive = env["drive"]
        a = drive.add_file("a.txt", content=b"v1")
        b = drive.add_file("b.txt", content=b"b")

        real_download = drive.download_file

        def download_then_edit_a(file_id, mime_type, dest):
            real_download(file_id, mime_type, dest)
            if file_id == b:  # a was already downloaded; now it changes remotely
                drive.simulate_remote_change(a, new_content=b"v2")

        drive.download_file = download_then_edit_a
        env["engine"].initial_sync()

        changes, _ = drive.get_changes(env["state_db"].get_page_token())
        assert a in [c["fileId"] for c in changes]


# ─── Finding 6: wrong local or remote location ────────────────────────────

class TestPathResolution:

    def test_unresolvable_parent_is_not_placed_at_root(self, make_env):
        env = make_env(include_shared_with_me=True)  # skips the ownership walk
        drive, root = env["drive"], env["local_root"]
        folder = drive.add_folder("docs")  # not indexed in the state DB
        file_id = drive.add_file("x.txt", parent_id=folder, content=b"x")
        meta = drive.get_file_metadata(file_id)
        drive.get_file_metadata = MagicMock(side_effect=RESET)

        try:
            env["engine"].handle_remote_changes([RemoteChange(file_id, "created", meta)])
        except Exception:
            pass

        assert not (root / "x.txt").exists()

    def test_repeated_folder_name_uploads_into_the_right_folder(self, setup):
        env = setup
        drive, root = env["drive"], env["local_root"]
        a = drive.add_folder("a")
        b = drive.add_folder("b", parent_id=a)
        inner_a = drive.add_folder("a", parent_id=b)
        env["engine"].initial_sync()

        (root / "a" / "b" / "a").mkdir(parents=True)
        (root / "a" / "b" / "a" / "f.txt").write_bytes(b"f")
        env["engine"].handle_local_changes([LocalChangeEvent("created", "a/b/a/f.txt")])

        uploaded = [f for f in drive._files.values() if f.name == "f.txt"]
        assert len(uploaded) == 1
        assert uploaded[0].parents == [inner_a]


# ─── Finding 8: local folder move ─────────────────────────────────────────

class TestLocalFolderMove:

    def test_moving_a_folder_moves_its_children_in_state(self, setup):
        env = setup
        drive, root, state_db = env["drive"], env["local_root"], env["state_db"]
        folder = drive.add_folder("old")
        file_id = drive.add_file("f.txt", parent_id=folder, content=b"x")
        env["engine"].initial_sync()

        (root / "old").rename(root / "new")
        env["engine"].handle_local_changes([LocalChangeEvent("moved", "old", "new")])

        moved = state_db.get_by_path("new/f.txt")
        assert moved is not None and moved.file_id == file_id
        assert state_db.get_by_path("old/f.txt") is None


# ─── Finding 9: Drive API lock covers the whole download ─────────────────

def _fake_drive_api():
    api = DriveAPI("test", "id", "secret")
    api._service = MagicMock()
    return api


class TestDriveApiConcurrency:

    def test_download_streams_while_holding_the_api_lock(self, tmp_path):
        api = _fake_drive_api()
        seen = []

        class FakeDownload:
            def __init__(self, fh, request, **kwargs):
                self.fh = fh

            def next_chunk(self):
                seen.append(api._api_lock.locked())
                self.fh.write(b"data")
                return None, True

        with patch("syncopath.drive_api.MediaIoBaseDownload", FakeDownload):
            api.download_file("id1", "text/plain", tmp_path / "f.txt")

        assert seen == [True]
        assert (tmp_path / "f.txt").read_bytes() == b"data"


# ─── Finding 14: bandwidth limit ──────────────────────────────────────────

class TestBandwidthLimit:

    def test_throttle_paces_to_the_limit(self):
        from syncopath.drive_api import Throttle
        now = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s

        t = Throttle(100, clock=lambda: now[0], sleep=sleep)  # 100 KiB/s
        t.wait(200 * 1024)  # 200 KiB sent instantly → must wait ~2 s
        assert sum(slept) == pytest.approx(2.0, abs=0.01)

    def test_limited_download_uses_small_chunks_and_waits(self, tmp_path):
        api = _fake_drive_api()
        api.bandwidth_limit_kib = 100
        chunk_sizes, waited = [], []

        class FakeDownload:
            def __init__(self, fh, request, chunksize=None, **kwargs):
                chunk_sizes.append(chunksize)
                self.fh, self.n = fh, 0

            def next_chunk(self):
                self.n += 1
                self.fh.write(b"x" * 1024)
                status = MagicMock(resumable_progress=self.n * 1024)
                return status, self.n == 3

        with patch("syncopath.drive_api.MediaIoBaseDownload", FakeDownload), \
             patch("syncopath.drive_api.Throttle.wait", lambda self, b: waited.append(b)):
            api.download_file("id1", "text/plain", tmp_path / "f.txt")

        assert chunk_sizes[0] is not None and chunk_sizes[0] <= 1024 * 1024
        assert waited == [1024, 2048, 3072]

    def test_limited_upload_is_chunked_and_waits(self, tmp_path):
        api = _fake_drive_api()
        api.bandwidth_limit_kib = 100
        src = tmp_path / "up.bin"
        src.write_bytes(b"x" * 10)
        waited = []

        request = MagicMock()
        request.next_chunk.side_effect = [
            (MagicMock(resumable_progress=5), None),
            (None, {"id": "f1", "md5Checksum": "m", "modifiedTime": "t"}),
        ]
        api._service.files.return_value.create.return_value = request

        with patch("syncopath.drive_api.Throttle.wait", lambda self, b: waited.append(b)):
            result = api.upload_file(src, "parent")

        assert result["id"] == "f1"
        assert waited == [5]


# ─── Finding 10: one SQLite connection shared by threads ──────────────────

class TestStateDbThreads:

    def test_concurrent_writers_lose_nothing(self, setup):
        state_db = setup["state_db"]
        errors = []

        def writer(n):
            try:
                for i in range(200):
                    state_db.upsert(FileState(
                        path=f"t{n}/f{i}", file_id=f"id-{n}-{i}", remote_md5=None,
                        local_md5=None, remote_mtime="", local_mtime=0,
                        mime_type="x", sync_status="synced"))
                    state_db.set_page_token(str(i))
                    state_db.get_all_paths_with_status()
            except Exception as e:  # noqa: BLE001 — any error is the symptom
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(state_db.get_all()) == 8 * 200


# ─── Findings 11–12: local watcher ────────────────────────────────────────

class TestLocalWatcher:

    def test_restart_after_stop(self, tmp_path):
        got = []
        lw = LocalWatcher(tmp_path, [], debounce=0.1)
        lw.start(got.extend)
        lw.stop()
        lw.start(got.extend)
        try:
            (tmp_path / "f.txt").write_text("x")
            assert wait_for(lambda: any(e.path == "f.txt" for e in got))
        finally:
            lw.stop()

    def test_continuous_events_still_flush(self, tmp_path):
        got = []
        lw = LocalWatcher(tmp_path, [], debounce=0.3, max_delay=1.0)
        lw._callback = got.append
        start = time.time()
        while time.time() - start < 2.5:
            lw._on_event(LocalChangeEvent("modified", f"busy{int(time.time() * 10)}.log"))
            time.sleep(0.1)
            if got:
                break
        lw.stop()
        assert got, "a steady stream of events must not postpone the flush forever"


# ─── Finding 11: pause stops the worker too ──────────────────────────────

class TestWorkerPause:

    def test_paused_worker_holds_events_until_resumed(self):
        results = []
        worker = SyncWorker()
        worker.set_handlers(on_local_changes=results.extend)
        worker.start()
        try:
            worker.pause()
            worker.enqueue_local(["a"])
            time.sleep(0.3)
            assert results == []
            worker.resume()
            assert wait_for(lambda: results == ["a"])
        finally:
            worker.stop(timeout=2)


# ─── Finding 15: a failed initial sync is retried ────────────────────────

class TestStartupRetry:

    def test_failed_initial_sync_is_retried(self, tmp_path):
        import syncopath.__main__ as main_mod

        daemon = main_mod.SyncoPathDaemon.__new__(main_mod.SyncoPathDaemon)
        daemon.tray = MagicMock()
        daemon._overlays, daemon._workers = [], []
        daemon._running = True
        daemon._stop = threading.Event()

        engine = MagicMock()
        engine.initial_sync.side_effect = [RESET, None]
        state_db = MagicMock()
        state_db.get_page_token.return_value = None
        acc = {"config": MagicMock(name="cfg", local_path=tmp_path), "engine": engine,
               "state_db": state_db, "local_watcher": MagicMock(),
               "remote_watcher": MagicMock()}
        acc["config"].name = "acc"
        daemon.accounts = [acc]

        with patch.object(main_mod, "_INITIAL_SYNC_BACKOFF", [0]):
            daemon._start_sync()
        try:
            assert engine.initial_sync.call_count == 2
            assert "worker" in acc
        finally:
            daemon._running = False
            daemon._stop.set()
            for w in daemon._workers:
                w.stop(timeout=2)


class TestWorkerOrdering:

    def test_equal_priority_events_run_in_arrival_order(self):
        results = []
        worker = SyncWorker()
        worker.set_handlers(on_local_changes=results.extend)
        worker.pause()  # let the queue fill up first
        worker.start()
        try:
            for i in range(20):
                worker.enqueue_local([i])
            worker.resume()
            assert wait_for(lambda: len(results) == 20)
            assert results == list(range(20))
        finally:
            worker.stop(timeout=2)
