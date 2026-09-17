"""Unit tests for syncopath.drive_api — utility functions."""
import pytest

from syncopath.drive_api import sanitize_filename, is_skip_mime, GOOGLE_SKIP_MIMES


class TestSanitizeFilename:
    """Tests for filename sanitization."""

    def test_normal_filename_unchanged(self):
        assert sanitize_filename("report.pdf") == "report.pdf"

    def test_fullwidth_slash_replaced(self):
        # U+FF0F (fullwidth solidus) → U+2215 (division slash)
        assert sanitize_filename("BAY／EBS draft") == "BAY∕EBS draft"

    def test_regular_slash_replaced(self):
        # U+002F → U+2215
        assert sanitize_filename("folder/file") == "folder∕file"

    def test_null_removed(self):
        assert sanitize_filename("file\x00name.txt") == "filename.txt"

    def test_multiple_slashes(self):
        assert sanitize_filename("a／b／c/d") == "a∕b∕c∕d"

    def test_empty_string(self):
        assert sanitize_filename("") == ""

    def test_unicode_preserved(self):
        assert sanitize_filename("résumé — final.docx") == "résumé — final.docx"

    def test_spaces_preserved(self):
        assert sanitize_filename("  file  name  ") == "  file  name  "


class TestIsSkipMime:
    """Tests for MIME type skip detection."""

    def test_google_script_skipped(self):
        assert is_skip_mime("application/vnd.google-apps.script") is True

    def test_google_folder_skipped(self):
        assert is_skip_mime("application/vnd.google-apps.folder") is True

    def test_google_shortcut_skipped(self):
        assert is_skip_mime("application/vnd.google-apps.shortcut") is True

    def test_lucidchart_skipped(self):
        assert is_skip_mime("application/vnd.google-apps.drive-sdk.70") is True

    def test_other_drive_sdk_skipped(self):
        assert is_skip_mime("application/vnd.google-apps.drive-sdk.123456") is True

    def test_google_doc_not_skipped(self):
        assert is_skip_mime("application/vnd.google-apps.document") is False

    def test_google_sheet_not_skipped(self):
        assert is_skip_mime("application/vnd.google-apps.spreadsheet") is False

    def test_pdf_not_skipped(self):
        assert is_skip_mime("application/pdf") is False

    def test_plain_text_not_skipped(self):
        assert is_skip_mime("text/plain") is False


class TestGetSharedWithMeStats:
    """Tests for get_shared_with_me_stats method."""

    def test_counts_shared_files_and_size(self):
        from tests.mock_drive import MockDriveAPI
        drive = MockDriveAPI()

        # Add owned files (should not be counted)
        drive.add_file("my_file.txt", content=b"1234567890", owned=True)
        drive.add_file("my_doc.pdf", content=b"x" * 1000, owned=True)

        # Add shared files (should be counted)
        drive.add_file("shared1.txt", content=b"abc", owned=False)
        drive.add_file("shared2.pdf", content=b"x" * 500, owned=False)
        drive.add_file("shared3.doc", content=b"y" * 200, owned=False)

        stats = drive.get_shared_with_me_stats()

        assert stats["file_count"] == 3
        assert stats["total_size"] == 3 + 500 + 200  # 703 bytes

    def test_excludes_folders(self):
        from tests.mock_drive import MockDriveAPI
        drive = MockDriveAPI()

        # Add a shared folder (should not be counted)
        drive.add_folder("Shared Folder", owned=False)

        # Add a shared file
        drive.add_file("shared.txt", content=b"data", owned=False)

        stats = drive.get_shared_with_me_stats()

        assert stats["file_count"] == 1
        assert stats["total_size"] == 4

    def test_empty_when_no_shared_files(self):
        from tests.mock_drive import MockDriveAPI
        drive = MockDriveAPI()

        # Only owned files
        drive.add_file("owned.txt", content=b"mine", owned=True)

        stats = drive.get_shared_with_me_stats()

        assert stats["file_count"] == 0
        assert stats["total_size"] == 0
