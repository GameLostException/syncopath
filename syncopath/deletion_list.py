"""Window listing the local deletions waiting to be sent to Drive (spec AC-18)."""
from typing import Callable

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


def show_pending_deletions(items: list[tuple[str, object]], on_send_all: Callable[[], None]):
    """items: (account name, PendingDeletion). The single action sends them all."""
    window = Gtk.Window(title="Pending deletions")
    window.set_default_size(560, 380)
    window.set_border_width(12)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    window.add(box)

    intro = Gtk.Label(xalign=0)
    intro.set_line_wrap(True)
    intro.set_text(
        "You deleted these items here. They are still on Drive until you send the "
        "deletions to Drive's bin. To keep one, restore it from the trash.")
    box.pack_start(intro, False, False, 0)

    store = Gtk.ListStore(str, str, str)
    for account, item in items:
        kind = "Folder" if item.is_folder else "File"
        note = "" if item.owned else "Not yours: will only stop syncing"
        store.append([f"{item.path}", f"{kind} · {account}", note])
    view = Gtk.TreeView(model=store)
    for i, title in enumerate(("Item", "Type and account", "Note")):
        column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
        column.set_resizable(True)
        view.append_column(column)
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_vexpand(True)
    scrolled.add(view)
    box.pack_start(scrolled, True, True, 0)

    buttons = Gtk.Box(spacing=8)
    close = Gtk.Button(label="Close")
    close.connect("clicked", lambda _: window.destroy())
    send = Gtk.Button(label="Send all to Drive's bin")

    def _send(_button):
        window.destroy()
        on_send_all()  # asks for confirmation itself (AC-20)

    send.connect("clicked", _send)
    buttons.pack_end(send, False, False, 0)
    buttons.pack_end(close, False, False, 0)
    box.pack_start(buttons, False, False, 0)
    window.show_all()
