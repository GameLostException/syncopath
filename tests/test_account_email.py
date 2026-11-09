"""Preferences shows each account by its Google email (owner's request, 2026-11-09)."""
import json
from unittest.mock import MagicMock, patch

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from syncopath import drive_api, preferences
from syncopath.drive_api import DriveAPI


def _labels(widget):
    out = []
    if isinstance(widget, Gtk.Label):
        out.append(widget.get_text())
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            out += _labels(child)
    return out


class TestEmailFromDrive:

    def test_reads_the_signed_in_email(self, tmp_path):
        api = DriveAPI("acc", "id", "secret")
        api._service = MagicMock()
        api._service.about.return_value.get.return_value.execute.return_value = {
            "user": {"emailAddress": "desoya@gmail.com"}}
        assert api.get_account_email() == "desoya@gmail.com"
        assert api._service.about.return_value.get.call_args.kwargs["fields"] == "user(emailAddress)"

    def test_email_is_stored_with_the_token_and_survives_a_refresh(self, tmp_path):
        with patch.object(drive_api, "TOKENS_DIR", tmp_path):
            api = DriveAPI("acc", "id", "secret")
            api._creds = MagicMock(token="t1", refresh_token="r1")
            api._save_credentials()
            api.remember_email("desoya@gmail.com")
            assert drive_api.stored_email("acc") == "desoya@gmail.com"

            api._creds = MagicMock(token="t2", refresh_token="r1")  # token refreshed
            api._save_credentials()
            assert drive_api.stored_email("acc") == "desoya@gmail.com"
            assert json.loads((tmp_path / "acc.json").read_text())["access_token"] == "t2"

    def test_no_email_known(self, tmp_path):
        with patch.object(drive_api, "TOKENS_DIR", tmp_path):
            assert drive_api.stored_email("never-signed-in") is None


class TestAccountRow:

    def test_row_shows_email_then_folder(self, tmp_path):
        (tmp_path / "gdrive-st.json").write_text(json.dumps(
            {"access_token": "t", "email": "bmartinez@smart-trade.net"}))
        with patch.object(preferences, "TOKENS_DIR", tmp_path), \
             patch.object(drive_api, "TOKENS_DIR", tmp_path):
            row = preferences.AccountRow({"name": "gdrive-st", "local_path": "/home/x/gdrive-st"})
        texts = _labels(row)
        assert texts[:2] == ["bmartinez@smart-trade.net", "/home/x/gdrive-st"]
        assert "gdrive-st" not in texts

    def test_row_falls_back_to_config_name_until_email_is_known(self, tmp_path):
        with patch.object(preferences, "TOKENS_DIR", tmp_path), \
             patch.object(drive_api, "TOKENS_DIR", tmp_path):
            row = preferences.AccountRow({"name": "desoya", "local_path": "/x"})
        assert _labels(row)[0] == "desoya"

    def test_row_updates_when_email_arrives(self, tmp_path):
        with patch.object(preferences, "TOKENS_DIR", tmp_path), \
             patch.object(drive_api, "TOKENS_DIR", tmp_path):
            row = preferences.AccountRow({"name": "desoya", "local_path": "/x"})
        row.show_email("desoya@gmail.com")
        assert _labels(row)[0] == "desoya@gmail.com"


class TestEmailFetchedForExistingAccounts:

    def test_signed_in_account_without_email_is_looked_up_once(self, tmp_path):
        (tmp_path / "desoya.json").write_text(json.dumps({"access_token": "t"}))
        (tmp_path / "known.json").write_text(json.dumps({"access_token": "t", "email": "k@x"}))
        rows = {}
        for name in ("desoya", "known", "not-signed-in"):
            rows[name] = MagicMock()
            rows[name].data = {"name": name}
        prefs = MagicMock()
        prefs._accounts_listbox.get_children.return_value = list(rows.values())

        api = MagicMock()
        api.get_account_email.return_value = "desoya@gmail.com"
        acc = MagicMock()
        acc.name = "desoya"
        with patch.object(preferences, "TOKENS_DIR", tmp_path), \
             patch.object(drive_api, "TOKENS_DIR", tmp_path), \
             patch.object(preferences.threading, "Thread",
                          lambda target, daemon: MagicMock(start=target)), \
             patch.object(preferences.GLib, "idle_add", lambda fn, *a: fn(*a)), \
             patch.object(preferences.AppConfig, "load", return_value=MagicMock(accounts=[acc])), \
             patch("syncopath.drive_api.DriveAPI", return_value=api) as make_api:
            preferences.PreferencesWindow._fill_missing_emails(prefs)

        assert [c.args[0] for c in make_api.call_args_list] == ["desoya"]
        api.remember_email.assert_called_once_with("desoya@gmail.com")
        rows["desoya"].show_email.assert_called_once_with("desoya@gmail.com")
        rows["known"].show_email.assert_not_called()
