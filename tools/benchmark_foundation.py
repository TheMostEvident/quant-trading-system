"""Small synchronous synthetic probe; does not estimate live feed capacity."""

import argparse
import json
import math
import tempfile
import time
from copy import deepcopy
from pathlib import Path

from quant_system.foundation.buffer import CaptureBuffer
from quant_system.foundation.environment import environment_report
from quant_system.foundation.models import Capture
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import encode, timestamp, utc_us


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=200)
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be positive")
    fixture = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/contracts-v0.2.json").read_text(encoding="utf-8"))[0]
    base = utc_us("2026-11-04T00:00:00.000000Z")
    samples = {key: [] for key in ("enqueue_to_raw_commit", "normalize_commit", "delivery_commit", "ack_commit", "enqueue_to_delivery")}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "probe.db"
        queue = CaptureBuffer()
        with JournalStore(path) as store:
            store.register_run("probe", mode="SYNTHETIC_REPLAY", config={"events": args.events, "seed": 7}, code_version="quant-system-0.1.0")
            store.put_artifact("spec-fut-v1", "probe", {"synthetic_only": True}, ready_at=timestamp(base))
            started = time.perf_counter_ns()
            for i in range(1, args.events + 1):
                at = timestamp(base + i * 1000)
                seen = timestamp(base + i * 1000 + 500)
                raw = encode({"sequence": i, "bids": [["100.9", "8"]], "asks": [["101.0", "2"]]})
                capture = Capture("c-" + str(i), "probe", "fixture", "epoch-1", i, "fixture", "depth", raw, at, at, i * 1_000_000)
                start = time.perf_counter_ns()
                if not queue.submit(capture):
                    raise RuntimeError("Unexpected synthetic queue overflow")
                queue.drain(store)
                raw_done = time.perf_counter_ns()
                message = deepcopy(fixture)
                message.update(message_id="m-" + str(i), run_id="probe", source_event_id=None, event_at=at,
                               received_at=at, ready_at=at, lineage=[capture.capture_id])
                message["payload"]["features"]["basis"] = [capture.capture_id]
                store.publish(capture.capture_id, message, ["consumer"])
                normalized = time.perf_counter_ns()
                store.dispatch("probe", "consumer", seen_at=seen, consumer_epoch="consumer-1", monotonic_ns=i * 1_000_000 + 500_000)
                delivered = time.perf_counter_ns()
                store.acknowledge("consumer", message["message_id"], at=seen)
                acked = time.perf_counter_ns()
                for key, value in (("enqueue_to_raw_commit", raw_done - start), ("normalize_commit", normalized - raw_done),
                                   ("delivery_commit", delivered - normalized), ("ack_commit", acked - delivered),
                                   ("enqueue_to_delivery", delivered - start)):
                    samples[key].append(value / 1_000_000)
            duration = (time.perf_counter_ns() - started) / 1_000_000_000
            latency = {key: {"p95": sorted(values)[math.ceil(len(values) * .95) - 1],
                             "p99": sorted(values)[math.ceil(len(values) * .99) - 1]} for key, values in samples.items()}
            result = {"environment": environment_report(), "events": args.events, "elapsed_seconds": duration,
                      "events_per_second": args.events / duration, "latency_ms": latency,
                      "queue_high_water_items": queue.high_water_items, "queue_high_water_bytes": queue.high_water_bytes,
                      "dropped": queue.dropped, "sqlite_busy_failures": 0,
                      "wal_bytes_before_checkpoint": Path(str(path) + "-wal").stat().st_size,
                      "raw_blob_bytes": store.connection.execute("SELECT SUM(byte_length) FROM blobs").fetchone()[0],
                      "checkpoint_passive": store.checkpoint(),
                      "integrity": store.verify_integrity(), "synchronous": store.profile()["synchronous"],
                      "scope": "single-process synthetic synchronous probe; no burst, hardware fsync or live-feed guarantee"}
            print(encode(result).decode())


if __name__ == "__main__":
    main()
