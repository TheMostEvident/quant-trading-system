"""Reproducible synthetic storage/delivery demonstration; no trading strategy."""

from pathlib import Path

from .foundation.buffer import CaptureBuffer
from .foundation.errors import ValidationError
from .foundation.models import Capture, Version
from .foundation.replay import ReplaySession, build_manifest
from .foundation.store import JournalStore
from .foundation.values import encode, timestamp, utc_us

DEFAULT_PROFILE = {"seed": 7, "max_raw_bytes": 16 * 1024 * 1024,
                   "queue_max_items": 1000, "queue_max_bytes": 8 * 1024 * 1024,
                   "dispatch_limit": 100, "manifest_max_bytes": 64 * 1024 * 1024}
_BASE = utc_us("2026-11-04T00:00:00.000000Z")


def run_demo(directory, profile=None):
    profile = dict(DEFAULT_PROFILE if profile is None else profile)
    if set(profile) != set(DEFAULT_PROFILE) or any(type(v) is not int or v < (0 if k == "seed" else 1) for k, v in profile.items()):
        raise ValidationError("Demo profile requires the documented integer limits and seed")
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValidationError("Demo output directory must be empty; previous results are preserved")
    at = lambda second: timestamp(_BASE + second * 1_000_000)
    queue = CaptureBuffer(max_items=profile["queue_max_items"], max_bytes=profile["queue_max_bytes"])
    with JournalStore(directory / "history.db", max_raw_bytes=profile["max_raw_bytes"]) as store:
        store.register_run("synthetic-demo", mode="SYNTHETIC_REPLAY", config=profile, code_version="quant-system-0.1.0")
        store.put_artifact("demo-spec", "synthetic-demo", {"synthetic_only": True, "quantity_unit": "CONTRACT"}, ready_at=at(0))
        boundaries = []
        for revision, second in ((1, 1), (2, 4)):
            raw = encode({"source": "synthetic-book", "revision": revision, "bid": "100.9", "ask": "101.0"})
            capture = Capture("capture-" + str(revision), "synthetic-demo", "fixture", "producer-1", revision,
                              "synthetic-book", "depth", raw, at(second), at(second), second * 1_000_000_000,
                              native_id=str(revision), identity_scope="SIM/session")
            if not queue.submit(capture):
                raise ValidationError("Configured demo queue cannot hold one capture")
            queue.drain(store)
            message = {"schema_version": "0.2.0", "message_type": "MarketSnapshot", "message_id": "book-" + str(revision),
                       "run_id": "synthetic-demo", "instrument_id": "SIM:KR-FUT1", "source": "synthetic-book",
                       "source_event_id": str(revision), "event_at": at(1), "received_at": at(second), "ready_at": at(second),
                       "data_origin": "SYNTHETIC", "quality_flags": [], "lineage": [capture.capture_id],
                       "payload": {"instrument_spec_ref": "demo-spec", "trading_date": "2026-11-04", "update_kind": "BOOK",
                                   "market_status": "CONTINUOUS", "book": {"coverage": "TOP_N_SNAPSHOT", "sync_status": "SYNCED",
                                   "source_sequence": revision, "bids": [{"price": "100.9", "quantity": str(8 + revision)}],
                                   "asks": [{"price": "101.0", "quantity": "2"}]}, "trade": None, "features": None}}
            store.publish(capture.capture_id, message, ["demo-consumer"], version=Version("book", revision, at(1)))
            # The first input is deliberately delivered twice before ACK.
            for seen in ((2, 3) if revision == 1 else (5,)):
                store.dispatch("synthetic-demo", "demo-consumer", seen_at=at(seen), consumer_epoch="consumer-1",
                               monotonic_ns=seen * 1_000_000_000, limit=profile["dispatch_limit"])
                store.apply_once("synthetic-demo", "demo-consumer", message["message_id"], "observe:" + str(revision),
                                 {"observed": message["message_id"]}, {"last_seen": message["message_id"]}, at=at(seen))
            store.acknowledge("demo-consumer", message["message_id"], at=at(3 if revision == 1 else 5))
            boundaries.append(store.boundary("synthetic-demo", "demo-consumer", at=at(3 if revision == 1 else 6),
                                             decision_id="decision-" + str(revision)))
        manifest = build_manifest(store, boundaries[-1], seed=profile["seed"], max_bytes=profile["manifest_max_bytes"])
        replay = ReplaySession(manifest)
        trace = replay.play(target_run_id="demo-replay")
        summary = {"data_origin": "SYNTHETIC", "integrity": store.verify_integrity(),
                   "captures": store.connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0],
                   "messages": store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                   "deliveries": len(trace), "business_effects": store.connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0],
                   "past_book": store.latest(boundaries[0], "book")["message_id"],
                   "latest_book": store.latest(boundaries[1], "book")["message_id"],
                   "manifest_sha256": manifest["sha256"], "trace_sha256": replay.trace_digest(target_run_id="demo-replay"),
                   "speed_independent": replay.trace_digest(speed=1) == replay.trace_digest(speed=1000),
                   "queue_high_water_items": queue.high_water_items, "queue_high_water_bytes": queue.high_water_bytes,
                   "storage_profile": store.profile(), "output_directory": str(directory)}
        summary["backup"] = store.backup(directory / "backup.db")
        for name, data in (("manifest.json", manifest), ("trace.json", trace), ("summary.json", summary)):
            with (directory / name).open("xb") as output:
                output.write(encode(data) + b"\n")
        return summary
