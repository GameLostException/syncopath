"""Unit tests for syncopath.state — StateDB operations."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from syncopath.state import StateDB, FileState


@pytest.fixture
def db(tmp_path):
    """Create a temporary StateDB."""
    with patch("syncopath.state.STATE_DIR", tmp_path):
        yield StateDB("test-account")


class TestStateDB:
    """Tests for StateDB CRUD operations."""

    def test_upsert_and_get_by_path(self, db):
        state = FileState(
            path="folder/file.txt",
            file_id="abc123",
            remote_md5="d41d8cd98f00b204e9800998ecf8427e",
            local_md5="d41d8cd98f00b204e9800998ecf8427e",
            remote_mtime="2026-01-01T00:00:00Z",
            local_mtime=1700000000.0,
            mime_type="text/plain",
            sync_status="synced",
        )
        db.upsert(state)
        result = db.get_by_path("folder/file.txt")
        assert result is not None
        assert result.file_id == "abc123"
        assert result.sync_status == "synced"

    def test_get_by_file_id(self, db):
        state = FileState(
            path="test.pdf",
            file_id="xyz789",
            remote_md5="abc",
            local_md5="abc",
            remote_mtime="2026-01-01T00:00:00Z",
            local_mtime=1700000000.0,
            mime_type="application/pdf",
        )
        db.upsert(state)
        result = db.get_by_file_id("xyz789")
        assert result is not None
        assert result.path == "test.pdf"

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_by_path("nonexistent") is None
        assert db.get_by_file_id("nonexistent") is None

    def test_upsert_updates_existing(self, db):
        state = FileState(
            path="file.txt", file_id="id1",
            remote_md5="aaa", local_md5="aaa",
            remote_mtime="", local_mtime=0,
            mime_type="text/plain", sync_status="unknown",
        )
        db.upsert(state)
        state.sync_status = "synced"
        state.local_md5 = "bbb"
        db.upsert(state)
        result = db.get_by_path("file.txt")
        assert result.sync_status == "synced"
        assert result.local_md5 == "bbb"

    def test_delete_by_path(self, db):
        state = FileState(
            path="to_delete.txt", file_id="del1",
            remote_md5=None, local_md5=None,
            remote_mtime="", local_mtime=0,
            mime_type="text/plain",
        )
        db.upsert(state)
        db.delete_by_path("to_delete.txt")
        assert db.get_by_path("to_delete.txt") is None

    def test_delete_by_file_id(self, db):
        state = FileState(
            path="to_delete2.txt", file_id="del2",
            remote_md5=None, local_md5=None,
            remote_mtime="", local_mtime=0,
            mime_type="text/plain",
        )
        db.upsert(state)
        db.delete_by_file_id("del2")
        assert db.get_by_file_id("del2") is None

    def test_rename(self, db):
        state = FileState(
            path="old/path.txt", file_id="ren1",
            remote_md5=None, local_md5=None,
            remote_mtime="", local_mtime=0,
            mime_type="text/plain",
        )
        db.upsert(state)
        db.rename("old/path.txt", "new/path.txt")
        assert db.get_by_path("old/path.txt") is None
        result = db.get_by_path("new/path.txt")
        assert result is not None
        assert result.file_id == "ren1"

    def test_rename_prefix(self, db):
        # Create parent folder and children
        for i in range(3):
            db.upsert(FileState(
                path=f"Projects/Alpha/file{i}.txt", file_id=f"f{i}",
                remote_md5=None, local_md5=None,
                remote_mtime="", local_mtime=0,
                mime_type="text/plain",
            ))
        # Also a file NOT under the prefix
        db.upsert(FileState(
            path="Projects/Beta/other.txt", file_id="other",
            remote_md5=None, local_md5=None,
            remote_mtime="", local_mtime=0,
            mime_type="text/plain",
        ))

        db.rename_prefix("Projects/Alpha", "Projects/Alpha Renamed")

        # Children should be renamed
        for i in range(3):
            assert db.get_by_path(f"Projects/Alpha/file{i}.txt") is None
            result = db.get_by_path(f"Projects/Alpha Renamed/file{i}.txt")
            assert result is not None
            assert result.file_id == f"f{i}"

        # Beta should be untouched
        assert db.get_by_path("Projects/Beta/other.txt") is not None

    def test_page_token(self, db):
        assert db.get_page_token() is None
        db.set_page_token("12345")
        assert db.get_page_token() == "12345"
        db.set_page_token("67890")
        assert db.get_page_token() == "67890"

    def test_set_sync_status(self, db):
        db.upsert(FileState(
            path="status_test.txt", file_id="st1",
            remote_md5=None, local_md5=None,
            remote_mtime="", local_mtime=0,
            mime_type="text/plain", sync_status="unknown",
        ))
        db.set_sync_status("status_test.txt", "synced")
        assert db.get_by_path("status_test.txt").sync_status == "synced"

    def test_get_all(self, db):
        for i in range(5):
            db.upsert(FileState(
                path=f"file{i}.txt", file_id=f"id{i}",
                remote_md5=None, local_md5=None,
                remote_mtime="", local_mtime=0,
                mime_type="text/plain",
            ))
        all_states = db.get_all()
        assert len(all_states) == 5

    def test_clear(self, db):
        db.upsert(FileState(
            path="clearme.txt", file_id="c1",
            remote_md5=None, local_md5=None,
            remote_mtime="", local_mtime=0,
            mime_type="text/plain",
        ))
        db.set_page_token("token123")
        db.clear()
        assert db.get_by_path("clearme.txt") is None
        assert db.get_page_token() is None

    def test_is_folder_flag(self, db):
        db.upsert(FileState(
            path="MyFolder", file_id="folder1",
            remote_md5=None, local_md5=None,
            remote_mtime="", local_mtime=0,
            mime_type="application/vnd.google-apps.folder",
            is_folder=True,
        ))
        result = db.get_by_path("MyFolder")
        assert result.is_folder is True

    def test_wal_mode_enabled(self, db):
        """Verify WAL mode is active for concurrent read safety."""
        conn = sqlite3.connect(str(db.db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"
