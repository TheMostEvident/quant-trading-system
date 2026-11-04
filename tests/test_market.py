from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.replay import ReplaySession, build_manifest, referenced_ids
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, encode, timestamp, utc_us
from quant_system.market.bridge import BridgeController, QtPort
from quant_system.market.cli import ingest_file
from quant_system.market.feed import MarketFeed, validate_frame
from quant_system.market.mapping import BOOK_TYPE, STATUS_TYPE, TRADE_TYPE
from quant_system.market.profile import validate_profile
from quant_system.market.spool import CallbackSpool

ROOT = Path(__file__).resolve().parents[1]
PROFILE = decode((ROOT / "config/kiwoom.synthetic.json").read_bytes())
FIXTURE = (ROOT / "tests/fixtures/kiwoom-callbacks.synthetic.jsonl").read_bytes().splitlines(keepends=True)
BOOK = decode(FIXTURE[2])["event"]
BASE = utc_us("2026-11-04T00:00:00.000000Z")


def at(seconds):
    return timestamp(BASE + int(seconds * 1_000_000))


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = JournalStore(self.path / "feed.db")
        self.feed = MarketFeed(self.store, "run", PROFILE)
        self.seq = 0
        self.decisions = 0
        self.ready = at(0)
        self.delivery_mono = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def frame(self, event=None, *, seconds=None, epoch="source-1", **changes):
        self.seq += 1
        seconds = self.seq / 10 if seconds is None else seconds
        frame = {"frame_version": 1, "producer_id": "producer", "epoch": epoch, "sequence": self.seq,
                 "received_at": at(seconds), "monotonic_ns": int(seconds * 1e9), "data_origin": self.feed.profile["data_origin"],
                 "event": deepcopy(event or BOOK)}
        frame.update(changes)
        return frame

    def send(self, event=None, *, ready=None, **changes):
        frame = self.frame(event, **changes)
        self.ready = ready or frame["received_at"]
        return self.feed.process(encode(frame) + b"\n", ready_at=self.ready)

    def connect(self):
        return self.send({"kind": "CONNECTION", "connected": True, "server": "MOCK", "error_code": 0})

    def status(self, status="3"):
        return self.send({"kind": "REAL", "code": "", "real_type": STATUS_TYPE, "fields": {"215": status}, "raw_real_data": "raw status"})

    def start(self):
        self.connect(); self.status()
        return self.send()[-1]

    def boundary(self, seconds=None):
        self.decisions += 1
        seen = at(seconds) if seconds is not None else timestamp(max(utc_us(self.ready), self.feed.last_ready_us or BASE))
        self.delivery_mono += 1
        rows = self.store.dispatch("run", "market", seen_at=seen, consumer_epoch="consumer", monotonic_ns=self.delivery_mono, limit=1000)
        for row in rows:
            self.store.acknowledge("market", row["message_id"], at=seen)
        return self.store.boundary("run", "market", at=seen, decision_id=str(self.decisions))

    def eligible(self, seconds=None, instrument="SIM:KR-FUT1"):
        return self.store.usable(self.boundary(seconds), "book:" + instrument)

    def test_M01_raw_preservation_and_five_level_normalization(self):
        m = self.start()
        raw = self.store.raw(m["lineage"][0])
        self.assertEqual(decode(raw)["event"]["fields"]["41"], " +101.0 ")
        self.assertEqual(m["payload"]["book"]["asks"][0]["price"], "101.0")
        self.assertEqual(len(m["payload"]["book"]["bids"]), 5)
        self.assertIsNone(m["event_at"])
        self.assertIsNone(m["source_event_id"])
        self.assertEqual(m["payload"]["features"]["feature_version"], "microstructure-v1")
        self.assertIsNone(m["payload"]["features"]["ofi_top1_delta"])
        self.assertIn("SOURCE_SEQUENCE_UNAVAILABLE", m["quality_flags"])
        self.assertTrue(self.eligible())

    def test_M02_trade_does_not_refresh_book_or_infer_aggressor(self):
        original = self.start()
        m = self.send(decode(FIXTURE[3])["event"])[-1]
        self.assertIsNone(m["payload"]["book"])
        self.assertEqual(m["payload"]["trade"], {"trade_id": None, "price": "101.0", "quantity": "2", "aggressor": "UNKNOWN"})
        self.assertEqual(self.store.latest(self.boundary(), "book:SIM:KR-FUT1")["message_id"], original["message_id"])

    def test_M03_transport_retry_deduplicates_but_equal_market_values_do_not(self):
        self.start()
        frame = self.frame(); raw = encode(frame)
        self.ready = frame["received_at"]
        first = self.feed.process(raw, ready_at=self.ready)
        self.assertEqual(self.feed.process(raw, ready_at=self.ready), [])
        second = self.send()
        self.assertNotEqual(first[-1]["message_id"], second[-1]["message_id"])
        self.assertEqual(self.feed.metrics["duplicates"], 1)

    def test_M04_conflicting_frame_identity_is_retained_and_blocks(self):
        self.start()
        frame = self.frame(); self.feed.process(encode(frame), ready_at=frame["received_at"])
        frame["event"]["fields"]["61"] = "99"
        self.ready = frame["received_at"]
        self.feed.process(encode(frame), ready_at=self.ready)
        self.assertEqual(self.feed.metrics["raw_frames"], 5)
        self.assertEqual(self.feed.metrics["quarantined"], 1)
        self.assertFalse(self.eligible())

    def test_M05_invalid_depth_is_quarantined_without_valid_book_publication(self):
        self.start()
        cases = [{"41": "0"}, {"61": "-1"}, {"61": "1.5"}, {"41": "NaN"}, {"41": "100.8"},
                 {"42": "100.0"}, {"61": "0"}, {"41": ""}]
        for change in cases:
            with self.subTest(change=change):
                event = deepcopy(BOOK); event["fields"].update(change)
                out = self.send(event)
                self.assertFalse(any(m["payload"]["update_kind"] == "BOOK" for m in out))
                self.assertFalse(self.eligible())
                self.send()
                self.assertTrue(self.eligible())
        event = deepcopy(BOOK); del event["fields"]["75"]
        self.send(event)
        self.assertFalse(self.eligible())

    def test_M06_zero_depth_is_removed_not_published_as_active_liquidity(self):
        self.start()
        event = deepcopy(BOOK); event["fields"].update({"75": "0", "55": "", "65": "0", "45": ""})
        book = self.send(event)[-1]["payload"]["book"]
        self.assertEqual((len(book["bids"]), len(book["asks"])), (4, 4))
        self.assertTrue(self.eligible())

    def test_M07_reconnect_requires_status_and_new_full_book(self):
        self.start(); self.assertTrue(self.eligible())
        self.send({"kind": "CONNECTION", "connected": False, "server": "UNKNOWN", "error_code": -106})
        self.assertFalse(self.eligible())
        self.connect(); self.send()
        self.assertFalse(self.eligible())
        self.status()
        self.assertFalse(self.eligible())
        self.send(); self.assertTrue(self.eligible())

    def test_M08_closed_or_unknown_session_cannot_reuse_previous_book(self):
        self.start(); self.status("8")
        self.assertFalse(self.eligible())
        self.status("3"); self.assertFalse(self.eligible())
        self.send(); self.assertTrue(self.eligible())
        self.status("unrecognized"); self.send()
        self.assertFalse(self.eligible())

    def test_M09_local_gap_requires_another_complete_snapshot(self):
        self.start()
        self.seq += 1
        m = self.send()[-1]
        self.assertIn("GAP", m["quality_flags"])
        self.assertFalse(self.eligible())
        self.send(); self.assertTrue(self.eligible())
        self.assertEqual(self.feed.metrics["gaps"], 1)

    def test_M10_new_producer_epoch_invalidates_session_and_old_epoch_is_rejected(self):
        self.start()
        self.seq = 0
        self.send(epoch="source-2", seconds=1)
        self.assertFalse(self.eligible())
        self.send(epoch="source-1", seconds=1.1)
        self.assertFalse(self.eligible())
        self.assertEqual(self.feed.metrics["quarantined"], 1)

    def test_M11_source_time_regression_does_not_invent_midnight_or_trading_date(self):
        event = deepcopy(BOOK); event["fields"]["21"] = "235959"
        self.connect(); self.status(); self.send(event)
        event["fields"]["21"] = "000001"
        m = self.send(event)[-1]
        self.assertIn("OUT_OF_ORDER", m["quality_flags"])
        self.assertIsNone(m["event_at"])
        self.assertFalse(self.eligible())
        self.send(); self.assertFalse(self.eligible())

    def test_M12_utc_regression_blocks_even_with_forward_source_monotonic(self):
        self.start()
        self.send(seconds=.2, monotonic_ns=400_000_000, ready=at(.4))
        self.assertTrue(self.feed.clock_fault)
        self.assertFalse(self.eligible())

    def test_M13_reversed_producer_monotonic_is_raw_preserved(self):
        self.start()
        self.send(monotonic_ns=1)
        self.assertEqual(self.feed.metrics["raw_frames"], 4)
        self.assertEqual(self.feed.metrics["quarantined"], 1)
        self.assertFalse(self.eligible())
        self.send(); self.assertFalse(self.eligible())

    def test_M14_expiry_blocks_without_watchdog_and_trade_does_not_extend_it(self):
        self.start()
        self.send(decode(FIXTURE[3])["event"])
        old = self.boundary(seconds=.5)
        self.assertTrue(self.store.usable(old, "book:SIM:KR-FUT1"))
        self.assertFalse(self.eligible(seconds=2.3))
        self.feed.tick(at=at(2.4)); self.ready = at(2.4)
        self.assertFalse(self.eligible())
        self.assertTrue(self.store.usable(old, "book:SIM:KR-FUT1"))

    def test_M15_delayed_transport_cannot_become_fresh_on_ingest(self):
        self.connect(); self.status()
        m = self.send(ready=at(2))[-1]
        self.assertIn("STALE", m["quality_flags"])
        self.assertFalse(self.eligible())
        self.send(seconds=2.1)
        self.assertTrue(self.eligible())

    def test_M16_multiple_instruments_do_not_clear_each_others_fault(self):
        self.store.close()
        self.store = JournalStore(self.path / "multi.db")
        profile = deepcopy(PROFILE)
        other = deepcopy(profile["instruments"][0]); other.update(source_code="SIMFUT2", instrument_id="SIM:KR-FUT2")
        profile["instruments"].append(other)
        self.feed = MarketFeed(self.store, "run", profile)
        self.start(); self.assertFalse(self.eligible())
        event = deepcopy(BOOK); event["code"] = "SIMFUT2"
        self.send(event)
        self.assertTrue(self.eligible()); self.assertTrue(self.eligible(instrument="SIM:KR-FUT2"))
        event["fields"]["61"] = "-1"; self.send(event)
        self.assertFalse(self.eligible())

    def test_M17_unverified_observed_session_or_spec_stays_blocked(self):
        self.store.close()
        self.store = JournalStore(self.path / "observed.db")
        profile = deepcopy(PROFILE); profile["data_origin"] = "OBSERVED"; profile["session_mapping_verified"] = False
        profile["instruments"][0].update(source_code="101TEST0", instrument_id="KRX:UNVERIFIED", spec_verified=False)
        self.feed = MarketFeed(self.store, "run", profile)
        self.connect(); self.status()
        event = deepcopy(BOOK); event["code"] = "101TEST0"
        m = self.send(event)[-1]
        self.assertEqual(m["data_origin"], "OBSERVED")
        self.assertEqual(m["payload"]["market_status"], "UNKNOWN")
        self.assertFalse(self.eligible(instrument="KRX:UNVERIFIED"))

    def test_M18_origin_mixture_is_quarantined_before_market_data(self):
        self.start()
        out = self.send(data_origin="OBSERVED")
        self.assertTrue(all(x["payload"]["update_kind"] == "STATUS" for x in out))
        self.assertEqual(self.feed.metrics["quarantined"], 1)
        self.assertFalse(self.eligible())

    def test_M19_malformed_or_missing_fields_keep_original_raw(self):
        self.start()
        bad = b'{"oops": 1}\n'
        self.feed.process(bad, ready_at=at(.4)); self.ready = at(.4)
        capture = self.store.connection.execute("SELECT capture_id FROM captures ORDER BY seq DESC LIMIT 1").fetchone()[0]
        self.assertEqual(self.store.raw(capture), bad)
        self.assertFalse(self.eligible())

    def test_M20_unsubscribed_callbacks_are_audited_without_new_book(self):
        original = self.start()
        event = deepcopy(BOOK); event["code"] = "OTHER"
        self.assertEqual(self.send(event), [])
        self.assertEqual(self.feed.metrics["ignored"], 1)
        self.assertEqual(self.store.latest(self.boundary(), "book:SIM:KR-FUT1")["message_id"], original["message_id"])

    def test_M21_manifest_resolves_profile_spec_and_status_without_network(self):
        self.start()
        boundary = self.boundary()
        manifest = build_manifest(self.store, boundary)
        with patch("socket.socket", side_effect=AssertionError("network")), patch("time.sleep", side_effect=AssertionError("sleep")):
            session = ReplaySession(manifest)
            self.assertEqual(session.trace_digest(speed=1), session.trace_digest(speed=1000))
            trace = session.play(target_run_id="replay")
            view = session.reference_view(target_run_id="replay")
        for row in trace:
            for ref in referenced_ids(row["message"]):
                self.assertIn(ref, view)
        self.assertTrue(trace[-1]["healthy"])

    def test_M22_cli_synthetic_disconnect_recovery_stale_and_replay(self):
        manifest = self.path / "manifest.json"
        run = subprocess.run([sys.executable, "-m", "quant_system", "market-ingest", "--input", str(ROOT / "tests/fixtures/kiwoom-callbacks.synthetic.jsonl"),
                              "--database", str(self.path / "cli.db"), "--run-id", "cli", "--config", str(ROOT / "config/kiwoom.synthetic.json"),
                              "--simulate-clock", "--manifest", str(manifest)], check=True, capture_output=True, text=True)
        result = decode(run.stdout)
        self.assertEqual((result["metrics"]["raw_frames"], result["metrics"]["messages"]), (9, 10))
        self.assertTrue(result["integrity"])
        self.assertFalse(result["healthy"])
        self.assertFalse(result["windows_ocx_verified"])
        self.assertEqual(result["trace_sha256"], ReplaySession(decode(manifest.read_bytes())).trace_digest())

    def test_M23_partial_jsonl_at_eof_is_preserved_and_quarantined(self):
        path = self.path / "partial.jsonl"
        path.write_bytes(FIXTURE[0].rstrip(b"\n"))
        result = ingest_file(path, self.path / "partial.db", "partial", PROFILE, simulate_clock=True)
        self.assertEqual(result["metrics"]["quarantined"], 1)
        with JournalStore(self.path / "partial.db", read_only=True) as store:
            ref = store.connection.execute("SELECT capture_id FROM captures").fetchone()[0]
            self.assertEqual(store.raw(ref), path.read_bytes())

    def test_M24_follow_waits_for_complete_frame_and_stops_with_gate_closed(self):
        path = self.path / "follow.jsonl"
        path.write_bytes(FIXTURE[0][:50])
        def append():
            with path.open("ab") as output:
                output.write(FIXTURE[0][50:]); output.flush()
        timer = threading.Timer(.05, append); timer.start()
        result = ingest_file(path, self.path / "follow.db", "follow", PROFILE, follow=True, seconds=.15)
        timer.join()
        self.assertEqual(result["metrics"]["raw_frames"], 1)
        self.assertFalse(result["healthy"])

    def test_M25_spool_health_fault_closes_follow_even_before_first_frame(self):
        path = self.path / "failed.jsonl"; path.touch()
        Path(str(path) + ".health.json").write_bytes(encode({"healthy": False}))
        result = ingest_file(path, self.path / "failed.db", "failed", PROFILE, follow=True, seconds=.1)
        self.assertFalse(result["healthy"])

    def test_M26_heartbeat_loss_is_not_repaired_by_an_old_good_quote(self):
        self.start()
        self.feed.tick(at=at(4)); self.ready = at(4)
        self.assertFalse(self.eligible())
        self.send(seconds=4.1)
        self.assertFalse(self.eligible())

    def test_M27_wrong_status_scope_cannot_mark_futures_continuous(self):
        self.start()
        status = {"kind": "REAL", "code": "005930", "real_type": STATUS_TYPE, "fields": {"215": "3"}, "raw_real_data": "wrong market"}
        self.send(status)
        self.assertFalse(self.eligible())

    def test_M28_new_writer_requires_new_run_and_fresh_state(self):
        self.start()
        with self.assertRaises(ValidationError):
            MarketFeed(self.store, "run", PROFILE)
        next_feed = MarketFeed(self.store, "next", PROFILE)
        self.assertFalse(next_feed.connected)

    def test_M29_profile_and_frame_bounds_are_explicit(self):
        for key, value in (("stale_after_ms", 0), ("heartbeat_timeout_ms", True), ("max_frame_bytes", 20 * 1024 * 1024)):
            p = deepcopy(PROFILE); p[key] = value
            with self.assertRaises(ValidationError):
                validate_profile(p)
        p = deepcopy(PROFILE); p["data_origin"] = "OBSERVED"
        with self.assertRaises(ValidationError):
            validate_profile(p)
        frame = self.frame(); frame["event"]["fields"]["61"] = 1
        with self.assertRaises(ValidationError):
            validate_frame(frame)
        with self.assertRaises(ValidationError):
            self.feed.process(b"x" * 65537, ready_at=at(1))
        self.assertFalse(self.store.storage_healthy)

    def test_M35_live_readiness_is_sampled_after_normalization(self):
        self.start()
        frame = self.frame()
        with patch("quant_system.market.feed.now_utc", side_effect=[at(.5), at(.6)]):
            m = self.feed.process(encode(frame))[-1]
        self.assertEqual(m["ready_at"], at(.6))
        self.assertGreater(utc_us(m["ready_at"]), utc_us(m["received_at"]))

    def test_M38_invalid_health_sidecar_is_fail_closed(self):
        path = self.path / "health.jsonl"; path.touch()
        Path(str(path) + ".health.json").write_bytes(b"[]")
        result = ingest_file(path, self.path / "health.db", "health", PROFILE, follow=True, seconds=.1)
        self.assertFalse(result["healthy"])
        path = self.path / "failed-offline.jsonl"
        path.write_bytes(b"".join(FIXTURE[:3]))
        Path(str(path) + ".health.json").write_bytes(encode({"healthy": False}))
        result = ingest_file(path, self.path / "offline.db", "offline", PROFILE, simulate_clock=True)
        self.assertEqual(result["metrics"]["raw_frames"], 3)
        self.assertFalse(result["healthy"])
        self.assertFalse(result["usable_instruments"]["SIM:KR-FUT1"])


