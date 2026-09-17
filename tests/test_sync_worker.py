"""Unit tests for syncopath.sync_worker — event queue and worker."""
import time
import threading
import pytest

from syncopath.sync_worker import SyncWorker, SyncEvent, EventType


class TestSyncWorker:
    """Tests for the sync worker queue processing."""

    def test_start_and_stop(self):
        worker = SyncWorker()
        worker.start()
        assert worker._running is True
        worker.stop(timeout=2)
        assert worker._running is False

    def test_enqueue_local_processes(self):
        results = []
        worker = SyncWorker()
        worker.set_handlers(on_local_changes=lambda events: results.extend(events))
        worker.start()

        worker.enqueue_local(["file1.txt", "file2.txt"])
        time.sleep(0.5)
        worker.stop(timeout=2)

        assert results == ["file1.txt", "file2.txt"]

    def test_enqueue_remote_processes(self):
        results = []
        worker = SyncWorker()
        worker.set_handlers(on_remote_changes=lambda changes: results.extend(changes))
        worker.start()

        worker.enqueue_remote(["change1", "change2"])
        time.sleep(0.5)
        worker.stop(timeout=2)

        assert results == ["change1", "change2"]

    def test_enqueue_reconcile(self):
        called = []
        worker = SyncWorker()
        worker.set_handlers(on_reconcile=lambda: called.append(True))
        worker.start()

        worker.enqueue_reconcile()
        time.sleep(0.5)
        worker.stop(timeout=2)

        assert called == [True]

    def test_sequential_processing(self):
        """Events are processed in order, not concurrently."""
        order = []
        lock = threading.Lock()

        def handler_a(events):
            with lock:
                order.append("a_start")
            time.sleep(0.2)
            with lock:
                order.append("a_end")

        def handler_b(changes):
            with lock:
                order.append("b_start")
            time.sleep(0.1)
            with lock:
                order.append("b_end")

        worker = SyncWorker()
        worker.set_handlers(on_local_changes=handler_a, on_remote_changes=handler_b)
        worker.start()

        worker.enqueue_local(["x"])
        worker.enqueue_remote(["y"])
        time.sleep(1)
        worker.stop(timeout=2)

        # a should complete before b starts
        assert order == ["a_start", "a_end", "b_start", "b_end"]

    def test_empty_payload_not_enqueued(self):
        results = []
        worker = SyncWorker()
        worker.set_handlers(on_local_changes=lambda e: results.append(e))
        worker.start()

        worker.enqueue_local([])  # Empty — should not enqueue
        worker.enqueue_local(None)  # None — should not enqueue
        time.sleep(0.3)
        worker.stop(timeout=2)

        assert results == []

    def test_pending_count(self):
        worker = SyncWorker()
        # Don't start — events queue up
        worker.enqueue_local(["a"])
        worker.enqueue_local(["b"])
        assert worker.pending_count == 2

    def test_failure_retries(self):
        attempts = []

        def failing_handler(events):
            attempts.append(len(attempts) + 1)
            raise RuntimeError("transient error")

        worker = SyncWorker()
        worker._backoff_schedule = [0.1, 0.2]  # Fast backoff for testing
        worker.set_handlers(on_local_changes=failing_handler)
        worker.start()

        worker.enqueue_local(["test"])
        time.sleep(2)  # Wait for retries
        worker.stop(timeout=2)

        # Should have attempted multiple times (initial + retries)
        assert len(attempts) >= 2
