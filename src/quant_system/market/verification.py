"""Cross-check a bounded field-session bundle. No external API calls."""

from collections import Counter
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re

from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.replay import ReplaySession
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decimal_value, decode, digest, encode, utc_us

from .feature_replay import verify_feature_replay
from .feed import validate_frame
from .latency import LatencyHistogram
from .mapping import BOOK_TYPE, FIDS, STATUS_TYPE, TRADE_TYPE
from .profile import validate_profile


MAX_BUNDLE_FILE = 64 * 1024 * 1024


def load_json(path, *, limit=MAX_BUNDLE_FILE):
    with Path(path).open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValidationError("Evidence JSON exceeds its byte limit")
    return decode(raw)


def profile_review(profile):
    profile = validate_profile(profile)
    missing = []
    if profile["data_origin"] != "OBSERVED":
        missing.append("observed_origin")
    if not profile["session_mapping_verified"] or not profile["status_codes"]:
        missing.append("verified_futures_session_mapping")
    if any(word in profile["mapping_evidence"].lower() for word in ("pending", "replacewith")):
        missing.append("futures_session_mapping_evidence")
    for item in profile["instruments"]:
        spec = item["specification"]
        for field in ("price_unit", "verification_evidence", "trading_date_evidence"):
            if not isinstance(spec.get(field), str) or not spec[field].strip():
                missing.append(item["source_code"] + ":" + field)
        if not item["spec_verified"]:
            missing.append(item["source_code"] + ":spec_verified")
        if not isinstance(spec.get("contract_month"), str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", spec["contract_month"]):
            missing.append(item["source_code"] + ":contract_month")
        try:
            if decimal_value(spec.get("tick_size")) <= 0:
                raise ValidationError("Nonpositive tick")
        except ValidationError:
            missing.append(item["source_code"] + ":tick_size")
    return {"ready_for_field_validation": not missing, "missing": missing,
            "semantic_verification": "operator evidence required; flags are not independent proof"}


def scan_capture(path, *, max_frame_bytes=65536, max_frames=100000):
    path = Path(path)
    if path.stat().st_size > MAX_BUNDLE_FILE:
        raise ValidationError("Use a shorter bounded field capture")
    checksum = hashlib.sha256()
    counts = Counter({name: 0 for name in FIDS})
    raw_hashes = Counter()
    producer = first_at = last_at = last_monotonic = None
    sequence = 0
    mock_connection = False
    terminal_disconnect = False
    with path.open("rb") as source:
        for raw in iter(lambda: source.readline(max_frame_bytes + 1), b""):
            if len(raw) > max_frame_bytes or not raw.endswith(b"\n") or sequence >= max_frames:
                raise ValidationError("Incomplete, oversized, or excessive source frames")
            frame = validate_frame(decode(raw))
            identity = (frame["producer_id"], frame["epoch"])
            if producer is not None and identity != producer:
                raise ValidationError("One collector epoch per field capture required")
            if frame["sequence"] != sequence + 1 or (last_monotonic is not None and frame["monotonic_ns"] < last_monotonic):
                raise IntegrityFailure("Capture counter or monotonic clock is discontinuous")
            if last_at is not None and utc_us(frame["received_at"]) < utc_us(last_at):
                raise IntegrityFailure("Capture UTC moved backwards")
            producer = identity
            sequence += 1
            first_at = first_at or frame["received_at"]
            last_at, last_monotonic = frame["received_at"], frame["monotonic_ns"]
            checksum.update(raw)
            raw_hashes[digest(raw)] += 1
            event = frame["event"]
            if event["kind"] == "REAL" and event["real_type"] in counts:
                counts[event["real_type"]] += 1
            mock_connection |= event["kind"] == "CONNECTION" and event["connected"] and event["server"] == "MOCK" and event["error_code"] == 0
            terminal_disconnect = event["kind"] == "CONNECTION" and not event["connected"]
    if not sequence:
        raise ValidationError("Empty field capture")
    return {"sha256": checksum.hexdigest(), "frame_count": sequence, "real_counts": dict(counts),
            "producer": producer, "first_at": first_at, "last_at": last_at,
            "mock_connection": bool(mock_connection), "terminal_disconnect": terminal_disconnect,
            "raw_hashes": raw_hashes}


def audit_session(directory, *, fsync_budget_ms=1000, previous_directory=None):
    if type(fsync_budget_ms) not in (int, float) or not math.isfinite(fsync_budget_ms) or fsync_budget_ms <= 0:
        raise ValidationError("Explicit positive fsync latency budget required")
    root = Path(directory)
    metadata = load_json(root / "session.json", limit=65536)
    profile = validate_profile(load_json(root / "profile.json"))
    report = load_json(root / "collector.json", limit=1024 * 1024)
    manifest = load_json(root / "manifest.json")
    run_id = metadata["run_id"]
    checks = {}
    details = {"profile_review": profile_review(profile)}

    def check(name, passed):
        checks[name] = "PASS" if passed else "FAIL"

    scan = scan_capture(root / "callbacks.jsonl", max_frame_bytes=profile["max_frame_bytes"])
    check("not_fixture_evidence", metadata.get("fixture_only") is not True)
    check("declared_latency_budget", metadata.get("fsync_budget_ms") == fsync_budget_ms)
    check("windows_ocx", report.get("os") == "Windows" and report.get("process_bits") == 32
          and report.get("com_created") is True and report.get("gui_loop_reached") is True)
    check("observed_origin", profile["data_origin"] == "OBSERVED")
    if (root / "ingest.json").exists():
        details["ingest"] = load_json(root / "ingest.json", limit=1024 * 1024)
    # Verify every frame's origin, not just the profile label.
    with (root / "callbacks.jsonl").open("rb") as source:
        check("source_origin", all(decode(raw)["data_origin"] == "OBSERVED" for raw in source))
    check("collector_capture_binding", report.get("report_version") == 1
          and report.get("capture_sha256") == scan["sha256"] and report.get("capture_filename") == "callbacks.jsonl")
    check("mock_login", report.get("mock_login_confirmed") is True and report.get("login_error_code") == 0 and scan["mock_connection"])
    registrations = report.get("registration_results", [])
    check("registration", report.get("registration_confirmed") is True and bool(registrations)
          and all(type(r.get("return_value")) is int and r["return_value"] == 0 for r in registrations))
    check("collector_finished", report.get("error") is None)
    check("required_market_callbacks", all(scan["real_counts"].values()))
    reported_counts = report.get("real_callback_counts")
    check("reported_counts_match_source", isinstance(reported_counts, dict) and reported_counts == scan["real_counts"]
          and all(type(n) is int for n in reported_counts.values()))
    transport = report.get("transport", {})
    check("durable_spool", transport.get("healthy") is True and transport.get("dropped") == 0
          and transport.get("enqueued") == transport.get("fsynced") == scan["frame_count"])
    fsync = transport.get("max_enqueue_to_fsync_ms")
    check("spool_latency", type(fsync) in (int, float) and math.isfinite(fsync) and 0 <= fsync <= fsync_budget_ms)
    check("profile_evidence", details["profile_review"]["ready_for_field_validation"])

    latency = LatencyHistogram()
    normalized = Counter()
    invalid_market = 0
    off_tick = 0
    specs = {item["instrument_id"]: item["specification"] for item in profile["instruments"]}
    with JournalStore(root / "history.db", read_only=True) as store:
        check("database_integrity", store.verify_integrity())
        run = store.run(run_id)
        check("immutable_profile", encode(run["config"]["market_profile"]) == encode(profile) and run["data_origin"] == profile["data_origin"])
        stored_raw = Counter()
        for row in store.connection.execute("SELECT sha256,metadata FROM captures WHERE run_id=?", (run_id,)):
            if decode(row["metadata"])["source"] == "kiwoom-openapi-plus":
                stored_raw[row["sha256"]] += 1
        check("all_source_bytes_preserved", stored_raw == scan["raw_hashes"])
        saved = store.connection.execute("SELECT data FROM manifests WHERE manifest_id=?", (manifest["manifest_id"],)).fetchone()
        check("manifest_database_binding", manifest["payload"]["store_id"] == store.store_id
              and manifest["payload"]["source_run_id"] == run_id and saved is not None and bytes(saved[0]) == encode(manifest))
        for row in store.connection.execute("SELECT data FROM messages WHERE run_id=? ORDER BY seq", (run_id,)):
            message = decode(row[0])
            payload = message["payload"]
            if payload["update_kind"] not in ("BOOK", "TRADE"):
                continue
            normalized[payload["update_kind"]] += 1
            latency.record((utc_us(message["ready_at"]) - utc_us(message["received_at"])) / 1000)
            invalid_market += bool(set(message["quality_flags"]) & {"STALE", "GAP", "OUT_OF_ORDER", "CLOCK_UNVERIFIED", "SESSION_UNVERIFIED", "INSTRUMENT_UNVERIFIED"})
            try:
                tick = Fraction(decimal_value(specs[message["instrument_id"]].get("tick_size")))
                prices = [level["price"] for side in ("bids", "asks") for level in (payload["book"] or {}).get(side, [])]
                if payload["trade"]:
                    prices.append(payload["trade"]["price"])
                off_tick += sum((Fraction(decimal_value(price)) / tick).denominator != 1 for price in prices)
            except (ValidationError, ZeroDivisionError):
                off_tick += 1
        check("normalized_books_and_trades", normalized["BOOK"] > 0 and normalized["TRADE"] > 0)
        check("market_quality", invalid_market == 0 and off_tick == 0)
        check("capture_to_ready_latency", latency.count > 0 and latency.maximum < profile["max_transport_delay_ms"])
        health = [dict(row) for row in store.connection.execute("SELECT seq,healthy,reason FROM health WHERE run_id=? ORDER BY seq", (run_id,))]
        check("startup_and_shutdown_blocked", bool(health) and not health[0]["healthy"] and not health[-1]["healthy"])
        first_usable = next((h["seq"] for h in health if h["healthy"]), None)
        post_usable_gap = False
        if first_usable is not None:
            for row in store.connection.execute("SELECT data FROM messages WHERE run_id=? AND seq>?", (run_id, first_usable)):
                post_usable_gap |= bool(set(decode(row[0])["quality_flags"]) & {"GAP", "STALE"})

    session = ReplaySession(manifest)
    trace = session.play(target_run_id="field-audit", speed=1)
    check("replay_speed_independent", digest(encode(trace)) == session.trace_digest(target_run_id="field-audit", speed=1000))
    usable_codes = {row["message"]["instrument_id"] for row in trace if row["healthy"] and row["message"]["payload"]["update_kind"] == "BOOK"
                    and row["message"]["payload"]["market_status"] == "CONTINUOUS"
                    and row["message"]["payload"]["book"]["sync_status"] == "SYNCED"}
    check("usable_book_for_each_instrument", usable_codes == set(specs))
    try:
        one = verify_feature_replay(manifest, target_run_id="field-features", speed=1)
        fast = verify_feature_replay(manifest, target_run_id="field-features", speed=1000)
        check("independent_feature_recomputation", one["recomputed_sha256"] == fast["recomputed_sha256"] and one["trace_sha256"] == fast["trace_sha256"])
        details["feature_replay"] = one
    except (ValidationError, IntegrityFailure) as error:
        check("independent_feature_recomputation", False)
        details["feature_replay"] = {"error": type(error).__name__}
    details.update(source={key: value for key, value in scan.items() if key != "raw_hashes"},
                   normalized_counts=dict(normalized), market_quality_failures=invalid_market, off_tick_prices=off_tick,
                   capture_to_ready_latency=latency.summary(), spool_latency_budget_ms=fsync_budget_ms,
                   source_post_usable_gap=bool(post_usable_gap), collector_report_sha256=digest(encode(report)),
                   manifest_sha256=manifest["sha256"])
    baseline_passed = all(value == "PASS" for value in checks.values())
    checks["controlled_process_restart"] = "PENDING"
    if previous_directory is not None:
        if Path(previous_directory).resolve() == root.resolve():
            raise ValidationError("Restart evidence must be a separate session")
        previous = audit_session(previous_directory, fsync_budget_ms=fsync_budget_ms)
        prior = previous["details"]["source"]
        check("controlled_process_restart", previous["baseline_checks_passed"] and baseline_passed
              and previous["details"]["source_post_usable_gap"] and prior["terminal_disconnect"]
              and tuple(prior["producer"]) != tuple(scan["producer"])
              and utc_us(scan["first_at"]) > utc_us(prior["last_at"]))
        details["previous_manifest_sha256"] = previous["details"]["manifest_sha256"]
    return {"audit_version": 1, "scope": "declared mock session; bounded capture and controlled process stop/restart",
            "baseline_checks_passed": baseline_passed, "field_checks_passed": all(value == "PASS" for value in checks.values()),
            "issue_closure": "REQUIRES_REVIEW_OF_FIELD_AND_SEMANTIC_EVIDENCE", "checks": checks, "details": details}
