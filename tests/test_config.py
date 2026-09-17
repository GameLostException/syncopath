"""Unit tests for syncopath.config — path validation and sanitization."""
import pytest
from pathlib import Path

from syncopath.config import (
    sanitize_dirname,
    default_account_path,
    default_shared_drive_path,
    _validate_no_path_overlap,
)


class TestSanitizeDirname:
    """Tests for directory name sanitization."""

    def test_normal_name(self):
        assert sanitize_dirname("my-drive") == "my-drive"

    def test_slashes_replaced(self):
        result = sanitize_dirname("folder/name")
        assert "/" not in result
        assert result  # Not empty

    def test_backslash_replaced(self):
        result = sanitize_dirname("back\\slash")
        assert "\\" not in result

    def test_colon_replaced(self):
        result = sanitize_dirname("drive:name")
        assert ":" not in result

    def test_leading_dots_stripped(self):
        result = sanitize_dirname("...hidden")
        assert not result.startswith(".")

    def test_empty_falls_back(self):
        assert sanitize_dirname("") == "unnamed"
        assert sanitize_dirname("...") == "unnamed"

    def test_special_chars(self):
        result = sanitize_dirname('file*name?"<>|')
        assert all(c not in result for c in '*?"<>|')


class TestDefaultPaths:
    """Tests for default path generation."""

    def test_account_path(self):
        path = default_account_path("work")
        assert "work" in str(path)
        assert "GoogleDrive" in str(path)

    def test_shared_drive_path(self):
        path = default_shared_drive_path("work", "Team Drive")
        assert "work" in str(path)
        assert "Team Drive" in str(path)


class TestPathOverlapValidation:
    """Tests for path overlap detection."""

    def test_no_overlap(self):
        # Should not raise
        _validate_no_path_overlap([
            Path("/home/user/drive-a"),
            Path("/home/user/drive-b"),
            Path("/home/user/drive-c"),
        ])

    def test_parent_contains_child(self):
        with pytest.raises(ValueError, match="overlap"):
            _validate_no_path_overlap([
                Path("/home/user/drive"),
                Path("/home/user/drive/subfolder"),
            ])

    def test_child_inside_parent(self):
        with pytest.raises(ValueError, match="overlap"):
            _validate_no_path_overlap([
                Path("/home/user/gdrive/shared"),
                Path("/home/user/gdrive"),
            ])

    def test_same_path_twice(self):
        with pytest.raises(ValueError, match="overlap"):
            _validate_no_path_overlap([
                Path("/home/user/drive"),
                Path("/home/user/drive"),
            ])

    def test_similar_names_not_overlap(self):
        # drive-a is NOT inside drive-ab
        _validate_no_path_overlap([
            Path("/home/user/drive-a"),
            Path("/home/user/drive-ab"),
        ])
