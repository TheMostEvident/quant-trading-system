import time
from pathlib import Path

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.replay import ReplaySession, build_manifest
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, encode, now_utc, timestamp, utc_us

from .feed import MarketFeed


def ingest_file(input_path, database, run_id, profile, *, manifest_path=None, follow=False, seconds=60, simulate_clock=False, feature_config=None, producer_finished=None):
    if type(seconds) not in (int, float) or seconds <= 0 or seconds > 3600:
        raise ValidationError("Follow duration must be 0 < seconds <= 3600")
    if simulate_clock and (profile.get("data_origin") != "SYNTHETIC" or follow):
        raise ValidationError("Virtual readiness is only allowed for explicit offline synthetic runs")
    if producer_finished is not None and (not follow or not callable(producer_finished)):
        raise ValidationError("Producer completion callback requires live follow mode")
    path = Path(input_path)
    health_path = Path(str(path) + ".health.json")
    if manifest_path and Path(manifest_path).exists():
        raise ValidationError("Manifest destination already exists")
    with JournalStore(database) as store:
        feed = MarketFeed(store, run_id, profile, feature_config=feature_config)
        consumer_epoch = "market-consumer:" + run_id

        def deliver(at):
            at = timestamp(max(utc_us(at), feed.last_ready_us or utc_us(at)))
            rows = store.dispatch(run_id, feed.consumer, seen_at=at, consumer_epoch=consumer_epoch, monotonic_ns=time.monotonic_ns())
            for row in rows:
                store.acknowledge(feed.consumer, row["message_id"], at=at)

        last_at = None
        pending = b""
        started = time.monotonic()
        last_tick = started
        with path.open("rb") as source:
            while True:
                part = source.readline(profile["max_frame_bytes"] + 1)
                if part:
                    pending += part
                    if len(pending) > profile["max_frame_bytes"]:
                        store.storage_healthy = False
                        raise ValidationError("Spool frame exceeds configured bound; stop and quarantine source file")
                    if pending.endswith(b"\n"):
                        at = now_utc()
                        if simulate_clock:
                            try:
                                at = decode(pending)["received_at"]
                                utc_us(at)
                            except (ValidationError, KeyError, TypeError):
                                at = last_at or now_utc()
                        feed.process(pending, ready_at=at if simulate_clock else None)
                        last_at = max(at, now_utc()) if follow else at
                        if feed.last_ready_us is not None:
                            last_at = timestamp(max(utc_us(last_at), feed.last_ready_us))
                        deliver(last_at)
                        pending = b""
                if health_path.exists():
                    try:
                        with health_path.open("rb") as health_file:
                            raw_health = health_file.read(65537)
                        if len(raw_health) > 65536:
                            raise ValidationError("Health sidecar exceeds bound")
                        health = decode(raw_health)
                        failed = not isinstance(health, dict) or health.get("healthy") is not True
                    except (OSError, ValidationError):
                        failed = True
                    if failed:
                        if not feed.transport_fault:
                            last_at = (last_at or now_utc()) if simulate_clock else now_utc()
                            feed.tick(at=last_at, transport_failed=True)
                            deliver(last_at)
                        if follow:
                            break
                if not follow:
                    if part:
                        continue
                    if pending:
                        at = now_utc() if not simulate_clock else (last_at or now_utc())
                        feed.process(pending, ready_at=at if simulate_clock else None, framing_error=True)
                        last_at = at
                        last_at = timestamp(max(utc_us(last_at), feed.last_ready_us or utc_us(last_at)))
                        deliver(last_at)
                    break
                elapsed = time.monotonic()
                if not part and producer_finished is not None and producer_finished():
                    if pending:
                        feed.process(pending, framing_error=True)
                        pending = b""
                    last_at = now_utc()
                    feed.tick(at=last_at, transport_failed=True)
                    deliver(last_at)
                    break
                if elapsed - last_tick >= .5:
                    last_at = now_utc()
                    feed.tick(at=last_at)
                    deliver(last_at)
                    last_tick = elapsed
                if elapsed - started >= seconds:
                    last_at = now_utc()
                    feed.tick(at=last_at, transport_failed=True)
                    deliver(last_at)
                    break
                if not part:
                    time.sleep(.05)
        if not feed.initialized:
            raise ValidationError("Input contained no callback frames")
        last_at = timestamp(max(utc_us(last_at), feed.last_ready_us or utc_us(last_at)))
        boundary = store.boundary(run_id, feed.consumer, at=last_at, decision_id=run_id + ":import-end")
        manifest = build_manifest(store, boundary)
        if manifest_path:
            target = Path(manifest_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(encode(manifest) + b"\n")
        checkpoint = store.checkpoint()
        return {"run_id": run_id, "data_origin": profile["data_origin"], "input_view": "synthetic virtual producer" if simulate_clock else "new normalization availability; not original historical readiness",
                "metrics": feed.metrics, "integrity": store.verify_integrity(), "manifest_sha256": manifest["sha256"],
                "trace_sha256": ReplaySession(manifest).trace_digest(), "healthy": store.healthy_at(boundary),
                "usable_instruments": {s.binding["instrument_id"]: store.usable(boundary, "book:" + s.binding["instrument_id"]) for s in feed.states.values()},
                "checkpoint": checkpoint, "windows_ocx_verified": False}
