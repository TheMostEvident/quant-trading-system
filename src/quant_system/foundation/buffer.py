from collections import deque
from threading import Lock

from .errors import StorageFailure, ValidationError


class CaptureBuffer:
    """Nonblocking intake with a health path independent of the data queue.

    The caller owns the writer loop; this object does not start a collector or
    connect to COM. Queue overflow is a declared gap, never a silent success.
    """

    def __init__(self, *, max_items=1000, max_bytes=8 * 1024 * 1024):
        if type(max_items) is not int or type(max_bytes) is not int or min(max_items, max_bytes) < 1:
            raise ValidationError("Positive queue limits are required")
        self.max_items, self.max_bytes = max_items, max_bytes
        self._queue, self._health = deque(), {}
        self._bytes = 0
        self._lock = Lock()
        self.healthy = True
        self.dropped = 0
        self.high_water_items = 0
        self.high_water_bytes = 0

    def submit(self, capture):
        with self._lock:
            size = len(capture.body)
            if len(self._queue) >= self.max_items or self._bytes + size > self.max_bytes:
                self.healthy = False
                self.dropped += 1
                self._health[capture.run_id] = (capture.ready_at, "CAPTURE_QUEUE_OVERFLOW")
                return False
            self._queue.append(capture)
            self._bytes += size
            self.high_water_items = max(self.high_water_items, len(self._queue))
            self.high_water_bytes = max(self.high_water_bytes, self._bytes)
            return True

    def drain(self, store, *, limit=100):
        with self._lock:
            pending = dict(self._health)
        try:
            for run_id, (at, reason) in pending.items():
                store.set_health(run_id, False, reason, at=at)
                with self._lock:
                    if self._health.get(run_id) == (at, reason):
                        del self._health[run_id]
            completed = 0
            while completed < limit:
                with self._lock:
                    if not self._queue:
                        break
                    capture = self._queue[0]
                store.record_capture(capture)
                with self._lock:
                    self._queue.popleft()
                    self._bytes -= len(capture.body)
                completed += 1
            return completed
        except Exception:
            self.healthy = False
            raise

    def recover(self, store, run_id, *, at, evidence):
        with self._lock:
            if self._queue or self._health:
                raise StorageFailure("Drain or explicitly quarantine the backlog before recovery")
        store.verify_integrity()
        store.set_health(run_id, True, "EXPLICIT_RECONCILIATION", at=at, recovery_evidence=evidence)
        store.storage_healthy = True
        self.healthy = True
