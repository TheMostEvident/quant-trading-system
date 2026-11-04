"""Synthetic protocol fixtures only: these tests are not Windows field evidence."""

from copy import deepcopy
import json
from pathlib import Path
import io
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.replay import build_manifest
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode, timestamp, utc_us
from quant_system.market.bridge import BridgeController, BridgeFailure, QtPort, main as bridge_main
from quant_system.market.cli import ingest_file
from quant_system.market.diagnostics import EventObservation, INITIAL_FIELDS, INITIAL_RQ, INITIAL_SCREEN, INITIAL_TR, collection_diagnosis
from quant_system.market.field_run import make_profile, write_new_json
from quant_system.market.feed import MarketFeed
from quant_system.market.latency import LatencyHistogram
from quant_system.market.mapping import BOOK_TYPE, FIDS, STATUS_TYPE, TRADE_TYPE
from quant_system.market.verification import audit_session, profile_review, scan_capture

ROOT = Path(__file__).resolve().parents[1]
PROFILE = decode((ROOT / "config/kiwoom.synthetic.json").read_bytes())
ORIGINAL = [decode(raw) for raw in (ROOT / "tests/fixtures/kiwoom-callbacks.synthetic.jsonl").read_bytes().splitlines()]
BASE = utc_us("2026-11-04T01:00:00.000000Z")


class Sink:
    def __init__(self): self.frames = []
    def submit(self, frame): self.frames.append(frame); return True


class Control:
    def __init__(self, tr_result=0): self.calls, self.tr_result = [], tr_result
    def dynamicCall(self, name, *args):
        self.calls.append((name, args))
        if name.startswith("KOA_"): return "1"
        if name.startswith("CommRqData"): return self.tr_result
        if name.startswith("GetCommData"): return " +100.00 "
        if name.startswith("GetCommRealData"): return "2"
        return 0


