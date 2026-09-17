"""Integration tests for SyncoPath sync engine using MockDriveAPI."""
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from syncopath.config import AccountConfig
from syncopath.state import StateDB, FileState
from syncopath.sync_engine import SyncEngine, md5_file
from syncopath.remote_watcher import RemoteChange
from syncopath.local_watcher import LocalChangeEvent
from tests.mock_drive import MockDriveAPI


@pytest.fixture
def setup(tmp_path):
    """Create a full test environment with mock Drive."""
    local_root = tmp_path / "gdrive"
    local_root.mkdir()

    config = AccountConfig(
        name="test",
        local_path=local_root,
        remote_id="root",
        poll_interval=30,
        debounce=1,
        include_shared_with_me=False,
    )

    with patch("syncopath.state.STATE_DIR", tmp_path / "state"), \
         patch("syncopath.sync_engine.CONFIG_DIR", tmp_path / "config"):
        (tmp_path / "state").mkdir()
        (tmp_path / "config").mkdir()
        state_db = StateDB("test")
        drive = MockDriveAPI()
        engine = SyncEngine(config, drive, state_db)
        yield {
            "engine": engine,
            "drive": drive,
            "state_db": state_db,
            "local_root": local_root,
            "config": config,
        }


class TestFullSyncCycle:
    """Test: create remote file → detect change → download → verify local."""

    def test_initial_sync_downloads_files(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Add files on remote
        drive.add_file("readme.txt", content=b"Hello World")
        folder_id = drive.add_folder("docs")
        drive.add_file("notes.md", parent_id=folder_id, content=b"# Notes")

        # Run initial sync
        engine.initial_sync()

        # Verify local files exist
        assert (local_root / "readme.txt").exists()
        assert (local_root / "readme.txt").read_bytes() == b"Hello World"
        assert (local_root / "docs" / "notes.md").exists()
        assert (local_root / "docs" / "notes.md").read_bytes() == b"# Notes"

        # Verify state DB
        state = state_db.get_by_path("readme.txt")
        assert state is not None
        assert state.sync_status == "synced"

    def test_remote_change_downloads_update(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Initial file
        file_id = drive.add_file("data.txt", content=b"version 1")
        engine.initial_sync()
        assert (local_root / "data.txt").read_bytes() == b"version 1"

        # Simulate remote modification
        drive.simulate_remote_change(file_id, new_content=b"version 2")

        # Process the change
        changes, _ = drive.get_changes(state_db.get_page_token())
        remote_changes = [
            RemoteChange(c["fileId"], "modified", c.get("file"))
            for c in changes if not c.get("removed")
        ]
        engine.handle_remote_changes(remote_changes)

        # Verify updated
        assert (local_root / "data.txt").read_bytes() == b"version 2"

    def test_local_change_uploads(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Initial sync
        file_id = drive.add_file("upload_test.txt", content=b"original")
        engine.initial_sync()

        # Modify locally
        (local_root / "upload_test.txt").write_bytes(b"modified locally")

        # Process local change
        events = [LocalChangeEvent("modified", "upload_test.txt")]
        engine.handle_local_changes(events)

        # Verify uploaded to Drive
        remote_file = drive._files[file_id]
        assert remote_file.content == b"modified locally"

    def test_new_local_file_uploads(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Initial sync (empty)
        engine.initial_sync()

        # Create a new local file
        (local_root / "new_file.txt").write_bytes(b"brand new")

        # Process local creation
        events = [LocalChangeEvent("created", "new_file.txt")]
        engine.handle_local_changes(events)

        # Verify it was uploaded
        uploaded = [f for f in drive._files.values()
                    if f.name == "new_file.txt" and not f.trashed]
        assert len(uploaded) == 1
        assert uploaded[0].content == b"brand new"


class TestConflictResolution:
    """Test: modify both sides → verify .conflict file created."""

    def test_remote_wins_creates_conflict_file(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Initial file
        file_id = drive.add_file("shared.txt", content=b"base version")
        engine.initial_sync()

        # Modify locally (older mtime)
        local_file = local_root / "shared.txt"
        local_file.write_bytes(b"local edit")
        # Backdate local mtime to be older than remote
        import os
        os.utime(local_file, (time.time() - 100, time.time() - 100))

        # Simulate remote modification (newer)
        time.sleep(0.1)
        drive.simulate_remote_change(file_id, new_content=b"remote edit")

        # Process remote change
        changes, _ = drive.get_changes(state_db.get_page_token())
        remote_changes = [
            RemoteChange(c["fileId"], "modified", c.get("file"))
            for c in changes if not c.get("removed")
        ]
        engine.handle_remote_changes(remote_changes)

        # Remote wins — local saved as .conflict
        assert local_file.read_bytes() == b"remote edit"
        conflict_files = list(local_root.glob("shared.conflict*"))
        assert len(conflict_files) == 1
        assert conflict_files[0].read_bytes() == b"local edit"

    def test_local_wins_keeps_local(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Initial file
        file_id = drive.add_file("mine.txt", content=b"base")
        engine.initial_sync()

        # Modify locally (newer — touch to ensure recent mtime)
        local_file = local_root / "mine.txt"
        local_file.write_bytes(b"my newer edit")

        # Simulate remote modification with OLDER timestamp
        drive._files[file_id].content = b"old remote edit"
        drive._files[file_id].md5 = "different"
        drive._files[file_id].modified_time = "2020-01-01T00:00:00.000Z"
        drive._changes.append({
            "fileId": file_id,
            "removed": False,
            "file": drive._file_to_meta(drive._files[file_id]),
        })

        # Process remote change
        changes, _ = drive.get_changes(state_db.get_page_token())
        remote_changes = [
            RemoteChange(c["fileId"], "modified", c.get("file"))
            for c in changes if not c.get("removed")
        ]
        engine.handle_remote_changes(remote_changes)

        # Local wins — file unchanged
        assert local_file.read_bytes() == b"my newer edit"
        # No conflict file created
        conflict_files = list(local_root.glob("mine.conflict*"))
        assert len(conflict_files) == 0


class TestFolderRename:
    """Test: rename remote folder → verify local rename + children paths."""

    def test_folder_rename_propagates(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Create folder with files
        folder_id = drive.add_folder("Old Name")
        drive.add_file("doc.pdf", parent_id=folder_id, content=b"pdf content")
        engine.initial_sync()

        assert (local_root / "Old Name" / "doc.pdf").exists()

        # Rename folder on remote
        drive.simulate_remote_change(folder_id, new_name="New Name")
        drive._files[folder_id].name = "New Name"

        # Process folder change
        meta = drive.get_file_metadata(folder_id)
        change = RemoteChange(folder_id, "modified", meta)
        engine.handle_remote_changes([change])

        # Verify local rename
        assert not (local_root / "Old Name").exists()
        assert (local_root / "New Name" / "doc.pdf").exists()
        assert (local_root / "New Name" / "doc.pdf").read_bytes() == b"pdf content"

        # Verify state DB updated
        assert state_db.get_by_path("Old Name/doc.pdf") is None
        state = state_db.get_by_path("New Name/doc.pdf")
        assert state is not None


class TestSharedWithMeFiltering:
    """Test: shared files filtered when setting is off."""

    def test_shared_files_excluded_from_initial_sync(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Add owned and shared files
        drive.add_file("my_file.txt", content=b"mine")
        drive.add_file("shared_file.txt", content=b"theirs", owned=False)

        engine.initial_sync()

        # Only owned file should be downloaded
        assert (local_root / "my_file.txt").exists()
        assert not (local_root / "shared_file.txt").exists()

    def test_shared_files_included_when_enabled(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Enable shared-with-me
        setup["config"].include_shared_with_me = True

        drive.add_file("my_file.txt", content=b"mine")
        drive.add_file("shared_file.txt", content=b"theirs", owned=False)

        engine.initial_sync()

        assert (local_root / "my_file.txt").exists()
        assert (local_root / "Shared with me" / "shared_file.txt").exists()

    def test_file_in_shared_folder_excluded_via_changes_api(self, setup):
        """Test: file with ownedByMe=True inside a shared folder is excluded."""
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Create an owned file first so we have a valid page token
        drive.add_file("my_file.txt", content=b"mine")
        engine.initial_sync()
        token = state_db.get_page_token()

        # Now simulate a new file appearing via Changes API that is in a shared folder
        # The file itself has ownedByMe=True (you uploaded it), but the folder is shared
        shared_folder_id = drive.add_folder("Shared Project", owned=False)
        # Add file owned by me but inside a shared folder
        file_id = drive.add_file(
            "my_upload.txt",
            parent_id=shared_folder_id,
            content=b"I uploaded this to a shared folder",
            owned=True  # File is owned, but folder isn't
        )

        # Manually create a change event (simulating Changes API returning it)
        drive._changes.append({
            "fileId": file_id,
            "removed": False,
            "file": drive._file_to_meta(drive._files[file_id]),
        })

        # Process the change
        changes, _ = drive.get_changes(token)
        from syncopath.remote_watcher import RemoteChange
        remote_changes = [
            RemoteChange(c["fileId"], "created", c.get("file"))
            for c in changes if not c.get("removed")
        ]
        engine.handle_remote_changes(remote_changes)

        # File should NOT be downloaded (parent folder is shared)
        assert not (local_root / "Shared Project").exists()
        assert not (local_root / "Shared Project" / "my_upload.txt").exists()

    def test_shared_folder_in_state_db_still_excluded(self, setup):
        """Regression test: shared folder in state DB should NOT bypass ownership check.

        Bug scenario (fixed in FIX-18):
        1. A shared folder gets indexed into state DB (e.g., via previous file sync attempt)
        2. A new file appears in that shared folder via Changes API
        3. OLD BUG: _is_shared_file() saw folder in state DB and returned False (not shared)
        4. FIX: Always verify ownership via API, never trust state DB for ownership info
        """
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Create an owned file first so we have a valid page token
        drive.add_file("my_file.txt", content=b"mine")
        engine.initial_sync()

        # Create a shared folder with a file inside
        shared_folder_id = drive.add_folder("External Team Folder", owned=False)
        first_file_id = drive.add_file(
            "first_doc.txt",
            parent_id=shared_folder_id,
            content=b"First file in shared folder",
            owned=True  # File owned by me, but folder is not
        )

        # Manually add the shared folder to state DB (simulating it being indexed)
        # This is what could happen if folder metadata was cached during path resolution
        from syncopath.state import FileState
        state_db.upsert(FileState(
            path="External Team Folder",
            file_id=shared_folder_id,
            remote_md5=None,
            local_md5=None,
            remote_mtime="2026-01-01T00:00:00.000Z",
            local_mtime=0.0,
            mime_type="application/vnd.google-apps.folder",
            is_folder=True,
        ))

        # Verify the folder is in state DB
        assert state_db.get_by_file_id(shared_folder_id) is not None

        # Now a second file appears in the same shared folder via Changes API
        token = state_db.get_page_token()
        second_file_id = drive.add_file(
            "second_doc.txt",
            parent_id=shared_folder_id,
            content=b"Second file in shared folder",
            owned=True  # File owned by me, but folder is not
        )

        # Simulate the change
        drive._changes.append({
            "fileId": second_file_id,
            "removed": False,
            "file": drive._file_to_meta(drive._files[second_file_id]),
        })

        # Process the change
        changes, _ = drive.get_changes(token)
        from syncopath.remote_watcher import RemoteChange
        remote_changes = [
            RemoteChange(c["fileId"], "created", c.get("file"))
            for c in changes if not c.get("removed")
        ]
        engine.handle_remote_changes(remote_changes)

        # File should NOT be downloaded — even though folder is in state DB,
        # we must check ownership via API and see that the folder is shared
        assert not (local_root / "External Team Folder" / "second_doc.txt").exists()
        assert not (local_root / "second_doc.txt").exists()  # Not at root either

    def test_nested_shared_folder_excluded(self, setup):
        """Test: file in nested shared folder chain is excluded.

        Scenario: /My Drive/Projects -> /Shared Folder (not owned) -> /Subfolder -> file.txt
        The file should be excluded because an ancestor is not owned.
        """
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Create initial state
        drive.add_file("my_file.txt", content=b"mine")
        engine.initial_sync()
        token = state_db.get_page_token()

        # Create a nested structure: owned folder -> shared folder -> subfolder -> file
        owned_parent = drive.add_folder("Projects", owned=True)
        shared_middle = drive.add_folder("Partner Shared", parent_id=owned_parent, owned=False)
        nested_sub = drive.add_folder("Subfolder", parent_id=shared_middle, owned=True)

        file_id = drive.add_file(
            "nested_file.txt",
            parent_id=nested_sub,
            content=b"Deep in shared folder",
            owned=True
        )

        # Simulate the change
        drive._changes.append({
            "fileId": file_id,
            "removed": False,
            "file": drive._file_to_meta(drive._files[file_id]),
        })

        # Process
        changes, _ = drive.get_changes(token)
        from syncopath.remote_watcher import RemoteChange
        remote_changes = [
            RemoteChange(c["fileId"], "created", c.get("file"))
            for c in changes if not c.get("removed")
        ]
        engine.handle_remote_changes(remote_changes)

        # File should NOT be downloaded — parent chain contains a shared folder
        assert not (local_root / "Projects" / "Partner Shared" / "Subfolder" / "nested_file.txt").exists()
        assert not (local_root / "nested_file.txt").exists()


class TestRetryOnFailure:
    """Test: transient error on download → engine doesn't crash."""

    def test_download_failure_marks_error(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Add a file but make download fail
        file_id = drive.add_file("broken.txt", content=b"data")
        engine.initial_sync()

        # Simulate a change where download will fail
        original_download = drive.download_file

        def failing_download(*args, **kwargs):
            raise IOError("Network timeout")

        drive.download_file = failing_download
        drive.simulate_remote_change(file_id, new_content=b"updated")

        changes, _ = drive.get_changes(state_db.get_page_token())
        remote_changes = [
            RemoteChange(c["fileId"], "modified", c.get("file"))
            for c in changes if not c.get("removed")
        ]

        # Should not crash
        engine.handle_remote_changes(remote_changes)

        # File should be marked as error in state
        state = state_db.get_by_file_id(file_id)
        assert state is not None
        assert state.sync_status == "error"


class TestScanLocalUntracked:
    """Test: scan finds untracked local files and handles them correctly."""

    def test_new_local_file_uploaded(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Initial sync (empty remote)
        engine.initial_sync()

        # Create a local file after sync
        (local_root / "added_offline.txt").write_bytes(b"added while offline")

        # Run scan
        engine.scan_local_untracked()

        # File should be uploaded
        uploaded = [f for f in drive._files.values()
                    if f.name == "added_offline.txt" and not f.trashed]
        assert len(uploaded) == 1

    def test_file_already_on_remote_not_reuploaded(self, setup):
        engine, drive, state_db, local_root = (
            setup["engine"], setup["drive"], setup["state_db"], setup["local_root"])

        # Add file on remote and download it
        drive.add_file("existing.txt", content=b"content")
        engine.initial_sync()

        # Remove from state DB (simulates state loss)
        state_db.delete_by_path("existing.txt")

        # Count uploads before scan
        changes_before = len(drive._changes)

        # Run scan — should re-link, not re-upload
        engine.scan_local_untracked()

        # No new upload should have happened
        changes_after = len(drive._changes)
        assert changes_after == changes_before

        # But state DB should be re-populated
        state = state_db.get_by_path("existing.txt")
        assert state is not None
        assert state.sync_status == "synced"
