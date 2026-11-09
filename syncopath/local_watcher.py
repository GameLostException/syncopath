"""Local filesystem watcher using watchdog (inotify)."""
import fnmatch
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
)

log = logging.getLogger(__name__)


class LocalChangeEvent:
    """Represents a local filesystem change."""

    def __init__(self, event_type: str, path: str, dest_path: str = None):
        self.event_type = event_type  # "created", "modified", "deleted", "moved"
        self.path = path  # relative path
        self.dest_path = dest_path  # relative path (for moves only)
        self.timestamp = time.time()

    def __repr__(self):
        if self.dest_path:
            return f"LocalChange({self.event_type}: {self.path} -> {self.dest_path})"
        return f"LocalChange({self.event_type}: {self.path})"


class _Handler(FileSystemEventHandler):
    """Watchdog event handler that collects debounced changes."""

    def __init__(self, root: Path, exclude: list[str], callback: Callable):
        self.root = root
        self.exclude = exclude
        self.callback = callback

    def _relative(self, path: str) -> str:
        return str(Path(path).relative_to(self.root))

    def _is_excluded(self, path: str) -> bool:
        rel = self._relative(path)
        name = Path(path).name
        for pattern in self.exclude:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                return True
        # Ignore hidden/temp patterns
        if name.startswith(".") and name != ".":
            return True
        if name.endswith("~") or name.endswith(".partial"):
            return True
        # Ignore conflict copies created by sync engine
        if ".conflict" in name:
            return True
        return False

    def on_created(self, event):
        if self._is_excluded(event.src_path):
            return
        if isinstance(event, DirCreatedEvent):
            return  # We track files, dirs are created implicitly
        self.callback(LocalChangeEvent("created", self._relative(event.src_path)))

    def on_modified(self, event):
        if event.is_directory:
            return
        if self._is_excluded(event.src_path):
            return
        self.callback(LocalChangeEvent("modified", self._relative(event.src_path)))

    def on_deleted(self, event):
        if self._is_excluded(event.src_path):
            return
        evt_type = "deleted"
        self.callback(LocalChangeEvent(evt_type, self._relative(event.src_path)))

    def on_moved(self, event):
        if self._is_excluded(event.src_path) and self._is_excluded(event.dest_path):
            return
        self.callback(
            LocalChangeEvent(
                "moved",
                self._relative(event.src_path),
                self._relative(event.dest_path),
            )
        )


class LocalWatcher:
    """Watches a local directory for changes with debouncing."""

    def __init__(self, root: Path, exclude: list[str], debounce: float = 5.0,
                 max_delay: float = 30.0):
        self.root = root
        self.exclude = exclude
        self.debounce = debounce
        # A steady stream of events (a build, a download) must not postpone the
        # flush forever: a batch is flushed at most max_delay after its first event.
        self.max_delay = max(max_delay, debounce)
        self._observer = None
        self._pending: dict[str, LocalChangeEvent] = {}
        self._first_pending_at: float = 0.0
        self._lock = threading.Lock()
        self._callback: Callable = None
        self._debounce_timer: threading.Timer = None
        self._suppress_paths: set[str] = set()

    def suppress(self, rel_path: str):
        """Temporarily suppress events for a path (used during download)."""
        with self._lock:
            self._suppress_paths.add(rel_path)

    def unsuppress(self, rel_path: str):
        """Remove suppression for a path."""
        with self._lock:
            self._suppress_paths.discard(rel_path)

    def start(self, callback: Callable[[list[LocalChangeEvent]], None]):
        """Start watching. Callback receives batched changes after debounce."""
        self._callback = callback
        handler = _Handler(self.root, self.exclude, self._on_event)
        # An Observer is a thread and cannot be restarted: make a new one each time
        self._observer = Observer()
        self._observer.schedule(handler, str(self.root), recursive=True)
        self._observer.start()
        log.info("Watching local directory: %s", self.root)

    def stop(self):
        """Stop watching, and hand over the events already collected."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        if self._debounce_timer:
            self._debounce_timer.cancel()
        self._flush()

    def _on_event(self, event: LocalChangeEvent):
        """Collect events and debounce."""
        now = time.monotonic()
        with self._lock:
            if event.path in self._suppress_paths:
                return
            if not self._pending:
                self._first_pending_at = now
            # Deduplicate: latest event for same path wins
            self._pending[event.path] = event
            delay = min(self.debounce, self._first_pending_at + self.max_delay - now)

            # Reset debounce timer, but never past the batch's deadline
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(max(delay, 0.0), self._flush)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _flush(self):
        """Flush pending events to callback."""
        with self._lock:
            if not self._pending:
                return
            events = list(self._pending.values())
            self._pending.clear()

        if self._callback:
            self._callback(events)
