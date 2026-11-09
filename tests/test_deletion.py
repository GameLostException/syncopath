"""Deletion semantics — one test (or more) per criterion of docs/specs/deletion.md."""
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncopath.local_watcher import LocalChangeEvent
from syncopath.remote_watcher import RemoteWatcher
from syncopath.state import StateDB

PENDING = "pending_delete"
DELETED_ON_DRIVE = "deleted_on_drive"


def synced_env(make_env, safe_mode=True, **overrides):
    """An account whose initial sync has completed, with two files and a folder."""
    env = make_env(safe_mode=safe_mode, **overrides)
    drive = env["drive"]
    env["a"] = drive.add_file("a.txt", content=b"A")
    env["docs"] = drive.add_folder("docs")
    env["b"] = drive.add_file("b.txt", parent_id=env["docs"], content=b"B")
    env["engine"].initial_sync()
    (env["local_root"] / "docs").mkdir(exist_ok=True)
    env["token"] = env["state_db"].get_page_token()
    return env


def delete_locally(env, rel_path, event=True):
    path = env["local_root"] / rel_path
    if path.is_dir():
        import shutil
        shutil.rmtree(path)
    else:
        path.unlink()
    if event:
        env["engine"].handle_local_changes([LocalChangeEvent("deleted", rel_path)])


def apply_remote_changes(env):
    """Feed every Drive change since the stored token through the real classifier."""
    watcher = RemoteWatcher(env["drive"], env["state_db"])
    raw, new_token = env["drive"].get_changes(env["token"])
    env["token"] = new_token
    changes = watcher._parse_changes(raw)
    if changes:
        env["engine"].handle_remote_changes(changes)


def trashed_names(env):
    return sorted(p.name.split("-", 1)[1] for p in env["trash"].iterdir())


def status(env, rel_path):
    row = env["state_db"].get_by_path(rel_path)
    return row.sync_status if row else None


# ─── Local → Drive ──────────────────────────────────────────────────────────