class FakePort:
    def __init__(self, server="1"):
        self.server, self.calls, self.state = server, [], 1
    def login(self):
        self.calls.append("login"); return 0
    def server_kind(self):
        self.calls.append("server_kind"); return self.server
    def register(self, codes, fids):
        self.calls.append(("register", codes, fids))
    def real(self, code, fid):
        self.calls.append(("real", code, fid)); return " +101.0 " if fid == 10 else "2"
    def connected(self):
        return self.state


class Sink:
    def __init__(self):
        self.frames = []
    def submit(self, frame):
        self.frames.append(frame); return True


class BridgeTests(unittest.TestCase):
    def test_M30_fake_port_collects_original_strings_with_no_account_calls(self):
        port, sink = FakePort(), Sink()
        bridge = BridgeController(port, sink, ["101TEST0"], epoch="epoch", clock=lambda: (at(0), 0))
        bridge.login(); bridge.on_connect(0)
        frame = bridge.on_real("101TEST0", TRADE_TYPE, " raw callback ")
        validate_frame(frame)
        self.assertEqual(frame["event"]["fields"]["10"], " +101.0 ")
        self.assertEqual(frame["data_origin"], "OBSERVED")
        port.state = 0; bridge.heartbeat()
        self.assertFalse(sink.frames[-1]["event"]["connected"])
        self.assertTrue(all((c if isinstance(c, str) else c[0]) in ("login", "server_kind", "register", "real") for c in port.calls))

    def test_M31_nonmock_login_cannot_register(self):
        port, sink = FakePort(server="0"), Sink()
        bridge = BridgeController(port, sink, ["101TEST0"])
        with self.assertRaises(RuntimeError):
            bridge.on_connect(0)
        self.assertNotIn("register", [c[0] for c in port.calls if isinstance(c, tuple)])
        self.assertFalse(sink.frames[-1]["event"]["connected"])

    def test_M32_unselected_callbacks_never_read_fids(self):
        port, sink = FakePort(), Sink(); bridge = BridgeController(port, sink, ["101TEST0"])
        self.assertIsNone(bridge.on_real("OTHER", TRADE_TYPE, ""))
        self.assertIsNone(bridge.on_real("101TEST0", "주문체결", ""))
        self.assertEqual(port.calls, [])

    def test_M33_spool_flush_and_overflow_have_independent_health_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "callbacks.jsonl"
            spool = CallbackSpool(path, max_items=1)
            entered, release = threading.Event(), threading.Event()
            original = spool.output
            class SlowOutput:
                def write(self, raw):
                    entered.set(); release.wait(2); return original.write(raw)
                def flush(self): return original.flush()
                def fileno(self): return original.fileno()
                def close(self): return original.close()
            spool.output = SlowOutput()
            self.assertTrue(spool.submit({"n": 1}))
            self.assertTrue(entered.wait(1))
            self.assertTrue(spool.submit({"n": 2}))
            self.assertFalse(spool.submit({"n": 3}))
            self.assertFalse(decode(spool.health_path.read_bytes())["healthy"])
            release.set(); summary = spool.close()
            self.assertEqual([decode(line) for line in path.read_bytes().splitlines()], [{"n": 1}, {"n": 2}])
            self.assertEqual((summary["fsynced"], summary["dropped"]), (2, 1))
            with self.assertRaises(FileExistsError):
                CallbackSpool(path)

    def test_M34_qt_port_calls_only_market_and_connection_methods(self):
        class Control:
            def __init__(self): self.calls = []
            def dynamicCall(self, name, *args):
                self.calls.append((name, args)); return "1" if name.startswith("KOA_") else 0
        control = Control(); port = QtPort(control)
        port.login(); port.server_kind(); port.register(["101TEST0"], [10, 15, 20]); port.real("101TEST0", 10); port.connected(); port.close()
        methods = {c[0].split("(")[0] for c in control.calls}
        self.assertEqual(methods, {"CommConnect", "KOA_Functions", "SetRealReg", "GetCommRealData", "GetConnectState", "DisconnectRealData"})

    @unittest.skipIf(os.name == "nt", "Non-Windows guard only")
    def test_M36_standalone_probe_cannot_report_windows_success_on_linux(self):
        result = subprocess.run([sys.executable, str(ROOT / "adapters/kiwoom/collect.py"), "--probe"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires Windows", result.stderr)

    def test_M37_spool_fsync_failure_is_not_reported_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = CallbackSpool(Path(directory) / "io.jsonl")
            with patch("quant_system.market.spool.os.fsync", side_effect=OSError("injected fsync failure")):
                self.assertTrue(spool.submit({"n": 1}))
                result = spool.close()
            self.assertFalse(result["healthy"])
            self.assertEqual(result["fsynced"], 0)
            self.assertEqual(result["error"], "CALLBACK_SPOOL_IO_FAILURE")


if __name__ == "__main__":
    unittest.main()