class DiagnosticTests(unittest.TestCase):
    def bridge(self, bootstrap=True, tr_result=0):
        control, sink = Control(tr_result), Sink()
        bridge = BridgeController(QtPort(control), sink, ["SIMFUT1"], bootstrap_tr=bootstrap,
                                  clock=lambda: (timestamp(BASE), 100))
        return bridge, control, sink

    def test_initial_request_uses_only_public_quote_methods(self):
        bridge, control, sink = self.bridge()
        bridge.on_connect(0)
        names = {name.split("(")[0] for name, _ in control.calls}
        self.assertEqual(names, {"KOA_Functions", "SetRealReg", "SetInputValue", "CommRqData"})
        request = next(args for name, args in control.calls if name.startswith("CommRqData"))
        self.assertEqual(request, (INITIAL_RQ, INITIAL_TR, 0, INITIAL_SCREEN))
        self.assertTrue(bridge.mock_confirmed)
        self.assertTrue(bridge.initial_tr["requested"])

    def test_initial_quote_is_not_a_real_snapshot_or_fresh_book(self):
        bridge, control, sink = self.bridge()
        bridge.on_connect(0)
        before = len(sink.frames)
        bridge.on_initial_tr(INITIAL_SCREEN, INITIAL_RQ, INITIAL_TR, "record", "0", 0, "", "", "")
        self.assertEqual(len(sink.frames), before)
        self.assertFalse(any(bridge.real_counts.values()))
        self.assertTrue(bridge.initial_tr["response"]["diagnostic_only"])
        self.assertEqual(set(bridge.initial_tr["response"]["fields"]), set(INITIAL_FIELDS))
        bridge.on_initial_tr(INITIAL_SCREEN, INITIAL_RQ, INITIAL_TR, "record", "0", 0, "", "", "")
        reads = [call for call in control.calls if call[0].startswith("GetCommData")]
        self.assertEqual(len(reads), len(INITIAL_FIELDS))

    def test_unrequested_or_unrelated_tr_does_not_read_data(self):
        bridge, control, _ = self.bridge()
        bridge.on_connect(0)
        before = len(control.calls)
        for screen, rq, tr in [("0001", INITIAL_RQ, INITIAL_TR), (INITIAL_SCREEN, "other", INITIAL_TR), (INITIAL_SCREEN, INITIAL_RQ, "opw00018")]:
            bridge.on_initial_tr(screen, rq, tr, "private-record", "0", 0, "", "", "")
        self.assertEqual(len(control.calls), before)
        self.assertEqual(bridge.initial_tr["response_count"], 0)

    def test_failed_initial_request_preserves_authentication_and_code(self):
        bridge, _, _ = self.bridge(tr_result=-200)
        with self.assertRaises(BridgeFailure) as caught:
            bridge.on_connect(0)
        self.assertTrue(bridge.mock_confirmed)
        self.assertTrue(bridge.registration_confirmed)
        self.assertEqual(caught.exception.details["stage"], "INITIAL_TR_REQUEST")
        self.assertEqual(bridge.initial_tr["request_result"]["return_value"], -200)

    def test_disabled_bootstrap_never_calls_tr_methods(self):
        bridge, control, _ = self.bridge(bootstrap=False)
        bridge.on_connect(0)
        self.assertFalse(any(name.startswith(("SetInputValue", "CommRqData")) for name, _ in control.calls))

    def test_failed_tr_response_does_not_read_quote_or_erase_login(self):
        bridge, control, _ = self.bridge()
        bridge.on_connect(0)
        with self.assertRaises(BridgeFailure):
            bridge.on_initial_tr(INITIAL_SCREEN, INITIAL_RQ, INITIAL_TR, "record", "0", 0, "-200", "not retained", "")
        self.assertTrue(bridge.mock_confirmed)
        self.assertFalse(any(name.startswith("GetCommData") for name, _ in control.calls))
        self.assertEqual(bridge.initial_tr["response"]["error_code"], -200)

    def test_early_ocx_failure_saves_report_without_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            with patch("quant_system.market.bridge.os", SimpleNamespace(name="nt")), patch.dict(sys.modules, {"PyQt5.QAxContainer": None}), patch("sys.stdout", io.StringIO()):
                result = bridge_main(["--probe", "--report", str(target)])
            report = decode(target.read_bytes())
            self.assertEqual(result, 1)
            self.assertEqual(report["error_details"]["stage"], "OCX_INITIALIZE")
            self.assertEqual(report["report_version"], 1)

    def test_all_type_observation_precedes_filters_without_reading_other_fields(self):
        bridge, control, _ = self.bridge(bootstrap=False)
        bridge.on_real("OTHER", BOOK_TYPE, "not retained")
        bridge.on_real("SIMFUT1", "UNKNOWN_NIGHT_TYPE", "not retained")
        self.assertEqual(control.calls, [])
        obs = bridge.observations.summary()
        self.assertEqual(obs["total_before_filter"], 2)
        self.assertEqual(obs["filtered_unknown_type"], 1)
        self.assertEqual(obs["filtered_unselected_code"], 1)
        self.assertEqual(obs["by_type"]["UNKNOWN_NIGHT_TYPE"], 1)

    def test_type_cardinality_is_bounded(self):
        observation = EventObservation(max_types=3)
        for n in range(1000): observation.record(str(n))
        self.assertLessEqual(len(observation.by_type), 4)
        self.assertEqual(observation.total, 1000)

    def test_diagnosis_does_not_equate_tr_response_with_live_feed(self):
        result = collection_diagnosis(mock=True, registered=True, observations={"total_before_filter": 0},
                                     accepted={name: 0 for name in FIDS}, initial_tr={"enabled": True, "response_count": 1})
        self.assertEqual(result, "TR_RESPONSE_WITHOUT_REAL_CALLBACKS")

    def test_multi_instrument_bootstrap_is_rejected_before_api_requests(self):
        with self.assertRaises(ValueError):
            BridgeController(QtPort(Control()), Sink(), ["ONE", "TWO"], bootstrap_tr=True)