class TestLocalDeletionSafeMode:

    def test_ac1_deletion_is_listed_and_drive_kept(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")

        assert env["drive"]._files[env["a"]].trashed is False
        assert status(env, "a.txt") == PENDING
        assert [p.path for p in env["engine"].pending_local_deletions()] == ["a.txt"]

    def test_ac1_folder_is_listed_once(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "docs")

        assert [p.path for p in env["engine"].pending_local_deletions()] == ["docs"]
        assert status(env, "docs/b.txt") == PENDING

    def test_ac2_flush_moves_to_drive_bin_and_clears_list(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")
        delete_locally(env, "docs")

        env["engine"].flush_local_deletions()

        drive = env["drive"]
        assert drive._files[env["a"]].trashed is True
        assert drive._files[env["docs"]].trashed is True
        assert env["engine"].pending_local_deletions() == []
        assert env["state_db"].get_by_path("a.txt") is None
        assert env["state_db"].get_by_path("docs/b.txt") is None

    def test_ac11_restored_before_flush_is_synced_again(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")
        (env["local_root"] / "a.txt").write_bytes(b"A")
        env["engine"].handle_local_changes([LocalChangeEvent("created", "a.txt")])

        assert status(env, "a.txt") == "synced"
        assert env["engine"].pending_local_deletions() == []

    def test_ac11_restore_found_by_reconcile(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")
        (env["local_root"] / "a.txt").write_bytes(b"A")  # no event seen
        env["engine"].reconcile()

        assert status(env, "a.txt") == "synced"

    def test_ac13_reconcile_does_not_redownload_pending(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")
        env["engine"].reconcile()

        assert not (env["local_root"] / "a.txt").exists()
        assert status(env, "a.txt") == PENDING

    def test_ac16_changed_on_drive_before_flush_comes_back(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")
        env["drive"].simulate_remote_change(env["a"], new_content=b"A2")

        env["engine"].flush_local_deletions()

        assert env["drive"]._files[env["a"]].trashed is False
        assert (env["local_root"] / "a.txt").read_bytes() == b"A2"
        assert env["engine"].pending_local_deletions() == []

    def test_ac16_remote_change_while_pending_restores_it(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")
        env["drive"].simulate_remote_change(env["a"], new_content=b"A2")
        apply_remote_changes(env)

        assert (env["local_root"] / "a.txt").read_bytes() == b"A2"
        assert status(env, "a.txt") == "synced"

    def test_ac19_not_owned_is_listed_as_stop_syncing(self, make_env):
        env = make_env(include_shared_with_me=True)
        f = env["drive"].add_file("theirs.txt", content=b"T", owned=False)
        env["engine"].initial_sync()
        env["token"] = env["state_db"].get_page_token()
        rel = "Shared with me/theirs.txt"
        assert (env["local_root"] / rel).exists()

        delete_locally(env, rel)
        [item] = env["engine"].pending_local_deletions()
        assert item.owned is False

        env["engine"].flush_local_deletions()
        assert env["drive"]._files[f].trashed is False
        assert env["state_db"].get_by_path(rel) is None


class TestLocalDeletionUnsafeMode:

    def test_ac3_file_goes_to_drive_bin(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        delete_locally(env, "a.txt")

        assert env["drive"]._files[env["a"]].trashed is True
        assert env["state_db"].get_by_path("a.txt") is None

    def test_ac3_folder_goes_to_drive_bin_as_one_item(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        trashed = []
        real = env["drive"].delete_file
        env["drive"].delete_file = lambda fid: (trashed.append(fid), real(fid))
        delete_locally(env, "docs")

        assert trashed == [env["docs"]]
        assert env["state_db"].get_by_path("docs/b.txt") is None

    def test_ac16_changed_on_drive_is_downloaded_not_trashed(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        env["drive"].simulate_remote_change(env["a"], new_content=b"A2")
        delete_locally(env, "a.txt")

        assert env["drive"]._files[env["a"]].trashed is False
        assert (env["local_root"] / "a.txt").read_bytes() == b"A2"

    def test_ac19_not_owned_stops_syncing(self, make_env):
        env = make_env(safe_mode=False, include_shared_with_me=True)
        f = env["drive"].add_file("theirs.txt", content=b"T", owned=False)
        env["engine"].initial_sync()
        delete_locally(env, "Shared with me/theirs.txt")

        assert env["drive"]._files[f].trashed is False
        assert env["state_db"].get_by_path("Shared with me/theirs.txt") is None

    def test_ac19_drive_refusal_stops_syncing(self, make_env):
        """A file someone else owns inside your own folder: Drive refuses the bin."""
        env = synced_env(make_env, safe_mode=False)
        env["drive"]._files[env["a"]].owned_by_me = False
        delete_locally(env, "a.txt")

        assert env["drive"]._files[env["a"]].trashed is False
        assert env["state_db"].get_by_path("a.txt") is None


# ─── Drive → local ──────────────────────────────────────────────────────────

class TestDriveDeletionSafeMode:

    def test_ac4_local_kept_and_marked(self, make_env):
        env = synced_env(make_env)
        overlay = env["engine"]._overlay = MagicMock()
        env["drive"].simulate_remote_trash(env["a"])
        apply_remote_changes(env)

        assert (env["local_root"] / "a.txt").exists()
        assert status(env, "a.txt") == DELETED_ON_DRIVE
        overlay.set_file_deleted_on_drive.assert_any_call("a.txt")

    def test_ac4_folder_marks_everything_inside(self, make_env):
        env = synced_env(make_env)
        env["drive"].simulate_remote_trash(env["docs"])
        apply_remote_changes(env)

        assert status(env, "docs") == DELETED_ON_DRIVE
        assert status(env, "docs/b.txt") == DELETED_ON_DRIVE
        assert env["engine"].deleted_on_drive() == ["docs"]

    def test_ac5_flush_moves_marked_files_to_desktop_trash(self, make_env):
        env = synced_env(make_env)
        env["drive"].simulate_remote_trash(env["a"])
        apply_remote_changes(env)

        env["engine"].flush_drive_deletions()

        assert not (env["local_root"] / "a.txt").exists()
        assert trashed_names(env) == ["a.txt"]
        assert env["engine"].deleted_on_drive() == []

    def test_ac11_restored_on_drive_clears_mark(self, make_env):
        env = synced_env(make_env)
        env["drive"].simulate_remote_trash(env["a"])
        apply_remote_changes(env)
        env["drive"].simulate_remote_untrash(env["a"])
        apply_remote_changes(env)

        assert status(env, "a.txt") == "synced"

    def test_ac11_restored_folder_found_by_reconcile(self, make_env):
        env = synced_env(make_env)
        env["drive"].simulate_remote_trash(env["docs"])
        apply_remote_changes(env)
        env["drive"]._files[env["docs"]].trashed = False  # restored, child not reported

        env["engine"].reconcile()
        assert status(env, "docs/b.txt") == "synced"

    def test_ac17_local_edit_is_kept_and_uploaded_as_new(self, make_env):
        env = synced_env(make_env)
        (env["local_root"] / "a.txt").write_bytes(b"edited here")
        env["drive"].simulate_remote_trash(env["a"])
        apply_remote_changes(env)

        row = env["state_db"].get_by_path("a.txt")
        assert row.file_id != env["a"] and row.sync_status == "synced"
        assert env["drive"]._files[row.file_id].content == b"edited here"
        assert env["drive"]._files[row.file_id].trashed is False


class TestDriveDeletionUnsafeMode:

    def test_ac6_ac7_local_copy_goes_to_desktop_trash(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        env["drive"].simulate_remote_trash(env["a"])
        apply_remote_changes(env)

        assert not (env["local_root"] / "a.txt").exists()
        assert trashed_names(env) == ["a.txt"]
        assert env["state_db"].get_by_path("a.txt") is None

    def test_ac6_folder_goes_to_desktop_trash_as_one_item(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        env["drive"].simulate_remote_trash(env["docs"])
        apply_remote_changes(env)

        assert trashed_names(env) == ["docs"]
        assert env["state_db"].get_by_path("docs/b.txt") is None

    def test_ac17_local_edit_is_kept_and_uploaded_as_new(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        (env["local_root"] / "a.txt").write_bytes(b"edited here")
        env["drive"].simulate_remote_trash(env["a"])
        apply_remote_changes(env)

        assert (env["local_root"] / "a.txt").read_bytes() == b"edited here"
        row = env["state_db"].get_by_path("a.txt")
        assert row.file_id != env["a"]
        assert trashed_names(env) == []


# ─── Both directions ────────────────────────────────────────────────────────

class TestGuards:

    def test_ac10_nothing_propagates_before_initial_sync(self, make_env):
        env = make_env(safe_mode=False)
        drive, state_db = env["drive"], env["state_db"]
        f = drive.add_file("a.txt", content=b"A")
        state_db.upsert(__import__("syncopath.state", fromlist=["FileState"]).FileState(
            path="a.txt", file_id=f, remote_md5=None, local_md5=None, remote_mtime="",
            local_mtime=0, mime_type="text/plain", sync_status="synced"))
        (env["local_root"] / "a.txt").write_bytes(b"A")
        assert state_db.get_page_token() is None

        env["engine"].handle_local_changes([LocalChangeEvent("deleted", "a.txt")])
        drive.simulate_remote_trash(f)
        env["engine"].handle_remote_changes(
            RemoteWatcher(drive, state_db)._parse_changes(drive.get_changes("0")[0]))

        assert drive._files[f].trashed is True  # only by our simulation
        assert (env["local_root"] / "a.txt").exists()
        assert trashed_names(env) == []
        assert state_db.get_by_path("a.txt") is not None

    def test_ac12_marks_survive_a_restart(self, make_env, tmp_path):
        env = synced_env(make_env)
        delete_locally(env, "a.txt")
        env["drive"].simulate_remote_trash(env["b"])
        apply_remote_changes(env)
        env["state_db"].close()

        from syncopath.sync_engine import SyncEngine
        with patch("syncopath.state.STATE_DIR", tmp_path / "state"):
            reopened = StateDB(env["config"].name)
        engine = SyncEngine(env["config"], env["drive"], reopened, safe_mode=True)
        try:
            assert [p.path for p in engine.pending_local_deletions()] == ["a.txt"]
            assert engine.deleted_on_drive() == ["docs/b.txt"]
        finally:
            reopened.close()
            env["state_db"] = StateDB.__new__(StateDB)  # already closed; fixture close is a no-op
            env["state_db"]._lock = threading.RLock()
            env["state_db"]._conn = MagicMock()

    def test_ac14_empty_folder_propagates_nothing(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        errors = []
        env["engine"].set_status_callback(lambda s, d="": errors.append(d) if s == "error" else None)
        delete_locally(env, "a.txt", event=False)
        delete_locally(env, "docs", event=False)  # folder now empty: looks unmounted

        env["engine"].reconcile()

        assert env["drive"]._files[env["a"]].trashed is False
        assert env["drive"]._files[env["b"]].trashed is False
        assert errors, "an error must be reported"

    def test_ac15_missing_at_startup_is_a_local_deletion(self, make_env):
        env = synced_env(make_env)
        delete_locally(env, "a.txt", event=False)  # deleted while SyncoPath was off

        env["engine"].reconcile()

        assert status(env, "a.txt") == PENDING
        assert not (env["local_root"] / "a.txt").exists()

    def test_ac15_missing_at_startup_unsafe_goes_to_drive_bin(self, make_env):
        env = synced_env(make_env, safe_mode=False)
        delete_locally(env, "a.txt", event=False)

        env["engine"].reconcile()

        assert env["drive"]._files[env["a"]].trashed is True


class TestNoPermanentDeletion:

    def test_ac8_drive_deletion_uses_the_bin(self):
        from syncopath.drive_api import DriveAPI
        api = DriveAPI("t", "id", "secret")
        api._service = MagicMock()
        api.delete_file("f1")
        files = api._service.files.return_value
        files.update.assert_called_once()
        assert files.update.call_args.kwargs["body"] == {"trashed": True}
        files.delete.assert_not_called()

    def test_ac7_ac9_source_never_deletes_permanently_or_empties_a_bin(self):
        src = "\n".join(p.read_text() for p in Path("syncopath").glob("*.py"))
        for forbidden in ("emptyTrash", "files().delete(", "expunge", "Trash/files"):
            assert forbidden not in src, forbidden

    def test_ac7_preferences_issue_delete_uses_trash(self, tmp_path):
        from syncopath import preferences
        local = tmp_path / "drive"
        local.mkdir()
        (local / "orphan.txt").write_text("x")
        acc = MagicMock(local_path=local)
        acc.name = "acc"
        trashed = []
        fake_self = MagicMock()
        with patch.object(preferences.AppConfig, "load", return_value=MagicMock(accounts=[acc])), \
             patch("syncopath.state.StateDB"), \
             patch.object(preferences, "move_to_trash", side_effect=trashed.append):
            preferences.PreferencesWindow._delete_issue_file(
                fake_self, {"path": "orphan.txt"}, {"name": "acc"})
        assert trashed == [local / "orphan.txt"]


# ─── Tray and confirmation (daemon) ─────────────────────────────────────────

def _daemon(engines):
    import syncopath.__main__ as main_mod
    daemon = main_mod.SyncoPathDaemon.__new__(main_mod.SyncoPathDaemon)
    daemon.tray = MagicMock()
    daemon.accounts = []
    for name, engine in engines:
        cfg = MagicMock()
        cfg.name = name
        daemon.accounts.append({"config": cfg, "engine": engine, "worker": MagicMock()})
    return daemon


def _engine(pending=0, deleted=0):
    engine = MagicMock()
    engine.pending_local_deletions.return_value = [MagicMock(path=f"p{i}") for i in range(pending)]
    engine.deleted_on_drive.return_value = [f"d{i}" for i in range(deleted)]
    return engine


class TestTrayAndConfirmation:

    def test_ac18_tray_shows_counts_across_accounts(self):
        daemon = _daemon([("a", _engine(pending=2, deleted=1)), ("b", _engine(pending=1))])
        daemon._refresh_deletion_counts()
        daemon.tray.update_deletions.assert_called_with(3, 1)

    def test_ac20_cancel_sends_nothing(self):
        engine = _engine(pending=2)
        daemon = _daemon([("a", engine)])
        daemon._confirm = MagicMock(return_value=False)
        daemon._send_local_deletions()
        assert "2" in daemon._confirm.call_args.args[0]
        daemon.accounts[0]["worker"].enqueue_task.assert_not_called()

    def test_ac20_confirm_sends_all(self):
        engine = _engine(pending=2)
        daemon = _daemon([("a", engine)])
        daemon._confirm = MagicMock(return_value=True)
        daemon._send_local_deletions()
        daemon.accounts[0]["worker"].enqueue_task.assert_called_once_with(engine.flush_local_deletions)

    def test_ac5_drive_flush_needs_confirmation(self):
        engine = _engine(deleted=3)
        daemon = _daemon([("a", engine)])
        daemon._confirm = MagicMock(return_value=False)
        daemon._trash_drive_deletions()
        assert "3" in daemon._confirm.call_args.args[0]
        daemon.accounts[0]["worker"].enqueue_task.assert_not_called()
        daemon._confirm.return_value = True
        daemon._trash_drive_deletions()
        daemon.accounts[0]["worker"].enqueue_task.assert_called_once_with(engine.flush_drive_deletions)


class TestDeletingEverything:

    def test_deleting_the_last_files_live_is_a_real_deletion(self, make_env):
        """An empty folder after live delete events is the user's choice, not an unmount."""
        env = synced_env(make_env, safe_mode=False)
        delete_locally(env, "a.txt")
        delete_locally(env, "docs")

        assert env["drive"]._files[env["a"]].trashed is True
        assert env["drive"]._files[env["docs"]].trashed is True
