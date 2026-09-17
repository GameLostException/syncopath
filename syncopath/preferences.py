"""GTK Preferences window for SyncoPath."""
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango

import yaml

from .config import (
    CONFIG_FILE, CONFIG_DIR, TOKENS_DIR, STATE_DIR,
    AppConfig, sanitize_dirname, default_account_path,
    default_shared_drive_path, validate_sync_dir_empty,
    _validate_no_path_overlap,
)

log = logging.getLogger(__name__)


# ─── Account List Row ───────────────────────────────────────────────────────

class AccountRow(Gtk.ListBoxRow):
    """A row representing one sync account in the left panel."""

    def __init__(self, account_data: dict):
        super().__init__()
        self.data = account_data
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.set_margin_start(8)
        hbox.set_margin_end(8)
        hbox.set_margin_top(6)
        hbox.set_margin_bottom(6)

        # Status icon
        token_path = TOKENS_DIR / f"{account_data['name']}.json"
        is_authenticated = token_path.exists()
        if is_authenticated:
            icon = Gtk.Image.new_from_icon_name("emblem-default", Gtk.IconSize.MENU)
            icon.set_tooltip_text("Authenticated")
        else:
            icon = Gtk.Image.new_from_icon_name("dialog-warning", Gtk.IconSize.MENU)
            icon.set_tooltip_text("Not authenticated")
        hbox.pack_start(icon, False, False, 0)

        # Account info
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name_label = Gtk.Label()
        name_label.set_markup(f"<b>{account_data['name']}</b>")
        name_label.set_xalign(0)
        vbox.pack_start(name_label, False, False, 0)

        path_label = Gtk.Label(label=account_data.get("local_path", ""))
        path_label.set_xalign(0)
        path_label.set_opacity(0.6)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_label.set_max_width_chars(20)
        vbox.pack_start(path_label, False, False, 0)

        hbox.pack_start(vbox, True, True, 0)
        self.add(hbox)


# ─── Main Preferences Window ───────────────────────────────────────────────