def fixture_bundle(root, *, observed=False, offset=0):
    """Fabricate protocol observations, explicitly marked fixture_only."""
    profile = deepcopy(PROFILE)
    if observed:
        profile["data_origin"] = "OBSERVED"
        profile["instruments"][0].update(source_code="TESTFUT0", instrument_id="TEST:FIELD")
    binding = profile["instruments"][0]
    binding["specification"].update(contract_month="2026-12", verification_evidence="unit-test only",
                                    trading_date_evidence="unit-test only")
    events = [deepcopy(ORIGINAL[i]["event"]) for i in (0, 1, 2, 3, 2, 4)]
    frames = []
    for index, event in enumerate(events, 1):
        if event["kind"] == "REAL" and event["real_type"] != STATUS_TYPE:
            event["code"] = binding["source_code"]
        frames.append({"frame_version": 1, "producer_id": "unit-test-not-live", "epoch": "fixture-" + str(offset),
                       "sequence": index, "received_at": timestamp(BASE + offset + index * 100000),
                       "monotonic_ns": index * 100000000, "data_origin": profile["data_origin"], "event": event})
    raw = b"".join(encode(frame) + b"\n" for frame in frames)
    (root / "callbacks.jsonl").write_bytes(raw)
    run_id = "fixture-" + str(offset)
    with JournalStore(root / "history.db") as store:
        feed = MarketFeed(store, run_id, profile)
        for frame in frames:
            feed.process(encode(frame) + b"\n", ready_at=frame["received_at"])
            for row in store.dispatch(run_id, feed.consumer, seen_at=frame["received_at"], consumer_epoch="unit-consumer", monotonic_ns=frame["monotonic_ns"]):
                store.acknowledge(feed.consumer, row["message_id"], at=frame["received_at"])
        boundary = store.boundary(run_id, feed.consumer, at=frames[-1]["received_at"], decision_id="fixture-end-" + str(offset))
        manifest = build_manifest(store, boundary)
    counts = {name: sum(frame["event"].get("real_type") == name for frame in frames) for name in FIDS}
    report = {"report_version": 1, "os": "Windows", "process_bits": 32, "com_created": True, "gui_loop_reached": True,
              "mock_login_confirmed": True, "login_error_code": 0, "registration_confirmed": True,
              "registration_results": [{"return_value": 0}], "error": None,
              "capture_filename": "callbacks.jsonl", "capture_sha256": digest(raw), "real_callback_counts": counts,
              "transport": {"healthy": True, "dropped": 0, "enqueued": len(frames), "fsynced": len(frames), "max_enqueue_to_fsync_ms": 1}}
    for filename, value in (("collector.json", report), ("manifest.json", manifest), ("profile.json", profile),
                            ("session.json", {"run_id": run_id, "fixture_only": True, "fsync_budget_ms": 1000})):
        write_new_json(root / filename, value)
    return profile, report


class FieldAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()

    def test_fixture_bundle_crosschecks_but_cannot_pass_as_field_evidence(self):
        fixture_bundle(self.root, observed=True)
        result = audit_session(self.root)
        self.assertFalse(result["field_checks_passed"])
        self.assertEqual(result["checks"]["not_fixture_evidence"], "FAIL")
        for name in ("all_source_bytes_preserved", "manifest_database_binding", "database_integrity", "independent_feature_recomputation", "usable_book_for_each_instrument"):
            self.assertEqual(result["checks"][name], "PASS", name)
        self.assertEqual(result["checks"]["controlled_process_restart"], "PENDING")

    def test_synthetic_origin_cannot_be_upgraded_by_a_windows_report(self):
        fixture_bundle(self.root)
        result = audit_session(self.root)
        self.assertEqual(result["checks"]["observed_origin"], "FAIL")
        self.assertEqual(result["checks"]["source_origin"], "FAIL")

    def test_report_count_or_capture_digest_tampering_is_detected(self):
        _, report = fixture_bundle(self.root, observed=True)
        report["real_callback_counts"][BOOK_TYPE] += 10
        report["capture_sha256"] = "0" * 64
        (self.root / "collector.json").write_bytes(encode(report))
        result = audit_session(self.root)
        self.assertEqual(result["checks"]["reported_counts_match_source"], "FAIL")
        self.assertEqual(result["checks"]["collector_capture_binding"], "FAIL")

    def test_latency_budget_is_recorded_and_cannot_be_silently_relaxed(self):
        _, report = fixture_bundle(self.root, observed=True)
        report["transport"]["max_enqueue_to_fsync_ms"] = 4134.619
        (self.root / "collector.json").write_bytes(encode(report))
        result = audit_session(self.root)
        self.assertEqual(result["checks"]["spool_latency"], "FAIL")
        self.assertEqual(audit_session(self.root, fsync_budget_ms=5000)["checks"]["declared_latency_budget"], "FAIL")

    def test_counter_gap_and_partial_frame_are_not_accepted(self):
        fixture_bundle(self.root)
        path = self.root / "callbacks.jsonl"
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        path.write_bytes(lines[0] + b"".join(lines[2:]))
        with self.assertRaises(IntegrityFailure): scan_capture(path)
        path.write_bytes(original[:-1])
        with self.assertRaises(ValidationError): scan_capture(path)

    def test_draft_never_copies_verified_flags(self):
        draft = make_profile(PROFILE, "PUBLICFUT", "2026-11-04")
        self.assertEqual(draft["data_origin"], "OBSERVED")
        self.assertFalse(draft["session_mapping_verified"])
        self.assertFalse(draft["instruments"][0]["spec_verified"])
        self.assertIsNone(draft["instruments"][0]["specification"]["tick_size"])
        self.assertFalse(profile_review(draft)["ready_for_field_validation"])

    def test_profile_review_requires_tick_month_and_public_evidence(self):
        profile, _ = fixture_bundle(self.root, observed=True)
        self.assertTrue(profile_review(profile)["ready_for_field_validation"])
        del profile["instruments"][0]["specification"]["verification_evidence"]
        self.assertFalse(profile_review(profile)["ready_for_field_validation"])

    def test_mapping_flag_does_not_replace_pending_session_evidence(self):
        profile, _ = fixture_bundle(self.root, observed=True)
        profile["mapping_evidence"] = "PENDING: verify futures semantics"
        self.assertFalse(profile_review(profile)["ready_for_field_validation"])

    def test_restart_cannot_be_proven_by_reusing_fixture_or_same_session(self):
        fixture_bundle(self.root, observed=True)
        prior = self.root / "previous"
        prior.mkdir()
        fixture_bundle(prior, observed=True, offset=-10000000)
        result = audit_session(self.root, previous_directory=prior)
        self.assertEqual(result["checks"]["controlled_process_restart"], "FAIL")
        with self.assertRaises(ValidationError): audit_session(self.root, previous_directory=self.root)

    def test_live_follow_drains_completed_producer_then_closes_gate(self):
        path = self.root / "callbacks.jsonl"
        path.write_bytes(b"".join(encode(frame) + b"\n" for frame in ORIGINAL[:4]))
        result = ingest_file(path, self.root / "history.db", "finished", PROFILE, follow=True, seconds=10, producer_finished=lambda: True)
        self.assertEqual(result["metrics"]["raw_frames"], 4)
        self.assertFalse(result["healthy"])

    def test_upper_bound_percentiles_and_overflow_are_explicit(self):
        hist = LatencyHistogram()
        for _ in range(99): hist.record(2)
        hist.record(4134.619)
        self.assertEqual(hist.summary()["p99_upper_ms"], 2)
        self.assertEqual(hist.summary()["max_ms"], 4134.619)
        hist.record(70000)
        self.assertEqual(hist.summary()["overflow_above_60000_ms"], 1)
        with self.assertRaises(ValueError): hist.record(float("nan"))

    def test_cli_missing_profile_evidence_returns_nonzero_without_enabling_it(self):
        fixture_bundle(self.root)
        result = subprocess.run([sys.executable, "-m", "quant_system", "review-market-profile", str(self.root / "profile.json")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(decode(result.stdout)["ready_for_field_validation"])


if __name__ == "__main__":
    unittest.main()
