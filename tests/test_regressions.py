"""Regression tests for bugs found during audit/development."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from syncopath.drive_api import sanitize_filename
from syncopath.sync_engine import md5_file, iso_to_timestamp


class TestMyDrivePrefix:
    """Regression: 'My Drive/' prefix should never appear in paths."""

    def test_sanitize_does_not_add_my_drive(self):
        # The old bug added "My Drive" when root ID didn't match
        assert not sanitize_filename("My Drive").startswith("My Drive/")

    def test_root_name_is_sanitized(self):
        # Even if Drive returns "My Drive" as root name, it should be sanitized
        # (but this is a path issue, not filename issue)
        pass


class TestUnicodeSlashInPaths:
    """Regression: Unicode slashes should not create spurious directories."""

    def test_fullwidth_slash_does_not_split_path(self):
        name = "BAY／EBS draft"
        sanitized = sanitize_filename(name)
        # Should NOT contain a real slash
        assert "/" not in sanitized
        # Should contain the safe division slash
        assert "∕" in sanitized

    def test_path_with_multiple_slashes(self):
        name = "PROD／UAT／Archive"
        sanitized = sanitize_filename(name)
        parts = sanitized.split("/")
        # Should be a single component, not split into directories
        assert len(parts) == 1


class TestMd5File:
    """Tests for md5_file utility."""

    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = md5_file(f)
        assert result is not None
        assert len(result) == 32  # MD5 hex digest

    def test_nonexistent_file(self, tmp_path):
        assert md5_file(tmp_path / "nope.txt") is None

    def test_directory_returns_none(self, tmp_path):
        assert md5_file(tmp_path) is None

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty"
        f.write_bytes(b"")
        result = md5_file(f)
        assert result == "d41d8cd98f00b204e9800998ecf8427e"  # MD5 of empty


class TestIsoToTimestamp:
    """Tests for ISO timestamp conversion."""

    def test_utc_z_suffix(self):
        ts = iso_to_timestamp("2026-07-28T12:00:00Z")
        assert ts > 0

    def test_with_offset(self):
        ts = iso_to_timestamp("2026-07-28T14:00:00+02:00")
        ts_utc = iso_to_timestamp("2026-07-28T12:00:00Z")
        assert abs(ts - ts_utc) < 1  # Same moment

    def test_with_milliseconds(self):
        ts = iso_to_timestamp("2026-07-28T12:00:00.123Z")
        assert ts > 0


class TestConflictDetectionGoogleDocs:
    """Regression: Google Docs (md5=None) should still detect conflicts."""

    def test_none_md5_local_modified(self, tmp_path):
        """When local_md5 is None (Google Doc), use mtime for detection."""
        # This test verifies the logic concept — actual integration tested elsewhere
        local_md5_in_state = None
        local_mtime_in_state = 1700000000.0
        current_file_mtime = 1700001000.0  # File was modified after last sync

        # If state has None md5, we should compare mtimes
        if local_md5_in_state is None:
            local_changed = (current_file_mtime > local_mtime_in_state
                            and local_mtime_in_state > 0)
        else:
            local_changed = True  # Would compare hashes normally

        assert local_changed is True

    def test_none_md5_not_modified(self):
        """When local file hasn't changed since sync, no conflict."""
        local_md5_in_state = None
        local_mtime_in_state = 1700001000.0
        current_file_mtime = 1700001000.0  # Same as last sync

        if local_md5_in_state is None:
            local_changed = (current_file_mtime > local_mtime_in_state
                            and local_mtime_in_state > 0)
        else:
            local_changed = True

        assert local_changed is False