class PreferencesWindow(Gtk.Window):
    """Main preferences/settings window with two top-level tabs:
    - Accounts (split view: list left, per-account settings right)
    - Global Settings
    """

    def __init__(self, on_save_callback=None):
        super().__init__(title="SyncoPath — Preferences")
        self.set_default_size(780, 560)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(0)
        self._on_save = on_save_callback
        self._config_data = self._load_config()

        # Header bar
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("SyncoPath")
        header.set_subtitle("Preferences")
        self.set_titlebar(header)

        # Main notebook: Accounts | Global Settings
        self._main_notebook = Gtk.Notebook()
        self._main_notebook.set_margin_start(8)
        self._main_notebook.set_margin_end(8)
        self._main_notebook.set_margin_top(8)
        self._main_notebook.set_margin_bottom(8)

        # Tab 1: Accounts (split pane)
        self._main_notebook.append_page(
            self._build_accounts_pane(), Gtk.Label(label="Accounts"))
        # Tab 2: Global Settings
        self._main_notebook.append_page(
            self._build_global_settings_tab(), Gtk.Label(label="Global Settings"))

        self.add(self._main_notebook)

        # Select first account before show_all so the right panel is populated
        if self._accounts_listbox.get_children():
            self._accounts_listbox.select_row(self._accounts_listbox.get_row_at_index(0))
            # Prevent show_all from re-showing the placeholder
            self._no_account_label.set_no_show_all(True)
        else:
            # No accounts — hide notebook, show placeholder
            self._account_notebook.set_no_show_all(True)

        self.show_all()

    def _load_config(self) -> dict:
        """Load raw config YAML."""
        if CONFIG_FILE.exists():
            return yaml.safe_load(CONFIG_FILE.read_text())
        return {"log_level": "INFO", "accounts": [], "safe_mode": True,
                "icon_theme": "dark", "min_free_space_gb": 5}

    def _save_config(self):
        """Save config back to YAML."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(yaml.dump(
            self._config_data, default_flow_style=False, sort_keys=False))
        log.info("Config saved to %s", CONFIG_FILE)
        if self._on_save:
            self._on_save()

    # ─── Accounts Pane (split: list left, details right) ───────────────────

    def _build_accounts_pane(self) -> Gtk.Paned:
        """Build the split-pane accounts view."""
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)

        # ── Left panel: Account list (1/3) ──
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_margin_top(8)
        left_box.set_margin_bottom(8)
        left_box.set_margin_start(4)
        left_box.set_margin_end(4)
        left_box.set_size_request(220, -1)

        # Account listbox in a frame
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._accounts_listbox = Gtk.ListBox()
        self._accounts_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._accounts_listbox.connect("row-selected", self._on_account_selected)
        self._refresh_account_list()
        scroll.add(self._accounts_listbox)
        frame.add(scroll)
        left_box.pack_start(frame, True, True, 0)

        # Add/Remove buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_btn = Gtk.Button(label="Add")
        add_btn.set_tooltip_text("Add account")
        add_btn.connect("clicked", self._on_add_account)
        btn_box.pack_start(add_btn, True, True, 0)

        remove_btn = Gtk.Button(label="Remove")
        remove_btn.set_tooltip_text("Remove selected account")
        remove_btn.connect("clicked", self._on_remove_account)
        btn_box.pack_start(remove_btn, True, True, 0)
        left_box.pack_start(btn_box, False, False, 0)

        paned.pack1(left_box, resize=False, shrink=False)

        # ── Right panel: Per-account settings (2/3) ──
        self._right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._right_panel.set_margin_top(8)
        self._right_panel.set_margin_bottom(8)
        self._right_panel.set_margin_start(8)
        self._right_panel.set_margin_end(4)

        # Placeholder when no account selected
        self._no_account_label = Gtk.Label(label="Select an account to configure")
        self._no_account_label.set_opacity(0.5)
        self._right_panel.pack_start(self._no_account_label, True, True, 0)

        # Per-account notebook (initially empty, built on account selection)
        self._account_notebook = Gtk.Notebook()
        self._right_panel.pack_start(self._account_notebook, True, True, 0)

        paned.pack2(self._right_panel, resize=True, shrink=False)
        paned.set_position(220)

        return paned

    def _on_account_selected(self, listbox, row):
        """When an account is selected, show its per-account settings."""
        if not row:
            self._account_notebook.hide()
            self._account_notebook.set_no_show_all(True)
            self._no_account_label.set_no_show_all(False)
            self._no_account_label.show()
            return

        self._no_account_label.hide()
        self._no_account_label.set_no_show_all(True)
        self._rebuild_account_notebook(row.data)
        self._account_notebook.set_no_show_all(False)
        self._account_notebook.show_all()

    def _rebuild_account_notebook(self, account_data: dict):
        """Rebuild the per-account notebook tabs for the selected account."""
        # Remove old pages
        while self._account_notebook.get_n_pages() > 0:
            self._account_notebook.remove_page(0)

        self._current_account = account_data
        acc_name = account_data["name"]
        is_shared_drive = bool(account_data.get("shared_drive_id"))

        # Tab: General (auth + basic info)
        self._account_notebook.append_page(
            self._build_account_general_tab(account_data),
            Gtk.Label(label="General"))

        # Tab: Shared Drives (only for non-shared-drive accounts)
        if not is_shared_drive:
            self._account_notebook.append_page(
                self._build_account_shared_drives_tab(account_data),
                Gtk.Label(label="Shared Drives"))

        # Tab: Sync
        self._account_notebook.append_page(
            self._build_account_sync_tab(account_data),
            Gtk.Label(label="Sync"))

        # Tab: Exclusions
        self._account_notebook.append_page(
            self._build_account_exclusions_tab(account_data),
            Gtk.Label(label="Exclusions"))

        # Tab: Issues (count updated dynamically)
        self._issues_tab_label = Gtk.Label(label="Issues")
        self._account_notebook.append_page(
            self._build_account_issues_tab(account_data),
            self._issues_tab_label)

        self._account_notebook.show_all()

    # ─── Per-Account: General Tab ──────────────────────────────────────────

    def _build_account_general_tab(self, account_data: dict) -> Gtk.Box:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        acc_name = account_data["name"]
        token_path = TOKENS_DIR / f"{acc_name}.json"
        is_authenticated = token_path.exists()

        # Account name
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_box.pack_start(Gtk.Label(label="Account:"), False, False, 0)
        name_label = Gtk.Label()
        name_label.set_markup(f"<b>{acc_name}</b>")
        name_box.pack_start(name_label, False, False, 0)
        vbox.pack_start(name_box, False, False, 0)

        # Local path
        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        path_box.pack_start(Gtk.Label(label="Local folder:"), False, False, 0)
        path_label = Gtk.Label(label=account_data.get("local_path", ""))
        path_label.set_xalign(0)
        path_label.set_selectable(True)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_box.pack_start(path_label, True, True, 0)
        vbox.pack_start(path_box, False, False, 0)

        # Shared drive ID (if applicable)
        if account_data.get("shared_drive_id"):
            sd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            sd_box.pack_start(Gtk.Label(label="Shared Drive ID:"), False, False, 0)
            sd_label = Gtk.Label(label=account_data["shared_drive_id"])
            sd_label.set_selectable(True)
            sd_label.set_opacity(0.7)
            sd_box.pack_start(sd_label, False, False, 0)
            vbox.pack_start(sd_box, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 8)

        # Authentication section
        auth_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        if is_authenticated:
            # Red "Deauthenticate" button
            deauth_btn = Gtk.Button(label="Deauthenticate")
            deauth_btn.get_style_context().add_class("destructive-action")
            deauth_btn.set_tooltip_text("Revoke credentials and delete stored token")
            deauth_btn.connect("clicked", lambda _: self._do_deauthenticate(acc_name))
            auth_box.pack_start(deauth_btn, False, False, 0)

            status_label = Gtk.Label(label="✅ Authenticated")
            status_label.set_opacity(0.7)
            auth_box.pack_start(status_label, False, False, 0)
        else:
            auth_btn = Gtk.Button(label="Authenticate")
            auth_btn.get_style_context().add_class("suggested-action")
            auth_btn.connect("clicked", lambda _: self._do_authenticate(acc_name))
            auth_box.pack_start(auth_btn, False, False, 0)

            status_label = Gtk.Label(label="⚠ Not authenticated")
            status_label.set_opacity(0.7)
            auth_box.pack_start(status_label, False, False, 0)

        vbox.pack_start(auth_box, False, False, 0)

        # Include "Shared with me" checkbox (only for non-shared-drive accounts)
        if not account_data.get("shared_drive_id"):
            vbox.pack_start(Gtk.Separator(), False, False, 8)

            shared_with_me_label = Gtk.Label()
            shared_with_me_label.set_markup("<b>Shared with me</b>")
            shared_with_me_label.set_xalign(0)
            vbox.pack_start(shared_with_me_label, False, False, 0)

            self._shared_with_me_check = Gtk.CheckButton(
                label="Include files shared with me")
            self._shared_with_me_check.set_tooltip_text(
                "Sync files from \"Shared with me\" into a dedicated subfolder")
            self._shared_with_me_check.set_active(
                account_data.get("include_shared_with_me", False))
            vbox.pack_start(self._shared_with_me_check, False, False, 0)

            swm_desc = Gtk.Label()
            swm_desc.set_markup(
                "<small><i>Files will be synced to: "
                f"{account_data.get('local_path', '')}/Shared with me/</i></small>")
            swm_desc.set_xalign(0)
            swm_desc.set_opacity(0.6)
            swm_desc.set_line_wrap(True)
            vbox.pack_start(swm_desc, False, False, 0)

            # Shared-with-me stats display
            stats_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            stats_box.set_margin_top(8)

            self._swm_stats_label = Gtk.Label()
            self._swm_stats_label.set_xalign(0)
            self._swm_stats_label.set_markup("<small><i>Loading stats...</i></small>")
            stats_box.pack_start(self._swm_stats_label, True, True, 0)

            self._swm_stats_spinner = Gtk.Spinner()
            stats_box.pack_start(self._swm_stats_spinner, False, False, 0)

            refresh_btn = Gtk.Button()
            refresh_btn.set_image(Gtk.Image.new_from_icon_name(
                "view-refresh-symbolic", Gtk.IconSize.BUTTON))
            refresh_btn.set_tooltip_text("Refresh shared-with-me stats")
            refresh_btn.connect("clicked", lambda _: self._refresh_swm_stats(account_data))
            stats_box.pack_start(refresh_btn, False, False, 0)

            vbox.pack_start(stats_box, False, False, 0)

            # Load stats automatically (from cache first, then refresh)
            self._load_swm_stats_cached(account_data)
            self._refresh_swm_stats(account_data)

            # Save button for general settings
            save_btn = Gtk.Button(label="Save")
            save_btn.get_style_context().add_class("suggested-action")
            save_btn.connect("clicked", lambda _: self._on_save_account_general(account_data))
            vbox.pack_end(save_btn, False, False, 12)

        return vbox

    def _load_swm_stats_cached(self, account_data: dict):
        """Load cached shared-with-me stats from disk (instant display)."""
        cache_file = CONFIG_DIR / "swm_stats_cache.yaml"
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    cache = yaml.safe_load(f) or {}
                stats = cache.get(account_data["name"])
                if stats:
                    self._update_swm_stats_label(stats)
            except Exception:
                pass  # Ignore cache errors

    def _refresh_swm_stats(self, account_data: dict):
        """Fetch shared-with-me stats from Drive API in background."""
        self._swm_stats_spinner.start()

        def _fetch():
            try:
                from .drive_api import DriveAPI
                config = AppConfig.load()
                acc = next(a for a in config.accounts if a.name == account_data["name"])
                drive = DriveAPI(acc.name, acc.client_id, acc.client_secret)
                stats = drive.get_shared_with_me_stats()
                # Cache the result
                self._cache_swm_stats(account_data["name"], stats)
                GLib.idle_add(self._on_swm_stats_loaded, stats, None)
            except Exception as e:
                GLib.idle_add(self._on_swm_stats_loaded, None, str(e))

        threading.Thread(target=_fetch, daemon=True).start()

    def _cache_swm_stats(self, account_name: str, stats: dict):
        """Cache shared-with-me stats to disk."""
        cache_file = CONFIG_DIR / "swm_stats_cache.yaml"
        try:
            cache = {}
            if cache_file.exists():
                with open(cache_file) as f:
                    cache = yaml.safe_load(f) or {}
            cache[account_name] = stats
            with open(cache_file, "w") as f:
                yaml.safe_dump(cache, f)
        except Exception as e:
            log.warning("Failed to cache swm stats: %s", e)

    def _on_swm_stats_loaded(self, stats: dict, error: str):
        """Update UI with shared-with-me stats (main thread)."""
        self._swm_stats_spinner.stop()

        if error:
            self._swm_stats_label.set_markup(f"<small><i>⚠ {error}</i></small>")
            return False

        self._update_swm_stats_label(stats)
        return False

    def _update_swm_stats_label(self, stats: dict):
        """Format and display shared-with-me stats."""
        file_count = stats.get("file_count", 0)
        total_size = stats.get("total_size", 0)

        # Format size
        if total_size >= 1024 ** 3:
            size_str = f"{total_size / (1024 ** 3):.1f} GB"
        elif total_size >= 1024 ** 2:
            size_str = f"{total_size / (1024 ** 2):.1f} MB"
        elif total_size >= 1024:
            size_str = f"{total_size / 1024:.1f} KB"
        else:
            size_str = f"{total_size} bytes"

        self._swm_stats_label.set_markup(
            f"<small>{file_count:,} files · {size_str}</small>")

    def _on_save_account_general(self, account_data: dict):
        """Save account general settings (shared with me toggle)."""
        acc_name = account_data["name"]
        include_swm = self._shared_with_me_check.get_active()

        # Update in config data
        for acc in self._config_data.get("accounts", []):
            if acc["name"] == acc_name:
                acc["include_shared_with_me"] = include_swm
                break

        self._save_config()
        self._show_info("Settings saved. Restart SyncoPath to apply.")

    def _do_authenticate(self, account_name: str):
        """Authenticate a specific account (opens browser)."""
        venv_python = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")
        cmd = f"{venv_python} -m syncopath --auth {account_name}"

        terminals = ["xfce4-terminal", "kitty", "gnome-terminal", "xterm"]
        for term in terminals:
            try:
                if term == "xfce4-terminal":
                    subprocess.Popen([term, "-e", cmd, "--hold"])
                elif term == "kitty":
                    subprocess.Popen([term, "--hold", "sh", "-c", cmd])
                else:
                    subprocess.Popen([term, "-e", cmd])
                self._show_info(
                    f"Authentication started for '{account_name}'.\n"
                    "Complete the OAuth flow in your browser, then close the terminal.")
                return
            except FileNotFoundError:
                continue
        self._show_info("No terminal emulator found. Run manually:\n\n" + cmd)

    def _do_deauthenticate(self, account_name: str):
        """Deauthenticate an account after confirmation."""
        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Deauthenticate '{account_name}'?",
        )
        dialog.format_secondary_text(
            "This will delete the stored OAuth token and revoke access.\n"
            "You'll need to re-authenticate to sync this account again.\n\n"
            "Local files will NOT be deleted.")
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            token_path = TOKENS_DIR / f"{account_name}.json"
            if token_path.exists():
                token_path.unlink()
                log.info("Deleted token for account: %s", account_name)

            # Also clear state for this account
            state_path = STATE_DIR / f"{account_name}.db"
            if state_path.exists():
                state_path.unlink()
                log.info("Deleted state DB for account: %s", account_name)

            self._show_info(f"Account '{account_name}' deauthenticated.\n"
                           "Token and cache deleted.")
            # Refresh the UI
            self._refresh_account_list()
            if self._accounts_listbox.get_children():
                self._accounts_listbox.select_row(
                    self._accounts_listbox.get_row_at_index(0))

    # ─── Per-Account: Shared Drives Tab ────────────────────────────────────

    def _build_account_shared_drives_tab(self, account_data: dict) -> Gtk.Box:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        desc = Gtk.Label()
        desc.set_markup(
            "Enable shared drives to sync. Each gets its own local folder.\n"
            "<i>Folders must not overlap with each other or with My Drive.</i>")
        desc.set_xalign(0)
        desc.set_line_wrap(True)
        vbox.pack_start(desc, False, False, 0)

        # Scrollable list of shared drives
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(220)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._shared_drives_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll.add(self._shared_drives_box)
        frame.add(scroll)
        vbox.pack_start(frame, True, True, 0)

        # Refresh + status
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh_btn = Gtk.Button(label="Refresh from Google")
        refresh_btn.connect("clicked", lambda _: self._on_refresh_shared_drives(account_data))
        btn_box.pack_start(refresh_btn, False, False, 0)

        self._shared_drives_spinner = Gtk.Spinner()
        btn_box.pack_start(self._shared_drives_spinner, False, False, 0)

        self._shared_drives_status = Gtk.Label(label="")
        self._shared_drives_status.set_xalign(0)
        self._shared_drives_status.set_opacity(0.7)
        btn_box.pack_start(self._shared_drives_status, True, True, 0)

        vbox.pack_start(btn_box, False, False, 0)

        # Save button
        save_btn = Gtk.Button(label="Save Shared Drives")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", lambda _: self._on_save_shared_drives(account_data))
        vbox.pack_end(save_btn, False, False, 0)

        # Track shared drive rows
        self._shared_drive_rows = []
        self._shared_drive_size_labels = {}

        # Load current state
        self._populate_shared_drives_from_config(account_data)

        return vbox

    def _populate_shared_drives_from_config(self, account_data: dict):
        """Show currently configured shared drives from config."""
        enabled = {}
        for acc in self._config_data.get("accounts", []):
            sd_id = acc.get("shared_drive_id", "")
            if sd_id:
                enabled[sd_id] = acc.get("local_path", "")

        if enabled:
            self._shared_drives_status.set_text(
                f"{len(enabled)} shared drive(s) enabled. Click Refresh to see all.")
        else:
            self._shared_drives_status.set_text(
                "Click Refresh to load available shared drives.")

    def _on_refresh_shared_drives(self, account_data: dict):
        """Fetch shared drives from Google in background."""
        self._shared_drives_spinner.start()
        self._shared_drives_status.set_text("Loading...")

        def _fetch():
            try:
                from .drive_api import DriveAPI
                config = AppConfig.load()
                acc = next(a for a in config.accounts if a.name == account_data["name"])
                drive = DriveAPI(acc.name, acc.client_id, acc.client_secret)
                drives = drive.list_shared_drives()
                GLib.idle_add(self._on_shared_drives_loaded, drives, None, account_data)
            except Exception as e:
                GLib.idle_add(self._on_shared_drives_loaded, [], str(e), account_data)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_shared_drives_loaded(self, drives: list, error: Optional[str],
                                  account_data: dict):
        """Populate shared drives list (main thread)."""
        self._shared_drives_spinner.stop()

        for child in self._shared_drives_box.get_children():
            self._shared_drives_box.remove(child)
        self._shared_drive_rows = []
        self._shared_drive_size_labels = {}

        if error:
            self._shared_drives_status.set_text(f"Error: {error}")
            return False

        if not drives:
            self._shared_drives_status.set_text("No shared drives found.")
            return False

        self._shared_drives_status.set_text(
            f"{len(drives)} shared drive(s) available. Fetching sizes…")

        # Currently enabled shared drives
        enabled = {}
        for acc in self._config_data.get("accounts", []):
            sd_id = acc.get("shared_drive_id", "")
            if sd_id:
                enabled[sd_id] = acc.get("local_path", "")

        for d in sorted(drives, key=lambda x: x["name"]):
            row = self._create_shared_drive_row(
                d["id"], d["name"], enabled.get(d["id"], ""), account_data)
            self._shared_drives_box.pack_start(row, False, False, 0)

        self._shared_drives_box.show_all()
        self._fetch_shared_drive_sizes([d["id"] for d in drives], account_data)
        return False

    def _fetch_shared_drive_sizes(self, drive_ids: list, account_data: dict):
        """Fetch storage for each shared drive in background."""
        def _fetch_one(drive_id: str):
            try:
                from .drive_api import DriveAPI
                config = AppConfig.load()
                acc = next(a for a in config.accounts if a.name == account_data["name"])
                drive = DriveAPI(acc.name, acc.client_id, acc.client_secret)
                usage = drive.get_shared_drive_usage(drive_id)
                GLib.idle_add(self._on_drive_size_ready, drive_id, usage, None)
            except Exception as e:
                GLib.idle_add(self._on_drive_size_ready, drive_id, 0, str(e))

        for drive_id in drive_ids:
            threading.Thread(target=_fetch_one, args=(drive_id,), daemon=True).start()

    def _on_drive_size_ready(self, drive_id: str, usage_bytes: int, error: Optional[str]):
        """Update size label for a shared drive."""
        label = self._shared_drive_size_labels.get(drive_id)
        if not label:
            return False

        if error:
            label.set_markup(f"<small><i>⚠ {error}</i></small>")
        else:
            size_str = self._format_storage_size(usage_bytes)
            label.set_markup(f"<small><b>{size_str}</b></small>")
            label.set_tooltip_text(f"{usage_bytes:,} bytes")

        label.show()

        filled = sum(1 for lbl in self._shared_drive_size_labels.values()
                     if lbl.get_text() != "…")
        total = len(self._shared_drive_size_labels)
        if filled >= total:
            self._shared_drives_status.set_text(f"{total} shared drive(s) available.")
        return False

    @staticmethod
    def _format_storage_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes < 1024 * 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024 * 1024):.1f} TB"

    def _create_shared_drive_row(self, drive_id: str, name: str, local_path: str,
                                  account_data: dict) -> Gtk.Box:
        """Create a row widget for a shared drive."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.set_margin_top(6)
        row.set_margin_bottom(6)

        # Enable checkbox
        check = Gtk.CheckButton()
        check.set_active(bool(local_path))
        row.pack_start(check, False, False, 0)

        # Drive name + size
        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name_label = Gtk.Label(label=name)
        name_label.set_xalign(0)
        name_label.set_width_chars(20)
        name_label.set_max_width_chars(20)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.pack_start(name_label, False, False, 0)

        size_label = Gtk.Label()
        size_label.set_xalign(0)
        size_label.set_opacity(0.7)
        size_label.set_markup("<small><i>…</i></small>")
        name_box.pack_start(size_label, False, False, 0)
        self._shared_drive_size_labels[drive_id] = size_label

        row.pack_start(name_box, False, False, 0)

        # Local path entry
        path_entry = Gtk.Entry()
        default_path = str(default_shared_drive_path(account_data["name"], name))
        path_entry.set_placeholder_text(default_path)
        path_entry.set_text(local_path if local_path else default_path)
        path_entry.set_sensitive(bool(local_path))
        row.pack_start(path_entry, True, True, 0)

        # Browse button
        browse_btn = Gtk.Button(label="…")
        browse_btn.set_tooltip_text("Browse")
        browse_btn.set_sensitive(bool(local_path))

        def _browse(_b):
            chooser = Gtk.FileChooserDialog(
                title=f"Select folder for '{name}'",
                parent=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER)
            chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
            chooser.add_button("Select", Gtk.ResponseType.OK)
            current = path_entry.get_text().strip()
            if current and Path(current).exists():
                chooser.set_current_folder(current)
            if chooser.run() == Gtk.ResponseType.OK:
                path_entry.set_text(chooser.get_filename())
            chooser.destroy()

        browse_btn.connect("clicked", _browse)
        row.pack_start(browse_btn, False, False, 0)

        def _on_toggle(_chk):
            active = _chk.get_active()
            path_entry.set_sensitive(active)
            browse_btn.set_sensitive(active)

        check.connect("toggled", _on_toggle)

        # Outer with separator
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.pack_start(row, False, False, 0)
        outer.pack_start(Gtk.Separator(), False, False, 0)

        self._shared_drive_rows.append((drive_id, name, check, path_entry))
        return outer

    def _on_save_shared_drives(self, account_data: dict):
        """Save shared drive configuration for this account."""
        acc_name = account_data["name"]

        new_shared = []
        for drive_id, name, check, path_entry in self._shared_drive_rows:
            if check.get_active():
                local_path = path_entry.get_text().strip()
                if not local_path:
                    self._show_info(f"Please set a local folder for '{name}'.")
                    return

                target = Path(local_path)
                try:
                    validate_sync_dir_empty(target)
                except ValueError as e:
                    self._show_info(f"❌ {e}")
                    return

                safe_name = sanitize_dirname(name).lower()
                new_shared.append({
                    "name": f"shared-{safe_name}",
                    "local_path": local_path,
                    "shared_drive_id": drive_id,
                    "remote_id": drive_id,
                    "poll_interval": account_data.get("poll_interval", 30),
                    "debounce": account_data.get("debounce", 5),
                    "exclude": account_data.get("exclude",
                                                ["*.tmp", ".Trash*", ".syncopath*"]),
                })

        # Validate no overlap
        all_paths = []
        for acc in self._config_data.get("accounts", []):
            if not acc.get("shared_drive_id"):
                all_paths.append(Path(acc["local_path"]))
        for sd in new_shared:
            all_paths.append(Path(sd["local_path"]))

        try:
            _validate_no_path_overlap(all_paths)
        except ValueError as e:
            self._show_info(f"❌ {e}")
            return

        # Remove old shared drive entries, keep My Drive accounts
        self._config_data["accounts"] = [
            a for a in self._config_data.get("accounts", [])
            if not a.get("shared_drive_id")
        ]
        self._config_data["accounts"].extend(new_shared)
        self._save_config()

        for sd in new_shared:
            Path(sd["local_path"]).mkdir(parents=True, exist_ok=True)

        self._show_info(
            f"✅ {len(new_shared)} shared drive(s) configured.\n"
            "Restart SyncoPath to start syncing.")

    # ─── Per-Account: Sync Tab ─────────────────────────────────────────────

    def _build_account_sync_tab(self, account_data: dict) -> Gtk.Box:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        desc = Gtk.Label()
        desc.set_markup(
            "<small><i>Tick the checkbox to override the global default for this "
            "account.</i></small>")
        desc.set_xalign(0)
        desc.set_opacity(0.7)
        vbox.pack_start(desc, False, False, 0)

        # Grid layout: Label | Widget | Override checkbox
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(12)
        grid.set_column_homogeneous(False)

        # Get global defaults
        global_poll = self._config_data.get("default_poll_interval", 30)
        global_debounce = self._config_data.get("default_debounce", 5)

        # Determine if account has overrides
        acc_poll = account_data.get("poll_interval")
        acc_debounce = account_data.get("debounce")
        acc_conflict = account_data.get("conflict_resolution")
        has_poll_override = (acc_poll is not None and acc_poll != global_poll)
        has_debounce_override = (acc_debounce is not None and acc_debounce != global_debounce)
        has_conflict_override = (acc_conflict is not None and acc_conflict != "newer_wins")

        row = 0

        # ── Poll interval ──
        poll_label = Gtk.Label(label="Remote poll interval (seconds):")
        poll_label.set_xalign(0)
        grid.attach(poll_label, 0, row, 1, 1)

        self._acc_poll_spin = Gtk.SpinButton.new_with_range(10, 300, 5)
        self._acc_poll_spin.set_value(acc_poll if acc_poll is not None else global_poll)
        self._acc_poll_spin.set_sensitive(has_poll_override)
        grid.attach(self._acc_poll_spin, 1, row, 1, 1)

        self._acc_poll_override = Gtk.CheckButton()
        self._acc_poll_override.set_active(has_poll_override)
        self._acc_poll_override.set_tooltip_text("Override default")
        self._acc_poll_override.connect(
            "toggled", lambda chk: self._acc_poll_spin.set_sensitive(chk.get_active()))
        grid.attach(self._acc_poll_override, 2, row, 1, 1)

        row += 1

        # ── Debounce ──
        debounce_label = Gtk.Label(label="Local change debounce (seconds):")
        debounce_label.set_xalign(0)
        grid.attach(debounce_label, 0, row, 1, 1)

        self._acc_debounce_spin = Gtk.SpinButton.new_with_range(1, 30, 1)
        self._acc_debounce_spin.set_value(
            acc_debounce if acc_debounce is not None else global_debounce)
        self._acc_debounce_spin.set_sensitive(has_debounce_override)
        grid.attach(self._acc_debounce_spin, 1, row, 1, 1)

        self._acc_debounce_override = Gtk.CheckButton()
        self._acc_debounce_override.set_active(has_debounce_override)
        self._acc_debounce_override.set_tooltip_text("Override default")
        self._acc_debounce_override.connect(
            "toggled", lambda chk: self._acc_debounce_spin.set_sensitive(chk.get_active()))
        grid.attach(self._acc_debounce_override, 2, row, 1, 1)

        row += 1

        # ── Conflict resolution ──
        conflict_label = Gtk.Label(label="Conflict resolution:")
        conflict_label.set_xalign(0)
        grid.attach(conflict_label, 0, row, 1, 1)

        self._acc_conflict_combo = Gtk.ComboBoxText()
        self._acc_conflict_combo.append_text("Newer wins (save .conflict copy)")
        self._acc_conflict_combo.append_text("Always keep local")
        self._acc_conflict_combo.append_text("Always keep remote")
        conflict_mode = account_data.get("conflict_resolution", "newer_wins")
        idx = {"newer_wins": 0, "keep_local": 1, "keep_remote": 2}.get(conflict_mode, 0)
        self._acc_conflict_combo.set_active(idx)
        self._acc_conflict_combo.set_sensitive(has_conflict_override)
        grid.attach(self._acc_conflict_combo, 1, row, 1, 1)

        self._acc_conflict_override = Gtk.CheckButton()
        self._acc_conflict_override.set_active(has_conflict_override)
        self._acc_conflict_override.set_tooltip_text("Override default")
        self._acc_conflict_override.connect(
            "toggled",
            lambda chk: self._acc_conflict_combo.set_sensitive(chk.get_active()))
        grid.attach(self._acc_conflict_override, 2, row, 1, 1)

        row += 1

        # ── Max file size ──
        size_label = Gtk.Label(label="Max file size (MB, 0 = no limit):")
        size_label.set_xalign(0)
        grid.attach(size_label, 0, row, 1, 1)

        self._acc_max_size_spin = Gtk.SpinButton.new_with_range(0, 10000, 10)
        self._acc_max_size_spin.set_value(account_data.get("max_file_size_mb", 0))
        has_size_override = account_data.get("max_file_size_mb", 0) > 0
        self._acc_max_size_spin.set_sensitive(has_size_override)
        grid.attach(self._acc_max_size_spin, 1, row, 1, 1)

        self._acc_max_size_override = Gtk.CheckButton()
        self._acc_max_size_override.set_active(has_size_override)
        self._acc_max_size_override.set_tooltip_text("Override default")
        self._acc_max_size_override.connect(
            "toggled",
            lambda chk: self._acc_max_size_spin.set_sensitive(chk.get_active()))
        grid.attach(self._acc_max_size_override, 2, row, 1, 1)

        row += 1

        # ── Bandwidth throttling ──
        bw_label = Gtk.Label(label="Bandwidth limit:")
        bw_label.set_xalign(0)
        grid.attach(bw_label, 0, row, 1, 1)

        # Slider with discrete stops: 100K, 256K, 512K, 1M, 2M, 4M, 8M, 12M, 16M, 20M, Unlimited
        bw_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._bw_values = [100, 256, 512, 1024, 2048, 4096, 8192, 12288, 16384, 20480, 0]  # 0 = unlimited
        self._acc_bw_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, len(self._bw_values) - 1, 1)
        self._acc_bw_scale.set_draw_value(False)
        self._acc_bw_scale.set_hexpand(True)
        self._acc_bw_scale.set_size_request(180, -1)

        # Find current value's index
        current_bw = account_data.get("bandwidth_limit_kib", 0)
        if current_bw in self._bw_values:
            bw_idx = self._bw_values.index(current_bw)
        else:
            bw_idx = len(self._bw_values) - 1  # Default to unlimited
        self._acc_bw_scale.set_value(bw_idx)

        self._acc_bw_label = Gtk.Label()
        self._acc_bw_label.set_width_chars(12)
        self._update_bw_label(bw_idx)

        def _on_bw_changed(scale):
            idx = int(scale.get_value())
            self._update_bw_label(idx)

        self._acc_bw_scale.connect("value-changed", _on_bw_changed)

        has_bw_override = current_bw != 0  # 0 means unlimited (default)
        self._acc_bw_scale.set_sensitive(has_bw_override)
        self._acc_bw_label.set_sensitive(has_bw_override)

        bw_box.pack_start(self._acc_bw_scale, True, True, 0)
        bw_box.pack_start(self._acc_bw_label, False, False, 0)
        grid.attach(bw_box, 1, row, 1, 1)

        self._acc_bw_override = Gtk.CheckButton()
        self._acc_bw_override.set_active(has_bw_override)
        self._acc_bw_override.set_tooltip_text("Override default (unlimited)")
        self._acc_bw_override.connect(
            "toggled",
            lambda chk: (self._acc_bw_scale.set_sensitive(chk.get_active()),
                        self._acc_bw_label.set_sensitive(chk.get_active())))
        grid.attach(self._acc_bw_override, 2, row, 1, 1)

        vbox.pack_start(grid, False, False, 0)

        # Save
        save_btn = Gtk.Button(label="Save Sync Settings")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", lambda _: self._on_save_account_sync(account_data))
        vbox.pack_end(save_btn, False, False, 12)

        return vbox

    def _update_bw_label(self, idx: int):
        """Update the bandwidth label based on slider position."""
        value = self._bw_values[idx]
        if value == 0:
            self._acc_bw_label.set_text("Unlimited")
        elif value >= 1024:
            self._acc_bw_label.set_text(f"{value // 1024} MiB/s")
        else:
            self._acc_bw_label.set_text(f"{value} KiB/s")

    def _on_save_account_sync(self, account_data: dict):
        """Save per-account sync settings (only overridden values)."""
        acc_name = account_data["name"]
        global_poll = self._config_data.get("default_poll_interval", 30)
        global_debounce = self._config_data.get("default_debounce", 5)

        for acc in self._config_data.get("accounts", []):
            if acc["name"] == acc_name:
                # Poll interval
                if self._acc_poll_override.get_active():
                    acc["poll_interval"] = int(self._acc_poll_spin.get_value())
                else:
                    acc["poll_interval"] = global_poll

                # Debounce
                if self._acc_debounce_override.get_active():
                    acc["debounce"] = int(self._acc_debounce_spin.get_value())
                else:
                    acc["debounce"] = global_debounce

                # Conflict resolution
                if self._acc_conflict_override.get_active():
                    conflict_idx = self._acc_conflict_combo.get_active()
                    acc["conflict_resolution"] = [
                        "newer_wins", "keep_local", "keep_remote"][conflict_idx]
                else:
                    acc.pop("conflict_resolution", None)

                # Max file size
                if self._acc_max_size_override.get_active():
                    acc["max_file_size_mb"] = int(self._acc_max_size_spin.get_value())
                else:
                    acc["max_file_size_mb"] = 0

                # Bandwidth limit
                if self._acc_bw_override.get_active():
                    bw_idx = int(self._acc_bw_scale.get_value())
                    acc["bandwidth_limit_kib"] = self._bw_values[bw_idx]
                else:
                    acc["bandwidth_limit_kib"] = 0  # Unlimited

                break

        self._save_config()
        self._show_info("Sync settings saved. Restart SyncoPath to apply.")

    # ─── Per-Account: Exclusions Tab ───────────────────────────────────────

    def _build_account_exclusions_tab(self, account_data: dict) -> Gtk.Box:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        # Determine if account has custom exclusions vs global default
        global_exclude = self._config_data.get(
            "default_exclude", ["*.tmp", ".Trash*", ".syncopath*"])
        acc_exclude = account_data.get("exclude", [])
        has_override = (acc_exclude != global_exclude and len(acc_exclude) > 0)

        # Override header row
        override_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        desc = Gtk.Label()
        desc.set_markup(
            "Glob patterns for files/folders to <b>exclude</b> from sync.\n"
            "One pattern per line.")
        desc.set_xalign(0)
        desc.set_line_wrap(True)
        override_box.pack_start(desc, True, True, 0)

        self._acc_exclusions_override = Gtk.CheckButton()
        self._acc_exclusions_override.set_active(has_override)
        self._acc_exclusions_override.set_tooltip_text("Override default")
        override_box.pack_end(self._acc_exclusions_override, False, False, 0)

        vbox.pack_start(override_box, False, False, 0)

        # Text view with current exclusions
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(180)
        self._acc_exclusions_text = Gtk.TextView()
        self._acc_exclusions_text.set_monospace(True)
        self._acc_exclusions_text.set_left_margin(8)
        self._acc_exclusions_text.set_top_margin(8)
        self._acc_exclusions_text.set_sensitive(has_override)

        # Show account patterns if overridden, else global defaults (greyed out)
        excludes = acc_exclude if has_override else global_exclude
        buffer = self._acc_exclusions_text.get_buffer()
        buffer.set_text("\n".join(excludes))

        scroll.add(self._acc_exclusions_text)
        frame.add(scroll)
        vbox.pack_start(frame, True, True, 0)

        # Quick-add buttons
        self._acc_quick_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._acc_quick_box.pack_start(Gtk.Label(label="Quick add:"), False, False, 0)
        for pattern, tooltip in [
            ("node_modules", "Node.js deps"),
            (".git", "Git repos"),
            ("*.log", "Log files"),
            ("__pycache__", "Python cache"),
            ("*.iso", "ISO images"),
        ]:
            btn = Gtk.Button(label=pattern)
            btn.set_tooltip_text(tooltip)
            btn.set_sensitive(has_override)
            btn.connect("clicked", lambda b, p=pattern: self._add_exclusion_to_account(p))
            self._acc_quick_box.pack_start(btn, False, False, 0)
        vbox.pack_start(self._acc_quick_box, False, False, 0)

        # Wire up override toggle
        def _on_override_toggled(chk):
            active = chk.get_active()
            self._acc_exclusions_text.set_sensitive(active)
            for child in self._acc_quick_box.get_children():
                if isinstance(child, Gtk.Button):
                    child.set_sensitive(active)
            if not active:
                # Reset to global defaults display
                buffer = self._acc_exclusions_text.get_buffer()
                buffer.set_text("\n".join(global_exclude))

        self._acc_exclusions_override.connect("toggled", _on_override_toggled)

        # Save
        save_btn = Gtk.Button(label="Save Exclusions")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", lambda _: self._on_save_account_exclusions(account_data))
        vbox.pack_end(save_btn, False, False, 12)

        return vbox

    def _add_exclusion_to_account(self, pattern: str):
        """Add a pattern to the account exclusions text."""
        buffer = self._acc_exclusions_text.get_buffer()
        end = buffer.get_end_iter()
        text = buffer.get_text(buffer.get_start_iter(), end, False)
        if pattern not in text.split("\n"):
            buffer.insert(end, f"\n{pattern}")

    def _on_save_account_exclusions(self, account_data: dict):
        """Save per-account exclusion patterns (or revert to global)."""
        acc_name = account_data["name"]
        global_exclude = self._config_data.get(
            "default_exclude", ["*.tmp", ".Trash*", ".syncopath*"])

        for acc in self._config_data.get("accounts", []):
            if acc["name"] == acc_name:
                if self._acc_exclusions_override.get_active():
                    buffer = self._acc_exclusions_text.get_buffer()
                    text = buffer.get_text(
                        buffer.get_start_iter(), buffer.get_end_iter(), False)
                    patterns = [l.strip() for l in text.split("\n") if l.strip()]
                    acc["exclude"] = patterns
                else:
                    # Use global default
                    acc["exclude"] = list(global_exclude)
                break

        self._save_config()
        self._show_info("Exclusions saved. Restart SyncoPath to apply.")

    # ─── Per-Account: Issues Tab ───────────────────────────────────────────

    def _build_account_issues_tab(self, account_data: dict) -> Gtk.Box:
        """Build the Issues tab showing sync problems."""
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        # Header with refresh button
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        desc = Gtk.Label()
        desc.set_markup(
            "<b>Sync Issues</b>\n"
            "<small>Files with problems that need attention</small>")
        desc.set_xalign(0)
        header_box.pack_start(desc, True, True, 0)

        self._issues_spinner = Gtk.Spinner()
        header_box.pack_start(self._issues_spinner, False, False, 0)

        refresh_btn = Gtk.Button()
        refresh_btn.set_image(Gtk.Image.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON))
        refresh_btn.set_tooltip_text("Refresh issues")
        refresh_btn.connect("clicked", lambda _: self._refresh_issues(account_data))
        header_box.pack_start(refresh_btn, False, False, 0)

        vbox.pack_start(header_box, False, False, 0)

        # Issues list in a scrolled window
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(280)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._issues_listbox = Gtk.ListBox()
        self._issues_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.add(self._issues_listbox)
        frame.add(scroll)
        vbox.pack_start(frame, True, True, 0)

        # Status label
        self._issues_status = Gtk.Label()
        self._issues_status.set_xalign(0)
        self._issues_status.set_markup("<small><i>Click refresh to scan for issues</i></small>")
        vbox.pack_start(self._issues_status, False, False, 0)

        # Action buttons
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        clean_all_btn = Gtk.Button(label="Clean All Orphans")
        clean_all_btn.set_tooltip_text("Delete all local files whose cloud source is gone")
        clean_all_btn.connect("clicked", lambda _: self._clean_all_orphans(account_data))
        action_box.pack_start(clean_all_btn, False, False, 0)

        clean_failed_btn = Gtk.Button(label="Clean Failed Exports")
        clean_failed_btn.set_tooltip_text("Delete 0-byte files from failed exports")
        clean_failed_btn.connect("clicked", lambda _: self._clean_failed_exports(account_data))
        action_box.pack_start(clean_failed_btn, False, False, 0)

        vbox.pack_start(action_box, False, False, 0)

        # Auto-refresh on tab display
        self._refresh_issues(account_data)

        return vbox

    def _refresh_issues(self, account_data: dict):
        """Scan for sync issues in background."""
        self._issues_spinner.start()
        self._issues_status.set_markup("<small><i>Scanning...</i></small>")

        # Clear existing rows
        for child in self._issues_listbox.get_children():
            self._issues_listbox.remove(child)

        def _scan():
            issues = []
            try:
                from .drive_api import DriveAPI
                from .state import StateDB
                from pathlib import Path

                config = AppConfig.load()
                acc = next(a for a in config.accounts if a.name == account_data["name"])
                api = DriveAPI(acc.name, acc.client_id, acc.client_secret)
                state_db = StateDB(acc.name)
                local_root = Path(acc.local_path)

                # Scan for 0-byte files
                for f in local_root.rglob("*"):
                    if not f.is_file() or f.stat().st_size > 0:
                        continue
                    rel_path = str(f.relative_to(local_root))
                    state = state_db.get_by_path(rel_path)
                    if not state:
                        continue

                    try:
                        meta = api.service.files().get(
                            fileId=state.file_id,
                            fields='id,mimeType,size,trashed'
                        ).execute()

                        cloud_mime = meta.get('mimeType', '')
                        cloud_size = int(meta.get('size', 0) or 0)

                        if meta.get('trashed'):
                            issues.append({
                                'path': rel_path,
                                'type': 'orphan',
                                'reason': 'Cloud file trashed',
                                'file_id': state.file_id,
                            })
                        elif cloud_mime.startswith('application/vnd.google-apps.drive-sdk'):
                            issues.append({
                                'path': rel_path,
                                'type': 'unsupported',
                                'reason': 'Third-party app file (not exportable)',
                                'file_id': state.file_id,
                            })
                        elif cloud_mime.startswith('application/vnd.google-apps.') and cloud_size > 0:
                            issues.append({
                                'path': rel_path,
                                'type': 'export_failed',
                                'reason': f'Export failed (cloud: {cloud_size} bytes)',
                                'file_id': state.file_id,
                            })
                        elif cloud_size == 0:
                            # Genuinely empty in cloud — not an issue
                            pass
                        else:
                            issues.append({
                                'path': rel_path,
                                'type': 'download_failed',
                                'reason': f'Download failed (cloud: {cloud_size} bytes)',
                                'file_id': state.file_id,
                            })
                    except Exception as e:
                        if "404" in str(e) or "notFound" in str(e):
                            issues.append({
                                'path': rel_path,
                                'type': 'orphan',
                                'reason': 'Cloud file not found (404)',
                                'file_id': state.file_id,
                            })

                GLib.idle_add(self._on_issues_loaded, issues, None, account_data)
            except Exception as e:
                GLib.idle_add(self._on_issues_loaded, [], str(e), account_data)

        threading.Thread(target=_scan, daemon=True).start()

    def _on_issues_loaded(self, issues: list, error: str, account_data: dict):
        """Populate issues list (main thread)."""
        self._issues_spinner.stop()

        if error:
            self._issues_status.set_markup(f"<small><i>Error: {error}</i></small>")
            self._issues_tab_label.set_text("Issues")
            return False

        # Update tab label with count
        if issues:
            self._issues_tab_label.set_text(f"Issues ({len(issues)})")
        else:
            self._issues_tab_label.set_text("Issues")

        if not issues:
            self._issues_status.set_markup("<small><i>No issues found ✓</i></small>")
            return False

        self._issues_status.set_markup(f"<small><i>{len(issues)} issue(s) found</i></small>")

        # Type icons
        type_icons = {
            'orphan': '🗑️',
            'export_failed': '⚠️',
            'download_failed': '❌',
            'unsupported': '🚫',
        }

        for issue in issues[:100]:  # Limit display to 100
            row = Gtk.ListBoxRow()
            row.issue_data = issue

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hbox.set_margin_top(4)
            hbox.set_margin_bottom(4)
            hbox.set_margin_start(8)
            hbox.set_margin_end(8)

            # Icon
            icon = Gtk.Label(label=type_icons.get(issue['type'], '?'))
            hbox.pack_start(icon, False, False, 0)

            # Path and reason
            info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            path_label = Gtk.Label()
            path_label.set_markup(f"<b>{GLib.markup_escape_text(issue['path'])}</b>")
            path_label.set_xalign(0)
            path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            path_label.set_max_width_chars(50)
            info_box.pack_start(path_label, False, False, 0)

            reason_label = Gtk.Label()
            reason_label.set_markup(f"<small>{GLib.markup_escape_text(issue['reason'])}</small>")
            reason_label.set_xalign(0)
            reason_label.set_opacity(0.7)
            info_box.pack_start(reason_label, False, False, 0)

            hbox.pack_start(info_box, True, True, 0)

            # Delete button
            del_btn = Gtk.Button()
            del_btn.set_image(Gtk.Image.new_from_icon_name(
                "edit-delete-symbolic", Gtk.IconSize.BUTTON))
            del_btn.set_tooltip_text("Delete local file")
            del_btn.connect("clicked", lambda b, i=issue, a=account_data: self._delete_issue_file(i, a))
            hbox.pack_end(del_btn, False, False, 0)

            row.add(hbox)
            self._issues_listbox.add(row)

        self._issues_listbox.show_all()
        return False

    def _delete_issue_file(self, issue: dict, account_data: dict):
        """Delete a single issue file."""
        from .state import StateDB
        from pathlib import Path

        config = AppConfig.load()
        acc = next(a for a in config.accounts if a.name == account_data["name"])
        local_root = Path(acc.local_path)
        state_db = StateDB(acc.name)

        local_path = local_root / issue['path']
        if local_path.exists():
            local_path.unlink()
        state_db.delete_by_path(issue['path'])

        # Refresh list
        self._refresh_issues(account_data)

    def _clean_all_orphans(self, account_data: dict):
        """Delete all orphaned files."""
        count = 0
        for child in self._issues_listbox.get_children():
            if hasattr(child, 'issue_data') and child.issue_data.get('type') == 'orphan':
                count += 1

        if count == 0:
            self._show_info("No orphaned files to clean.")
            return

        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Delete {count} orphaned file(s)?")
        dialog.format_secondary_text("These local files have no cloud source.")
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            for child in self._issues_listbox.get_children():
                if hasattr(child, 'issue_data') and child.issue_data.get('type') == 'orphan':
                    self._delete_issue_file(child.issue_data, account_data)
            self._refresh_issues(account_data)

    def _clean_failed_exports(self, account_data: dict):
        """Delete all failed export files."""
        count = 0
        for child in self._issues_listbox.get_children():
            if hasattr(child, 'issue_data') and child.issue_data.get('type') in ('export_failed', 'unsupported'):
                count += 1

        if count == 0:
            self._show_info("No failed export files to clean.")
            return

        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Delete {count} failed export file(s)?")
        dialog.format_secondary_text("These are 0-byte files from exports that couldn't complete.")
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            for child in self._issues_listbox.get_children():
                if hasattr(child, 'issue_data') and child.issue_data.get('type') in ('export_failed', 'unsupported'):
                    self._delete_issue_file(child.issue_data, account_data)
            self._refresh_issues(account_data)

    # ─── Global Settings Tab ───────────────────────────────────────────────

    def _build_global_settings_tab(self) -> Gtk.Box:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        vbox.set_margin_top(16)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)

        # ── Section: Defaults for new accounts ──
        defaults_label = Gtk.Label()
        defaults_label.set_markup("<b>Account Defaults</b>")
        defaults_label.set_xalign(0)
        vbox.pack_start(defaults_label, False, False, 0)

        defaults_desc = Gtk.Label()
        defaults_desc.set_markup(
            "<small><i>Default values for newly created accounts. "
            "Existing accounts keep their own settings.</i></small>")
        defaults_desc.set_xalign(0)
        defaults_desc.set_opacity(0.7)
        vbox.pack_start(defaults_desc, False, False, 0)

        # Default poll interval
        poll_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        poll_box.pack_start(Gtk.Label(label="Default poll interval (s):"), False, False, 0)
        self._global_poll_spin = Gtk.SpinButton.new_with_range(10, 300, 5)
        self._global_poll_spin.set_value(
            self._config_data.get("default_poll_interval", 30))
        poll_box.pack_start(self._global_poll_spin, False, False, 0)
        vbox.pack_start(poll_box, False, False, 0)

        # Default debounce
        debounce_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        debounce_box.pack_start(Gtk.Label(label="Default debounce (s):"), False, False, 0)
        self._global_debounce_spin = Gtk.SpinButton.new_with_range(1, 30, 1)
        self._global_debounce_spin.set_value(
            self._config_data.get("default_debounce", 5))
        debounce_box.pack_start(self._global_debounce_spin, False, False, 0)
        vbox.pack_start(debounce_box, False, False, 0)

        # Default exclusions
        excl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        excl_box.pack_start(Gtk.Label(label="Default exclusions:"), False, False, 0)
        self._global_exclusions_entry = Gtk.Entry()
        default_excl = self._config_data.get(
            "default_exclude", ["*.tmp", ".Trash*", ".syncopath*"])
        self._global_exclusions_entry.set_text(", ".join(default_excl))
        self._global_exclusions_entry.set_tooltip_text("Comma-separated glob patterns")
        excl_box.pack_start(self._global_exclusions_entry, True, True, 0)
        vbox.pack_start(excl_box, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 8)

        # ── Section: Application Settings ──
        app_label = Gtk.Label()
        app_label.set_markup("<b>Application Settings</b>")
        app_label.set_xalign(0)
        vbox.pack_start(app_label, False, False, 0)

        # Log level
        log_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        log_box.pack_start(Gtk.Label(label="Log level:"), False, False, 0)
        self._log_combo = Gtk.ComboBoxText()
        for level in ["DEBUG", "INFO", "WARNING", "ERROR"]:
            self._log_combo.append_text(level)
        current_level = self._config_data.get("log_level", "INFO")
        self._log_combo.set_active(
            ["DEBUG", "INFO", "WARNING", "ERROR"].index(current_level))
        log_box.pack_start(self._log_combo, False, False, 0)
        vbox.pack_start(log_box, False, False, 0)

        # Icon theme
        theme_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        theme_box.pack_start(Gtk.Label(label="Icon theme:"), False, False, 0)
        self._theme_combo = Gtk.ComboBoxText()
        for theme in ["dark", "light", "auto"]:
            self._theme_combo.append_text(theme)
        current_theme = self._config_data.get("icon_theme", "dark")
        self._theme_combo.set_active(["dark", "light", "auto"].index(current_theme))
        theme_box.pack_start(self._theme_combo, False, False, 0)
        vbox.pack_start(theme_box, False, False, 0)

        # Safe mode
        self._safe_mode_check = Gtk.CheckButton(
            label="Safe mode (no deletions on either side)")
        self._safe_mode_check.set_active(self._config_data.get("safe_mode", True))
        vbox.pack_start(self._safe_mode_check, False, False, 0)

        # Min free space
        space_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        space_box.pack_start(
            Gtk.Label(label="Minimum free disk space (GB):"), False, False, 0)
        self._min_space_spin = Gtk.SpinButton.new_with_range(1, 100, 1)
        self._min_space_spin.set_value(
            self._config_data.get("min_free_space_gb", 5))
        space_box.pack_start(self._min_space_spin, False, False, 0)
        vbox.pack_start(space_box, False, False, 0)

        # Autostart
        self._autostart_check = Gtk.CheckButton(
            label="Start automatically at login (systemd)")
        self._autostart_check.set_active(self._is_service_enabled())
        self._autostart_check.connect("toggled", self._on_toggle_autostart)
        vbox.pack_start(self._autostart_check, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 8)

        # ── Section: Maintenance ──
        maint_label = Gtk.Label()
        maint_label.set_markup("<b>Maintenance</b>")
        maint_label.set_xalign(0)
        vbox.pack_start(maint_label, False, False, 0)

        maint_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reset_btn = Gtk.Button(label="Reset Sync State")
        reset_btn.get_style_context().add_class("destructive-action")
        reset_btn.set_tooltip_text("Delete state DB and force full resync")
        reset_btn.connect("clicked", self._on_reset_state)
        maint_box.pack_start(reset_btn, False, False, 0)

        log_btn = Gtk.Button(label="View Logs")
        log_btn.connect("clicked", self._on_view_logs)
        maint_box.pack_start(log_btn, False, False, 0)
        vbox.pack_start(maint_box, False, False, 0)

        # Config path
        path_label = Gtk.Label()
        path_label.set_markup(f"<small>Config: <tt>{CONFIG_FILE}</tt></small>")
        path_label.set_xalign(0)
        path_label.set_opacity(0.5)
        vbox.pack_end(path_label, False, False, 0)

        # Save button
        save_btn = Gtk.Button(label="Save Global Settings")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", self._on_save_global)
        vbox.pack_end(save_btn, False, False, 8)

        return vbox

    def _on_save_global(self, _btn):
        """Save global settings."""
        self._config_data["log_level"] = self._log_combo.get_active_text()
        self._config_data["icon_theme"] = self._theme_combo.get_active_text()
        self._config_data["safe_mode"] = self._safe_mode_check.get_active()
        self._config_data["min_free_space_gb"] = int(self._min_space_spin.get_value())
        self._config_data["default_poll_interval"] = int(
            self._global_poll_spin.get_value())
        self._config_data["default_debounce"] = int(
            self._global_debounce_spin.get_value())

        # Parse exclusions
        excl_text = self._global_exclusions_entry.get_text().strip()
        self._config_data["default_exclude"] = [
            p.strip() for p in excl_text.split(",") if p.strip()]

        self._save_config()
        self._show_info("Global settings saved. Restart SyncoPath to apply.")

    def _is_service_enabled(self) -> bool:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "syncopath"],
            capture_output=True, text=True)
        return result.stdout.strip() == "enabled"

    def _on_toggle_autostart(self, check):
        if check.get_active():
            subprocess.run(
                ["systemctl", "--user", "enable", "syncopath"], capture_output=True)
        else:
            subprocess.run(
                ["systemctl", "--user", "disable", "syncopath"], capture_output=True)

    def _on_reset_state(self, _btn):
        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Reset all sync state?")
        dialog.format_secondary_text(
            "This will delete the state database and force a full resync on next start. "
            "No local files will be deleted.")
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            import shutil
            if STATE_DIR.exists():
                shutil.rmtree(STATE_DIR)
                STATE_DIR.mkdir(parents=True)
            self._show_info("State cleared. Restart SyncoPath for a full resync.")

    def _on_view_logs(self, _btn):
        terminals = ["xfce4-terminal", "kitty", "gnome-terminal", "xterm"]
        cmd = "journalctl --user -u syncopath -f"
        for term in terminals:
            try:
                if term == "xfce4-terminal":
                    subprocess.Popen([term, "-e", cmd])
                elif term == "kitty":
                    subprocess.Popen([term, "sh", "-c", cmd])
                else:
                    subprocess.Popen([term, "-e", cmd])
                return
            except FileNotFoundError:
                continue

    # ─── Account List Management ───────────────────────────────────────────

    def _refresh_account_list(self):
        """Refresh the accounts listbox."""
        for child in self._accounts_listbox.get_children():
            self._accounts_listbox.remove(child)

        # Only show non-shared-drive accounts in the main list
        for acc in self._config_data.get("accounts", []):
            if not acc.get("shared_drive_id"):
                row = AccountRow(acc)
                self._accounts_listbox.add(row)

        self._accounts_listbox.show_all()

    def _on_add_account(self, _btn):
        """Add a new account via dialog."""
        dialog = Gtk.Dialog(
            title="Add Account", parent=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)
        dialog.set_default_size(400, 200)

        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(12)

        # Name
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_box.pack_start(Gtk.Label(label="Account name:"), False, False, 0)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("e.g. work")
        name_box.pack_start(name_entry, True, True, 0)
        content.pack_start(name_box, False, False, 0)

        # Local path
        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        path_box.pack_start(Gtk.Label(label="Local folder:"), False, False, 0)
        path_entry = Gtk.Entry()
        path_entry.set_placeholder_text(str(default_account_path("my-account")))
        path_box.pack_start(path_entry, True, True, 0)

        def _on_name_changed(entry):
            name = entry.get_text().strip()
            if name:
                path_entry.set_text(str(default_account_path(name)))
            else:
                path_entry.set_text("")

        name_entry.connect("changed", _on_name_changed)

        browse_btn = Gtk.Button(label="Browse")

        def _browse(_b):
            chooser = Gtk.FileChooserDialog(
                title="Select sync folder", parent=dialog,
                action=Gtk.FileChooserAction.SELECT_FOLDER)
            chooser.add_button("Cancel", Gtk.ResponseType.CANCEL)
            chooser.add_button("Select", Gtk.ResponseType.OK)
            if chooser.run() == Gtk.ResponseType.OK:
                path_entry.set_text(chooser.get_filename())
            chooser.destroy()

        browse_btn.connect("clicked", _browse)
        path_box.pack_start(browse_btn, False, False, 0)
        content.pack_start(path_box, False, False, 0)

        content.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            name = name_entry.get_text().strip()
            local_path = path_entry.get_text().strip()
            if name and local_path:
                target = Path(local_path)
                try:
                    validate_sync_dir_empty(target)
                except ValueError as e:
                    self._show_info(f"❌ {e}")
                    dialog.destroy()
                    return

                # Use global defaults
                default_poll = self._config_data.get("default_poll_interval", 30)
                default_debounce = self._config_data.get("default_debounce", 5)
                default_exclude = self._config_data.get(
                    "default_exclude", ["*.tmp", ".Trash*", ".syncopath*"])

                new_acc = {
                    "name": sanitize_dirname(name),
                    "local_path": local_path,
                    "remote_id": "root",
                    "poll_interval": default_poll,
                    "debounce": default_debounce,
                    "exclude": list(default_exclude),
                    "include_shared_with_me": False,
                }
                self._config_data.setdefault("accounts", []).append(new_acc)
                self._save_config()
                self._refresh_account_list()

        dialog.destroy()

    def _on_remove_account(self, _btn):
        """Remove selected account."""
        row = self._accounts_listbox.get_selected_row()
        if not row:
            return

        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Remove account '{row.data['name']}'?")
        dialog.format_secondary_text("This won't delete any local files.")
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            self._config_data["accounts"] = [
                a for a in self._config_data["accounts"]
                if a["name"] != row.data["name"]
            ]
            self._save_config()
            self._refresh_account_list()
            # Hide right panel
            self._account_notebook.hide()
            self._no_account_label.show()

    # ─── Helpers ───────────────────────────────────────────────────────────

    def _show_info(self, message: str):
        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=message)
        dialog.run()
        dialog.destroy()


def show_preferences(on_save_callback=None):
    """Show the preferences window (standalone or from tray)."""
    win = PreferencesWindow(on_save_callback)
    win.connect("destroy", lambda _: None)
    win.show_all()
    return win
