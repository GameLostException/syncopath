"""Bugs found while adding the 'desoya' account (2026-11-09)."""
import time
from unittest.mock import MagicMock, patch

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from syncopath import preferences
from syncopath.preferences import PreferencesWindow

ERROR = ('<HttpError 403 when requesting https://example/files?q=a&b returned '
         '"User rate limit exceeded.">')


# ─── 1. Preferences shows "Not authenticated" after a successful sign-in ────

class TestAuthStatusRefresh:

    def _prefs(self, tokens_dir):
        prefs = MagicMock()
        prefs._accounts_listbox.get_children.return_value = []
        return prefs

    def test_window_refreshes_when_the_token_appears(self, tmp_path):
        prefs = self._prefs(tmp_path)
        with patch.object(preferences, "TOKENS_DIR", tmp_path):
            started = time.time() - 1
            # Sign-in still running: keep watching, no refresh yet
            assert PreferencesWindow._check_auth_done(prefs, "desoya", started) is True
            prefs._refresh_account_list.assert_not_called()

            (tmp_path / "desoya.json").write_text("{}")  # the terminal finished sign-in
            assert PreferencesWindow._check_auth_done(prefs, "desoya", started) is False
        prefs._refresh_account_list.assert_called_once()

    def test_authenticate_starts_watching_for_the_token(self, tmp_path):
        prefs = MagicMock()
        with patch.object(preferences.subprocess, "Popen"), \
             patch.object(preferences.GLib, "timeout_add_seconds") as timer:
            PreferencesWindow._do_authenticate(prefs, "desoya")
        assert timer.called
        assert timer.call_args.args[1] == prefs._check_auth_done


# ─── 2. Error text with "<" breaks the Pango markup ─────────────────────────

class TestErrorMarkup:

    def test_shared_with_me_error_is_shown(self):
        prefs = MagicMock()
        prefs._swm_stats_label = Gtk.Label()
        PreferencesWindow._on_swm_stats_loaded(prefs, {}, ERROR)
        assert "User rate limit exceeded" in prefs._swm_stats_label.get_text()

    def test_shared_drive_size_error_is_shown(self):
        prefs = MagicMock()
        label = Gtk.Label()
        prefs._shared_drive_size_labels = {"d1": label}
        PreferencesWindow._on_drive_size_ready(prefs, "d1", 0, ERROR)
        assert "User rate limit exceeded" in label.get_text()

    def test_issues_error_is_shown(self):
        prefs = MagicMock()
        prefs._issues_status = Gtk.Label()
        PreferencesWindow._on_issues_loaded(prefs, [], ERROR, {"name": "a"})
        assert "User rate limit exceeded" in prefs._issues_status.get_text()

    def test_account_name_with_markup_characters_is_shown(self, tmp_path):
        with patch.object(preferences, "TOKENS_DIR", tmp_path):
            row = preferences.AccountRow({"name": "R&D <team>", "local_path": "/x"})
        texts = [w.get_text() for w in _walk(row) if isinstance(w, Gtk.Label)]
        assert "R&D <team>" in texts


def _walk(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _walk(child)


# ─── 3. Initial sync is marked complete when low disk space stopped it ──────

class TestInitialSyncLowDisk:

    def test_stopped_initial_sync_is_not_marked_complete(self, setup):
        engine, drive, state_db = setup["engine"], setup["drive"], setup["state_db"]
        drive.add_file("a.txt", content=b"A")
        drive.add_file("b.txt", content=b"B")
        checks = iter([True, False])  # room for the first file only
        engine._check_disk_space = lambda: next(checks, False)

        try:
            engine.initial_sync()
        except Exception:
            pass  # stopping is fine; claiming success is not

        assert state_db.get_page_token() is None

    def test_initial_sync_resumes_when_space_is_back(self, setup):
        engine, drive, state_db = setup["engine"], setup["drive"], setup["state_db"]
        drive.add_file("a.txt", content=b"A")
        drive.add_file("b.txt", content=b"B")
        engine._check_disk_space = lambda: False
        try:
            engine.initial_sync()
        except Exception:
            pass

        engine._check_disk_space = lambda: True
        engine.initial_sync()  # the folder is not empty now: it must resume, not refuse

        assert state_db.get_page_token() is not None
        assert (setup["local_root"] / "b.txt").read_bytes() == b"B"

    def test_low_disk_is_retried_not_refused_by_the_daemon(self, setup):
        """The daemon retries any failure except ValueError (a refused folder)."""
        engine, drive = setup["engine"], setup["drive"]
        drive.add_file("a.txt", content=b"A")
        engine._check_disk_space = lambda: False
        raised = None
        try:
            engine.initial_sync()
        except Exception as e:
            raised = e
        assert raised is not None and not isinstance(raised, ValueError)


# ─── 4. "Shared with me" stats of one account overwrite another's ───────────

class TestSharedWithMeStatsBelongToTheirAccount:

    def test_slow_result_for_first_account_does_not_overwrite_second(self):
        prefs = MagicMock()
        prefs._on_swm_stats_loaded = lambda *a: PreferencesWindow._on_swm_stats_loaded(prefs, *a)
        prefs._update_swm_stats_label = (
            lambda *a: PreferencesWindow._update_swm_stats_label(prefs, *a))
        first_label = Gtk.Label()
        prefs._swm_stats_label = first_label

        fetches = []
        acc_a = MagicMock()
        acc_a.name = "first"
        drive = MagicMock()
        drive.get_shared_with_me_stats.return_value = {"file_count": 111, "total_size": 0}
        with patch.object(preferences.threading, "Thread",
                          lambda target, daemon: MagicMock(start=lambda: fetches.append(target))), \
             patch.object(preferences.GLib, "idle_add", lambda fn, *args: fn(*args)), \
             patch.object(preferences.AppConfig, "load", return_value=MagicMock(accounts=[acc_a])), \
             patch("syncopath.drive_api.DriveAPI", return_value=drive):
            PreferencesWindow._refresh_swm_stats(prefs, {"name": "first"})  # fetch starts

            # The user selects the second account: its tab and label replace the first
            second_label = Gtk.Label(label="222 files · 0 bytes")
            prefs._swm_stats_label = second_label

            fetches[0]()  # the first account's slow fetch finishes now

        assert second_label.get_text() == "222 files · 0 bytes"
        assert "111" in first_label.get_text()
