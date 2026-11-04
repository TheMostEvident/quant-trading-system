"""Synthetic callback -> journal/outbox -> consumer probe; no broker or orders."""

import argparse
from copy import deepcopy
import math
from pathlib import Path
import tempfile
import time

from quant_system.foundation.environment import environment_report
from quant_system.foundation.replay import ReplaySession, build_manifest
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, encode, timestamp, utc_us
from quant_system.market.feed import MarketFeed
from quant_system.market.feature_replay import verify_feature_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.events <= 10000:
        parser.error("--events must be 1..10000")
    root = Path(__file__).resolve().parents[1]
    profile = decode((root / "config/kiwoom.synthetic.json").read_bytes())
    frames = [decode(row) for row in (root / "tests/fixtures/kiwoom-callbacks.synthetic.jsonl").read_bytes().splitlines()]
    base = utc_us(frames[0]["received_at"])
    samples = {key: [] for key in ("raw_and_normalize_commit", "callback_to_consumer_ack")}
    with tempfile.TemporaryDirectory() as directory, JournalStore(Path(directory) / "market.db") as store:
        feed = MarketFeed(store, "probe", profile)
        for frame in frames[:2]:
            feed.process(encode(frame), ready_at=frame["received_at"])
        started = time.perf_counter_ns()
        counter = 2
        for i in range(args.events):
            frame = deepcopy(frames[2])
            frame.update(received_at=timestamp(base + 200_000 + i * 1000), monotonic_ns=200_000_000 + i * 1_000_000)
            if i and i % 500 == 0:
                counter += 1
                heartbeat = deepcopy(frame)
                heartbeat.update(sequence=counter, event={"kind": "HEARTBEAT", "connected": True})
                feed.process(encode(heartbeat), ready_at=heartbeat["received_at"])
            counter += 1
            frame["sequence"] = counter
            begin = time.perf_counter_ns()
            feed.process(encode(frame) + b"\n", ready_at=frame["received_at"])
            normalized = time.perf_counter_ns()
            rows = store.dispatch("probe", "market", seen_at=frame["received_at"], consumer_epoch="consumer", monotonic_ns=time.monotonic_ns())
            for row in rows:
                store.acknowledge("market", row["message_id"], at=frame["received_at"])
            done = time.perf_counter_ns()
            samples["raw_and_normalize_commit"].append((normalized - begin) / 1_000_000)
            samples["callback_to_consumer_ack"].append((done - begin) / 1_000_000)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
        boundary = store.boundary("probe", "market", at=frame["received_at"], decision_id="probe-end")
        manifest = build_manifest(store, boundary)
        replay = ReplaySession(manifest)
        feature_verification = verify_feature_replay(manifest)
        latency = {key: {"p95": sorted(values)[math.ceil(len(values) * .95) - 1], "p99": sorted(values)[math.ceil(len(values) * .99) - 1]} for key, values in samples.items()}
        result = {"environment": environment_report(), "book_events": args.events, "elapsed_seconds": elapsed, "events_per_second": args.events / elapsed,
                  "latency_ms": latency, "metrics": feed.metrics, "healthy": store.healthy_at(boundary),
                  "book_usable": store.usable(boundary, "book:SIM:KR-FUT1"), "integrity": store.verify_integrity(),
                  "replay_speed_independent": replay.trace_digest(speed=1) == replay.trace_digest(speed=1000),
                  "feature_recomputation": feature_verification,
                  "wal_bytes_before_checkpoint": Path(str(store.path) + "-wal").stat().st_size, "checkpoint": store.checkpoint(),
                  "scope": "single-process synthetic callbacks with virtual availability; not live feed latency/capacity"}
        print(encode(result).decode())


if __name__ == "__main__":
    main()
