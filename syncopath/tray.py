"""GTK system tray icon for SyncoPath."""
import logging
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gtk, GLib, GdkPixbuf, Pango

# Try AppIndicator first (works on XFCE), fall back to StatusIcon
try:
    from gi.repository import AppIndicator3
    HAS_APPINDICATOR = True
except (ImportError, ValueError):
    HAS_APPINDICATOR = False

log = logging.getLogger(__name__)

# Icon names (from system icon theme)
ICON_IDLE = "syncopath"           # Custom icon (default)
ICON_IDLE_LIGHT = "syncopath-light"  # For light panels
ICON_IDLE_DARK = "syncopath-dark"    # For dark panels
ICON_ERROR = "dialog-warning"
ICON_PAUSED = "media-playback-pause"

# Syncing animation frames (smooth rotation - 12 frames at 30° each)
ICON_SYNCING_FRAMES = [f"syncopath-sync-{i:02d}" for i in range(12)]

# Working/scanning animation frames (pulse - 4 frames)
ICON_WORKING_FRAMES = [f"syncopath-work-{i:02d}" for i in range(4)]

# Fallback to generic icons if sync icons not available
ICON_FALLBACK = {
    "idle": "folder",
    "syncing": "view-refresh",
    "error": "dialog-warning",
    "paused": "media-playback-pause",
}

# Source SVG directory
SVG_ICON_DIR = Path.home() / ".local/share/icons/hicolor/scalable/apps"

# Tray icon render size (pixels)
TRAY_ICON_SIZE = 24


