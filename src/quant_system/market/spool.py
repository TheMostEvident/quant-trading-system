"""Bounded callback-to-local-file transport, usable by a separate bridge Python."""

import json
import os
import queue
import threading
import time
from pathlib import Path

from .latency import LatencyHistogram


class CallbackSpool:
    def __init__(self, path, *, max_items=1000, max_bytes=8 * 1024 * 1024, max_frame_bytes=65536):
        if any(type(v) is not int or v < 1 for v in (max_items, max_bytes, max_frame_bytes)):
            raise ValueError("Positive spool limits required")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.output = self.path.open("xb")
        self.health_path = Path(str(self.path) + ".health.json")
        self.max_bytes, self.max_frame_bytes = max_bytes, max_frame_bytes
        self.queue = queue.Queue(maxsize=max_items)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.pending_bytes = 0
        self.healthy = True
        self.metrics = {"enqueued": 0, "fsynced": 0, "dropped": 0, "high_water_items": 0, "high_water_bytes": 0, "max_enqueue_to_fsync_ms": 0.0}
        self.latency = LatencyHistogram()
        self.error = None
        self._health()
        self.thread = threading.Thread(target=self._write, name="kiwoom-spool-writer", daemon=True)
        self.thread.start()

    def _health(self):
        # An independent path remains available when the data queue is full.
        # If the disk itself fails the reader's heartbeat timeout is the gate.
        value = {"healthy": self.healthy, "error": self.error, "metrics": dict(self.metrics), "enqueue_to_fsync_latency": self.latency.summary()}
        temporary = Path(str(self.health_path) + ".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.health_path)

    def submit(self, frame):
        enqueued_at = time.monotonic_ns()
        raw = (json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        with self.lock:
            if not self.healthy or self.stop.is_set() or len(raw) > self.max_frame_bytes or self.pending_bytes + len(raw) > self.max_bytes or self.queue.full():
                self.healthy = False
                self.error = "CALLBACK_SPOOL_OVERFLOW_OR_STOPPED"
                self.metrics["dropped"] += 1
                try:
                    self._health()
                except OSError:
                    pass
                return False
            self.queue.put_nowait((raw, enqueued_at))
            self.pending_bytes += len(raw)
            self.metrics["enqueued"] += 1
            self.metrics["high_water_items"] = max(self.metrics["high_water_items"], self.queue.qsize())
            self.metrics["high_water_bytes"] = max(self.metrics["high_water_bytes"], self.pending_bytes)
            return True

    def _write(self):
        try:
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    raw, enqueued_at = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                self.output.write(raw)
                self.output.flush()
                os.fsync(self.output.fileno())
                with self.lock:
                    self.pending_bytes -= len(raw)
                    self.metrics["fsynced"] += 1
                    elapsed = (time.monotonic_ns() - enqueued_at) / 1_000_000
                    self.metrics["max_enqueue_to_fsync_ms"] = max(self.metrics["max_enqueue_to_fsync_ms"], elapsed)
                    self.latency.record(elapsed)
                self.queue.task_done()
            self.output.close()
            with self.lock:
                self._health()
        except OSError:
            with self.lock:
                self.healthy = False
                self.error = "CALLBACK_SPOOL_IO_FAILURE"
                try:
                    self._health()
                except OSError:
                    pass
            self.output.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.healthy = False
            self.error = "CALLBACK_SPOOL_DRAIN_TIMEOUT"
            raise RuntimeError(self.error)
        return {"healthy": self.healthy, **self.metrics, "error": self.error, "enqueue_to_fsync_latency": self.latency.summary()}
