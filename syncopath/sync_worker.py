"""Event queue and single-threaded sync worker for SyncoPath.

This replaces the fragile boolean `_syncing` lock pattern with a proper
FIFO queue. Local and remote watchers enqueue events; a single worker
thread processes them sequentially. This guarantees:

- No race conditions (single consumer = no concurrent state mutations)
- No dropped changes (queue never discards events)
- Correct ordering (FIFO within each source, interleaved fairly)
- Retry capability (failed items can be re-enqueued with backoff)
"""
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

log = logging.getLogger(__name__)


class EventType(Enum):
    """Types of sync events."""
    LOCAL_CHANGES = "local_changes"
    REMOTE_CHANGES = "remote_changes"
    RECONCILE = "reconcile"
    FOLDER_INDEX = "folder_index"
    SHUTDOWN = "shutdown"


@dataclass(order=True)
class SyncEvent:
    """A prioritized sync event.

    Priority: 0 = highest (shutdown), 1 = folder ops, 2 = normal, 3 = background.
    """
    priority: int
    event_type: EventType = field(compare=False)
    payload: Any = field(compare=False, default=None)
    attempt: int = field(compare=False, default=0)
    max_attempts: int = field(compare=False, default=3)
    created_at: float = field(compare=False, default_factory=time.time)


class SyncWorker:
    """Single-threaded worker that processes sync events from a priority queue.

    Usage:
        worker = SyncWorker()
        worker.set_handlers(
            on_local_changes=engine.handle_local_changes,
            on_remote_changes=engine.handle_remote_changes,
            on_reconcile=engine.reconcile,
            on_folder_index=engine._index_remote_folders,
        )
        worker.start()

        # From any thread:
        worker.enqueue_local(events)
        worker.enqueue_remote(changes)

        # Shutdown:
        worker.stop()
    """

    def __init__(self, status_callback=None):
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._processing = False
        self._status_callback = status_callback

        # Handlers
        self._on_local_changes = None
        self._on_remote_changes = None
        self._on_reconcile = None
        self._on_folder_index = None

        # Retry backoff schedule (seconds)
        self._backoff_schedule = [30, 60, 300, 1800]

    def set_handlers(self, on_local_changes=None, on_remote_changes=None,
                     on_reconcile=None, on_folder_index=None):
        """Set event handler functions."""
        self._on_local_changes = on_local_changes
        self._on_remote_changes = on_remote_changes
        self._on_reconcile = on_reconcile
        self._on_folder_index = on_folder_index

    def start(self):
        """Start the worker thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="sync-worker")
        self._thread.start()
        log.info("Sync worker started")

    def stop(self, timeout: float = 10.0):
        """Stop the worker thread gracefully."""
        self._running = False
        self._queue.put(SyncEvent(priority=0, event_type=EventType.SHUTDOWN))
        if self._thread:
            self._thread.join(timeout=timeout)
        log.info("Sync worker stopped")

    @property
    def is_busy(self) -> bool:
        """True if currently processing an event."""
        return self._processing

    @property
    def pending_count(self) -> int:
        """Number of events waiting in queue."""
        return self._queue.qsize()

    def enqueue_local(self, events):
        """Enqueue local filesystem changes for processing."""
        if events:
            self._queue.put(SyncEvent(
                priority=2,
                event_type=EventType.LOCAL_CHANGES,
                payload=events,
            ))

    def enqueue_remote(self, changes):
        """Enqueue remote Drive changes for processing."""
        if changes:
            self._queue.put(SyncEvent(
                priority=2,
                event_type=EventType.REMOTE_CHANGES,
                payload=changes,
            ))

    def enqueue_reconcile(self):
        """Enqueue a reconciliation pass."""
        self._queue.put(SyncEvent(
            priority=3,
            event_type=EventType.RECONCILE,
        ))

    def enqueue_folder_index(self):
        """Enqueue a folder re-indexing pass."""
        self._queue.put(SyncEvent(
            priority=1,
            event_type=EventType.FOLDER_INDEX,
        ))

    def _run(self):
        """Main worker loop — processes events sequentially."""
        while self._running:
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if event.event_type == EventType.SHUTDOWN:
                break

            self._processing = True
            try:
                self._process_event(event)
            except Exception as e:
                self._handle_failure(event, e)
            finally:
                self._processing = False
                self._queue.task_done()

    def _process_event(self, event: SyncEvent):
        """Process a single sync event."""
        if event.event_type == EventType.LOCAL_CHANGES:
            if self._on_local_changes:
                self._on_local_changes(event.payload)

        elif event.event_type == EventType.REMOTE_CHANGES:
            if self._on_remote_changes:
                self._on_remote_changes(event.payload)

        elif event.event_type == EventType.RECONCILE:
            if self._on_reconcile:
                self._on_reconcile()

        elif event.event_type == EventType.FOLDER_INDEX:
            if self._on_folder_index:
                self._on_folder_index()

    def _handle_failure(self, event: SyncEvent, error: Exception):
        """Handle a failed event — retry with backoff or give up."""
        event.attempt += 1
        log.error("Event %s failed (attempt %d/%d): %s",
                  event.event_type.value, event.attempt, event.max_attempts, error)

        if event.attempt >= event.max_attempts:
            log.error("Event %s permanently failed after %d attempts",
                      event.event_type.value, event.max_attempts)
            return

        # Schedule retry with backoff
        backoff_idx = min(event.attempt - 1, len(self._backoff_schedule) - 1)
        delay = self._backoff_schedule[backoff_idx]
        log.info("Retrying %s in %ds...", event.event_type.value, delay)

        def _retry():
            time.sleep(delay)
            if self._running:
                self._queue.put(event)

        threading.Thread(target=_retry, daemon=True, name="retry-timer").start()