def _prerender_icons(icon_theme: str) -> Path:
    """Pre-render SVG icons to PNG in a temp directory using GdkPixbuf/librsvg.

    This avoids glycin's buggy SVG renderer and uses the stable librsvg path
    via GdkPixbuf. The temp directory is used as the icon theme path for
    AppIndicator3.

    Returns the temp directory path containing the rendered PNGs.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="syncopath-icons-"))

    # Collect all SVGs we need to render
    svg_names = [ICON_IDLE, ICON_IDLE_LIGHT, ICON_IDLE_DARK] + ICON_SYNCING_FRAMES + ICON_WORKING_FRAMES

    rendered = 0
    for name in svg_names:
        svg_path = SVG_ICON_DIR / f"{name}.svg"
        if not svg_path.exists():
            continue

        png_path = tmp_dir / f"{name}.png"
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                str(svg_path), TRAY_ICON_SIZE, TRAY_ICON_SIZE)
            pixbuf.savev(str(png_path), "png", [], [])
            rendered += 1
        except Exception as e:
            log.warning("Failed to render %s: %s", svg_path.name, e)

    log.debug("Pre-rendered %d icons to %s", rendered, tmp_dir)
    return tmp_dir


class TrayIcon:
    """System tray icon with status, animation, and progress."""

    def __init__(self, icon_theme: str = "dark"):
        self._status = "idle"
        self._detail = ""
        self._paused = False
        self._on_pause: Optional[Callable] = None
        self._on_resume: Optional[Callable] = None
        self._on_sync_now: Optional[Callable] = None
        self._on_quit: Optional[Callable] = None
        self._on_open_folder: Optional[Callable] = None
        self._indicator = None
        self._status_icon = None
        self._menu = None

        # Icon theme: light, dark, or auto
        self._icon_theme = icon_theme
        if icon_theme == "light":
            self._idle_icon = ICON_IDLE_LIGHT
        elif icon_theme == "dark":
            self._idle_icon = ICON_IDLE_DARK
        else:
            self._idle_icon = ICON_IDLE

        # Pre-render SVG icons to PNG via librsvg (avoids glycin crashes)
        self._icon_dir = _prerender_icons(icon_theme)

        # Animation state
        self._anim_frame = 0
        self._anim_timeout_id = None

        # Progress tracking
        self._progress_total = 0
        self._progress_current = 0
        self._current_file = ""

    def set_callbacks(
        self,
        on_pause: Callable = None,
        on_resume: Callable = None,
        on_sync_now: Callable = None,
        on_quit: Callable = None,
        on_open_folder: Callable = None,
        on_preferences: Callable = None,
    ):
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_sync_now = on_sync_now
        self._on_quit = on_quit
        self._on_open_folder = on_open_folder
        self._on_preferences = on_preferences

    def start(self):
        """Initialize the tray icon (must be called from main thread)."""
        self._build_menu()

        if HAS_APPINDICATOR:
            self._indicator = AppIndicator3.Indicator.new(
                "syncopath",
                self._get_icon_name(),
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            # Set custom icon path so our animation frames are found (use PNG to avoid glycin SVG crashes)
            self._indicator.set_icon_theme_path(str(self._icon_dir))
            self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._indicator.set_menu(self._menu)
            self._indicator.set_title("SyncoPath")
        else:
            # Fallback to StatusIcon (deprecated but works)
            self._status_icon = Gtk.StatusIcon()
            self._status_icon.set_from_icon_name(self._get_icon_name())
            self._status_icon.set_tooltip_text("SyncoPath — Idle")
            self._status_icon.set_visible(True)
            self._status_icon.connect("popup-menu", self._on_popup)

        log.info("Tray icon started")

    def update_status(self, status: str, detail: str = ""):
        """Update tray icon status. Thread-safe."""
        GLib.idle_add(self._do_update_status, status, detail)

    def update_progress(self, current: int, total: int, current_file: str = "",
                        file_size: int = 0):
        """Update sync progress. Thread-safe."""
        GLib.idle_add(self._do_update_progress, current, total, current_file, file_size)

    def _do_update_status(self, status: str, detail: str):
        """Update status on GTK main thread."""
        old_status = self._status
        self._status = status
        self._detail = detail
        icon_name = self._get_icon_name()
        tooltip = self._get_tooltip()

        if HAS_APPINDICATOR and self._indicator:
            self._indicator.set_icon_full(icon_name, tooltip)
        elif self._status_icon:
            self._status_icon.set_from_icon_name(icon_name)
            self._status_icon.set_tooltip_text(tooltip)

        # Update menu status label
        if self._status_item:
            self._status_item.set_label(tooltip)

        # Start/stop animation
        if status in ("syncing", "working") and old_status not in ("syncing", "working"):
            self._start_animation()
        elif status not in ("syncing", "working") and old_status in ("syncing", "working"):
            self._stop_animation()
            self._hide_progress()

        return False  # Don't repeat

    def _do_update_progress(self, current: int, total: int, current_file: str,
                            file_size: int = 0):
        """Update progress on GTK main thread."""
        self._progress_current = current
        self._progress_total = total
        self._current_file = current_file

        if total > 0:
            fraction = current / total
            pct = int(fraction * 100)

            # Update progress bar
            self._progress_bar.set_fraction(fraction)
            self._progress_bar.set_text(f"{current}/{total} ({pct}%)")
            self._progress_bar.show()
            self._progress_item.show()

            # Update current file label with size
            if current_file:
                # Truncate long filenames
                display_name = current_file if len(current_file) <= 40 else \
                    "…" + current_file[-(40-1):]
                size_str = self._format_size(file_size) if file_size else ""
                label = f"  {display_name}"
                if size_str:
                    label += f"  ({size_str})"
                self._file_label_inner.set_text(label)
                self._file_label.show()
            else:
                self._file_label.hide()

            # Update tooltip
            tooltip = f"SyncoPath — Syncing {pct}%"
            if HAS_APPINDICATOR and self._indicator:
                self._indicator.set_icon_full(self._get_icon_name(), tooltip)
            elif self._status_icon:
                self._status_icon.set_tooltip_text(tooltip)
        else:
            self._hide_progress()

        return False

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format bytes to human-readable size (no decimal)."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes // 1024} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes // (1024 * 1024)} MB"
        else:
            return f"{size_bytes // (1024 * 1024 * 1024)} GB"

    def _hide_progress(self):
        """Hide progress bar and file label."""
        self._progress_bar.hide()
        self._progress_item.hide()
        self._file_label.hide()
        self._progress_current = 0
        self._progress_total = 0
        self._current_file = ""

    def _start_animation(self):
        """Start icon animation (spinning for sync, pulsing for working)."""
        if self._anim_timeout_id is not None:
            return  # Already animating
        self._anim_frame = 0
        if self._status == "syncing":
            # Fast spin: 100ms per frame × 12 frames = 1.2s per rotation
            self._anim_timeout_id = GLib.timeout_add(100, self._animate_frame)
        else:
            # Slow pulse: 400ms per frame × 4 frames = 1.6s per cycle
            self._anim_timeout_id = GLib.timeout_add(400, self._animate_frame)

    def _stop_animation(self):
        """Stop icon animation."""
        if self._anim_timeout_id is not None:
            GLib.source_remove(self._anim_timeout_id)
            self._anim_timeout_id = None
        # Reset to idle icon
        icon_name = self._get_icon_name()
        if HAS_APPINDICATOR and self._indicator:
            self._indicator.set_icon_full(icon_name, self._get_tooltip())
        elif self._status_icon:
            self._status_icon.set_from_icon_name(icon_name)

    def _animate_frame(self):
        """Advance animation by one frame."""
        if self._status not in ("syncing", "working"):
            self._anim_timeout_id = None
            return False  # Stop timer

        if self._status == "syncing":
            frames = ICON_SYNCING_FRAMES
        else:
            frames = ICON_WORKING_FRAMES

        frame_icon = frames[self._anim_frame % len(frames)]
        self._anim_frame += 1

        if HAS_APPINDICATOR and self._indicator:
            self._indicator.set_icon_full(frame_icon, self._get_tooltip())
        elif self._status_icon:
            self._status_icon.set_from_icon_name(frame_icon)

        return True  # Keep repeating

    def _get_icon_name(self) -> str:
        if self._paused:
            return ICON_PAUSED
        if self._status == "syncing":
            return ICON_SYNCING_FRAMES[0]
        elif self._status == "working":
            return ICON_WORKING_FRAMES[0]
        elif self._status == "error":
            return ICON_ERROR
        return self._idle_icon

    def _get_tooltip(self) -> str:
        if self._paused:
            return "SyncoPath — Paused"
        status_text = {
            "idle": "✅ Synced",
            "syncing": "🔄 Syncing",
            "working": "⏳ Working",
            "error": "⚠️ Error",
        }
        text = f"SyncoPath — {status_text.get(self._status, self._status)}"
        if self._detail:
            text += f"\n{self._detail}"
        if self._progress_total > 0 and self._status == "syncing":
            pct = int(self._progress_current / self._progress_total * 100)
            text += f" ({pct}%)"
        return text

    def _build_menu(self):
        """Build the right-click context menu."""
        self._menu = Gtk.Menu()

        # Status line (not clickable)
        self._status_item = Gtk.MenuItem(label="SyncoPath — ✅ Synced")
        self._status_item.set_sensitive(False)
        self._menu.append(self._status_item)

        # Progress bar (hidden by default) — fixed width to prevent menu jitter
        self._progress_item = Gtk.MenuItem()
        self._progress_item.set_sensitive(False)
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        progress_box.set_size_request(300, -1)  # Fixed width
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_text("0/0 (0%)")
        progress_box.pack_start(self._progress_bar, True, True, 0)
        self._progress_item.add(progress_box)
        self._menu.append(self._progress_item)
        self._progress_item.hide()

        # Current file label (hidden by default) — fixed width, ellipsized
        self._file_label = Gtk.MenuItem()
        self._file_label.set_sensitive(False)
        self._file_label_inner = Gtk.Label(label="")
        self._file_label_inner.set_xalign(0)
        self._file_label_inner.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._file_label_inner.set_max_width_chars(45)
        self._file_label_inner.set_width_chars(45)
        self._file_label.add(self._file_label_inner)
        self._menu.append(self._file_label)
        self._file_label.hide()

        self._menu.append(Gtk.SeparatorMenuItem())

        # Sync Now
        item = Gtk.MenuItem(label="Sync Now")
        item.connect("activate", lambda _: self._on_sync_now and self._on_sync_now())
        self._menu.append(item)

        # Open Folder
        item = Gtk.MenuItem(label="Open Folder")
        item.connect("activate", lambda _: self._on_open_folder and self._on_open_folder())
        self._menu.append(item)

        self._menu.append(Gtk.SeparatorMenuItem())

        # Pause/Resume
        self._pause_item = Gtk.MenuItem(label="Pause")
        self._pause_item.connect("activate", self._toggle_pause)
        self._menu.append(self._pause_item)

        self._menu.append(Gtk.SeparatorMenuItem())

        # Preferences
        item = Gtk.MenuItem(label="Preferences")
        item.connect("activate", lambda _: self._on_preferences and self._on_preferences())
        self._menu.append(item)

        self._menu.append(Gtk.SeparatorMenuItem())

        # Quit
        item = Gtk.MenuItem(label="Quit")
        item.connect("activate", lambda _: self._on_quit and self._on_quit())
        self._menu.append(item)

        self._menu.show_all()
        # Hide progress elements initially
        self._progress_item.hide()
        self._file_label.hide()

    def _toggle_pause(self, _widget):
        """Toggle pause/resume."""
        self._paused = not self._paused
        if self._paused:
            self._pause_item.set_label("Resume")
            self._stop_animation()
            self.update_status("paused")
            if self._on_pause:
                self._on_pause()
        else:
            self._pause_item.set_label("Pause")
            self.update_status("idle")
            if self._on_resume:
                self._on_resume()

    def _on_popup(self, icon, button, time):
        """Show popup menu for StatusIcon fallback."""
        self._menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)

    @property
    def is_paused(self) -> bool:
        return self._paused
