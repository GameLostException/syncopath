"""Move local files to the desktop trash (freedesktop.org), never delete them."""
from pathlib import Path


def move_to_trash(path: Path):
    """Move a file or folder to the user's trash. Raises if the trash refuses it.

    There is deliberately no fallback to permanent deletion (docs/specs/deletion.md).
    """
    from gi.repository import Gio  # imported here: the engine must not need GTK to load
    Gio.File.new_for_path(str(path)).trash(None)
