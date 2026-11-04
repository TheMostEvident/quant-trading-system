"""Integration, independent accounting and adverse synthetic execution paths."""

from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from quant_system.contracts.quality import execution_book_issues
from quant_system.execution.journal import ExecutionJournal
from quant_system.execution.ledger import net, reserved, validate_config, valuation
from quant_system.execution.paper import PaperBroker
from quant_system.execution.paper_scenario import run_paper_scenario
from quant_system.execution.scenario import run_scenario
from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode
from quant_system.validation.layer1 import run_layer1
from quant_system.validation.reference import audit_oms, economic_replay, quantity
from layer1_cases import ROOT, at, scenario
from oms_cases import Harness, config
from policy_cases import at as oms_at, Case
from test_paper import clock, command, parameters


class Layer1IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name) / "baseline"
        cls.summary = run_layer1(ROOT / "tests/fixtures/layer1.synthetic.json", cls.directory)
        cls.audit = audit_oms(cls.directory)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_hand_expected_end_to_end_pipeline_and_independent_ledger(self):
        expected = decode((ROOT / "tests/fixtures/layer1-expected.json").read_bytes())
        for key in ("positions", "balances", "generated_fills"):
            self.assertEqual(self.summary[key], expected[key], key)
        for key in ("positions", "balances", "equity", "initial_used", "maintenance_required", "charges_by_asset", "traded_notional_by_asset"):
            self.assertEqual(self.audit["independent_economics"][key], expected[key], key)
        self.assertTrue(self.audit["verified"], self.audit)
        self.assertEqual(self.audit["fill_ratio_by_instrument"], {"SIM:KR-FUT1": "1"})
        decisions = decode((self.directory / "decisions.json").read_bytes())
        self.assertEqual([[r["decision"]["action"], r["decision"]["target_position_qty"]] for r in decisions if r["oms_result"]], expected["actionable_decisions"])

    def test_book_trade_features_and_delivery_replay_are_recomputed(self):
        self.assertTrue(self.summary["replay_speed_independent"])
        self.assertTrue(self.summary["feature_recomputation"]["verified"])
        self.assertEqual(self.summary["feature_recomputation"]["book_features"], 11)
        self.assertEqual(self.summary["feature_recomputation"]["trade_features"], 1)

    def test_source_warnings_and_original_bytes_are_preserved(self):
        value = decode((self.directory / "inputs.json").read_bytes())
        original = [encode(x["frame"]) + b"\n" for x in value["events"] if x["kind"] == "CALLBACK"]
        with JournalStore(self.directory / "history.db", read_only=True) as store:
            actual = [store.raw(r[0]) for r in store.connection.execute("SELECT capture_id,metadata FROM captures ORDER BY local_sequence") if decode(r[1])["source"] == "kiwoom-openapi-plus"]
            self.assertEqual(actual, original)
            books = [decode(r[0]) for r in store.connection.execute("SELECT data FROM messages") if decode(r[0])["message_type"] == "MarketSnapshot" and decode(r[0])["payload"]["update_kind"] == "BOOK"]
            self.assertTrue(all(set(m["quality_flags"]) >= {"SOURCE_TIME_UNKNOWN", "SOURCE_SEQUENCE_UNAVAILABLE", "SOURCE_EVENT_ID_UNAVAILABLE"} for m in books))

    def test_real_boundaries_declare_delivered_inputs(self):
        with JournalStore(self.directory / "history.db", read_only=True) as store:
            targets = [decode(r[0]) for r in store.connection.execute("SELECT input FROM oms_events") if decode(r[0])["kind"] == "TARGET"]
            self.assertEqual(len(targets), 3)
            for event in targets:
                b = event["decision_boundary"]
                row = store.connection.execute("SELECT j.data FROM boundaries b JOIN journal j ON j.ingest_seq=b.seq WHERE b.decision_id=?", (b["decision_id"],)).fetchone()
                boundary = decode(row[0])
                self.assertTrue(boundary["input_refs"])
                self.assertLessEqual(set(boundary["input_refs"]), set(boundary["visible_refs"]))

    def test_saved_inputs_reproduce_state_trace_and_decisions(self):
        again = run_layer1(self.directory / "inputs.json", Path(self.temp.name) / "rerun")
        for key in ("state_sha256", "trace_sha256", "decisions_sha256"):
            self.assertEqual(again[key], self.summary[key], key)

    def test_summary_tamper_is_detected(self):
        copy = Path(self.temp.name) / "summary-tamper"; shutil.copytree(self.directory, copy)
        value = decode((copy / "summary.json").read_bytes()); value["balances"]["KRW"] = "999999"
        (copy / "summary.json").write_bytes(encode(value))
        audit = audit_oms(copy)
        self.assertEqual(audit["checks"]["database_integrity"], "PASS")
        self.assertEqual(audit["checks"]["independent_cash"], "FAIL")
        self.assertFalse(audit["verified"])

    def test_rehashed_projection_tamper_fails_independent_economics(self):
        copy = Path(self.temp.name) / "projection-tamper"; shutil.copytree(self.directory, copy)
        with JournalStore(copy / "history.db") as store:
            state = decode(store.connection.execute("SELECT state FROM oms_projection").fetchone()[0])
            state["ledger"]["cash"]["KRW"] = "999999"; raw = encode(state); sha = digest(raw)
            store.connection.execute("UPDATE oms_projection SET state=?,state_sha=?", (raw, sha))
        summary = decode((copy / "summary.json").read_bytes()); summary["state_sha256"] = sha; summary["balances"]["KRW"] = "999999"
        (copy / "summary.json").write_bytes(encode(summary))
        audit = audit_oms(copy)
        self.assertEqual(audit["checks"]["projection_digest"], "PASS")
        self.assertEqual(audit["checks"]["summary_state_binding"], "PASS")
        self.assertEqual(audit["checks"]["independent_cash"], "FAIL")

    def test_previous_paper_archive_is_independently_checked(self):
        out = Path(self.temp.name) / "paper"
        run_paper_scenario(ROOT / "tests/fixtures/paper.synthetic.json", out)
        audit = audit_oms(out)
        self.assertTrue(audit["verified"])
        self.assertEqual(audit["independent_economics"]["equity"], "1005880")

    def test_cli_audit_returns_nonzero_for_bad_economics(self):
        copy = Path(self.temp.name) / "cli-tamper"; shutil.copytree(self.directory, copy)
        value = decode((copy / "summary.json").read_bytes()); value["balances"]["KRW"] = "0"; (copy / "summary.json").write_bytes(encode(value))
        result = subprocess.run([sys.executable, "-m", "quant_system", "audit-oms", str(copy), "--output", str(copy / "audit.json")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(decode(result.stdout)["verified"])


class Layer1FaultTests(unittest.TestCase):
    def run_case(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"; path.write_bytes(encode(value)); out = Path(directory) / "out"
            result = run_layer1(path, out)
            decisions = decode((out / "decisions.json").read_bytes())
            audit = audit_oms(out); self.assertTrue(audit["verified"], audit)
            return result, decisions, audit

    def test_duplicate_callback_does_not_add_observation_or_fill(self):
        value = scenario(); duplicate = deepcopy(value["events"][2]); duplicate["ready_at"] = at(101); value["events"].insert(3, duplicate)
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["source_metrics"]["duplicates"], 1)
        self.assertEqual(result["generated_fills"], 3)
        self.assertEqual(result["balances"]["KRW"], "999680")

    def test_reversed_source_clock_blocks_entries(self):
        value = scenario(); value["events"][3]["frame"]["monotonic_ns"] = 50000000
        result, decisions, audit = self.run_case(value)
        self.assertGreater(result["source_metrics"]["quarantined"], 0)
        self.assertEqual(result["generated_fills"], 0)

    def test_sequence_gap_resets_long_entry_streak(self):
        value = scenario()
        for item in value["events"][3:]:
            if item["kind"] == "CALLBACK": item["frame"]["sequence"] += 1
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["source_metrics"]["gaps"], 1)
        self.assertEqual([d["decision"]["target_position_qty"] for d in decisions if d["oms_result"]], ["-2"])

    def test_delayed_book_does_not_complete_entry(self):
        value = scenario(); value["events"] = value["events"][:5]; value["events"][-1]["ready_at"] = at(1300)
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["generated_fills"], 0)
        self.assertGreaterEqual(result["source_metrics"]["max_transport_age_ms"], 1000)

    def test_source_disconnect_prevents_orders(self):
        value = scenario(); d = deepcopy(value["events"][0]); d["ready_at"] = at(250)
        d["frame"].update(sequence=5, received_at=at(250), monotonic_ns=250000000)
        d["frame"]["event"].update(connected=False, server="UNKNOWN")
        for item in value["events"][4:]:
            if item["kind"] == "CALLBACK": item["frame"]["sequence"] += 1
        value["events"].insert(4, d)
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["generated_fills"], 0)

    def test_halt_does_not_fabricate_emergency_fill(self):
        value = scenario(); value["events"].insert(4, {"kind": "OMS", "at": at(250), "event_kind": "HALT"})
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["generated_fills"], 0)

    def test_policy_expiry_blocks_entry(self):
        value = scenario(); value["policies"]["SIM:KR-FUT1"]["payload"]["expires_at"] = at(250)
        value["events"] = [e for e in value["events"] if e["kind"] != "POLICY" and (e.get("ready_at") or e.get("at")) < at(540)]
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["generated_fills"], 0)
        self.assertTrue(any(d["decision"]["policy_expired"] for d in decisions))

    def test_account_notional_limit(self):
        value = scenario(); value["config"]["max_gross_notional"] = "200000"
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["generated_fills"], 0)
        self.assertTrue(any("NOTIONAL_LIMIT" in d["decision"]["reason_codes"] for d in decisions))

    def test_instrument_quantity_limit(self):
        value = scenario(); value["risk"]["max_abs_position_qty"] = "1"
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["generated_fills"], 0)
        self.assertTrue(any("QUANTITY_LIMIT" in d["decision"]["reason_codes"] for d in decisions))

    def test_margin_shortfall_does_not_resize_target(self):
        value = scenario(); value["config"]["balances"]["KRW"] = "1000"
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["generated_fills"], 0)
        self.assertTrue(any("MARGIN_LIMIT" in d["decision"]["reason_codes"] for d in decisions))

    def test_eof_retains_pending_fill_and_reservation(self):
        value = scenario(); value["events"] = value["events"][:6]
        result, decisions, audit = self.run_case(value)
        self.assertEqual(result["pending_reports"], 1)
        self.assertEqual(result["positions"]["SIM:KR-FUT1"], "0")
        self.assertEqual(result["valuation"]["order_reserved"], "40000")

    def test_observed_and_unverified_profiles_stay_blocked(self):
        for edit in (lambda v: v["market_profile"].update(data_origin="OBSERVED"), lambda v: v["market_profile"].update(session_mapping_verified=False)):
            value = scenario(); edit(value)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "input.json"; path.write_bytes(encode(value))
                with self.assertRaises(ValidationError): run_layer1(path, Path(directory) / "out")


class Layer1ReferenceTests(unittest.TestCase):
    def fill(self, ms, *, qty="2", price="101", side="BUY", charges=None, sequence=1):
        return {"kind": "FILL", "at": at(ms), "sequence": sequence, "instrument_id": "SIM:KR-FUT1", "side": side,
                "quantity": qty, "price": price, "charges": charges or []}

    def test_settlement_preserves_equity_without_principal_charge(self):
        cfg = config(); fill = self.fill(10, charges=[{"kind": "FEE", "asset": "KRW", "amount": "40"}])
        mark = {"kind": "MARK", "at": at(20), "sequence": 2, "instrument_id": "SIM:KR-FUT1", "price": "102"}
        before = economic_replay(cfg, [fill, mark]); after = economic_replay(cfg, [fill, mark, dict(mark, kind="SETTLE", at=at(30), sequence=3)])
        self.assertEqual(before["equity"], "1001960")
        self.assertEqual(after["equity"], before["equity"])
        self.assertEqual(after["balances"]["KRW"], "1001960")

    def test_effective_fill_order_is_independent_of_arrival_order(self):
        first = self.fill(10, price="100"); second = self.fill(15, qty="1", price="105", side="SELL", sequence=2)
        settle = {"kind": "SETTLE", "at": at(30), "sequence": 3, "instrument_id": "SIM:KR-FUT1", "price": "110"}
        result = economic_replay(config(), [settle, second, first])
        self.assertEqual(result["balances"]["KRW"], "1015000")
        self.assertEqual(result["positions"]["SIM:KR-FUT1"], "1")

    def test_crypto_fractional_quantity_and_cost_assets_stay_separate(self):
        cfg = config(spot=True); cfg["balances"]["BNB"] = "1"
        result = economic_replay(cfg, [self.fill(10, qty="0.010", price="100", charges=[{"kind": "FEE", "asset": "BTC", "amount": "0.001"},
                     {"kind": "FEE", "asset": "USDT", "amount": "1"}, {"kind": "FEE", "asset": "BNB", "amount": "0.002"}])])
        self.assertEqual(result["balances"], {"USDT": "9998", "BTC": "0.009", "BNB": "0.998"})
        self.assertEqual(result["positions"]["SIM:KR-FUT1"], "0.009")
        self.assertEqual(result["equity"], "9998.9")
        self.assertEqual(result["charges_by_asset"], {"BTC": "0.001", "USDT": "1", "BNB": "0.002"})

    def test_invalid_inventory_fee_and_nondecimals_fail(self):
        with self.assertRaises(IntegrityFailure): economic_replay(config(spot=True), [self.fill(10, qty="0.001", charges=[{"kind": "FEE", "asset": "BTC", "amount": "0.002"}])])
        for value in ("NaN", "1e9999", 1.0, True):
            with self.assertRaises(ValidationError): quantity(value)

    def test_provenance_exemption_is_synthetic_and_narrow(self):
        message = {"data_origin": "SYNTHETIC", "quality_flags": ["SOURCE_TIME_UNKNOWN", "SOURCE_SEQUENCE_UNAVAILABLE", "SOURCE_EVENT_ID_UNAVAILABLE"]}
        self.assertEqual(execution_book_issues(message), set())
        for flag in ("GAP", "STALE", "CLOCK_UNVERIFIED", "INSTRUMENT_UNVERIFIED", "SESSION_UNVERIFIED", "UNRECOGNIZED_FLAG"):
            self.assertEqual(execution_book_issues(dict(message, quality_flags=message["quality_flags"] + [flag])), {flag})
        self.assertEqual(execution_book_issues(dict(message, data_origin="OBSERVED")), set(message["quality_flags"]))

    def test_source_spec_must_equal_ledger_spec(self):
        value = scenario(); binding = value["market_profile"]["instruments"][0]
        value["config"]["source_bindings"] = {binding["instrument_id"]: binding}
        with self.assertRaises(ValidationError): validate_config(value["config"])

    def test_new_source_clock_cannot_fill_old_order(self):
        b = PaperBroker(config(), parameters()); b.submit(command(), at=oms_at(0), clock=clock(0))
        b.on_book(Case().bundle(20)["market"], clock=clock(20))
        self.assertEqual(b.last_book_result, "SOURCE_CLOCK_UNMAPPED")
        self.assertEqual(b.fill_sequence, 0)

    def test_late_ready_quote_cannot_hide_pre_arrival_source_clock(self):
        b = PaperBroker(config(), parameters()); b.submit(command(), at=oms_at(0), clock=clock(0))
        message = Case().bundle(20)["market"]
        message["payload"]["features"]["details"]["clock"].update(producer_id="paper-clock", epoch="session-1", monotonic_ns=5000000)
        b.on_book(message, clock=clock(20))
        self.assertEqual(b.fill_sequence, 0)


class Layer1OrderFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = JournalStore(Path(self.temp.name) / "orders.db"); self.h = Harness(self.store)
    def tearDown(self): self.store.close(); self.temp.cleanup()

    def audit_history(self):
        path = Path(self.temp.name) / "events.json"
        path.write_bytes(encode({"scenario_version": "oms-scenario-v1", "config": self.h.config, "events": self.h.events}))
        out = Path(self.temp.name) / "independent"
        run_scenario(path, out)
        result = audit_oms(out)
        self.assertTrue(result["verified"], result)
        return result

    def test_duplicate_fill_changes_cash_and_quantity_once(self):
        self.h.target(); self.h.claim(); self.h.report(qty="1", cum="1", fee="20")
        duplicate = deepcopy(self.h.reports[-1]); event = self.h.event("REPORT", {"report": duplicate}); duplicate.update(message_id="duplicate-envelope", ready_at=event["at"], received_at=event["at"])
        self.assertEqual(self.h.apply(event)["status"], "DUPLICATE_FILL")
        state = self.h.journal.state()
        self.assertEqual(net(state["ledger"]["positions"][self.h.instrument]), 1)
        self.assertEqual(state["ledger"]["cash"]["KRW"], "999980")
        self.assertEqual(self.audit_history()["accepted_unique_fills"], 1)

    def test_conflicting_fill_identity_blocks_new_entry(self):
        self.h.target(); self.h.claim(); self.h.report(qty="1", cum="1", fee="20", fill_id="same")
        result = self.h.report(qty="1", cum="2", fee="20", price="101.1", fill_id="same")
        self.assertEqual(result["status"], "REVIEW")
        self.assertTrue(self.h.journal.state()["faults"])
        self.audit_history()
        self.assertEqual(self.h.target("2", intent="blocked")["reason"], "RECONCILE_OR_RESUME_REQUIRED")

    def test_reversed_cumulative_reports_preserve_effective_fifo(self):
        self.h.target(); self.h.claim()
        self.h.report(qty="1", cum="2", price="101", fee="20", ms=50, execution_ms=40, execution_sequence=2)
        self.h.report(qty="1", cum="1", price="100", fee="20", ms=60, execution_ms=30, execution_sequence=1)
        self.h.step("MARK", {"price": "102"}, ms=70)
        self.assertEqual(valuation(self.h.config, self.h.journal.state())["equity"], "1002960")
        self.assertTrue(self.h.journal.state()["faults"])

    def test_cancel_ack_keeps_reservation_for_late_fill(self):
        self.h.target(); self.h.claim(); self.h.report("ACK", qty="0"); self.h.report(qty="1", cum="1", fee="20")
        self.h.step("CANCEL", {"order_id": "intent:0"}); self.h.claim(kind="CANCEL"); self.h.report("CANCEL_ACK", qty="0", cum="1")
        self.assertEqual(reserved(self.h.journal.state()["orders"]["intent:0"]), 1)
        self.h.report(qty="1", cum="2", fee="20", execution_ms=65)
        self.assertEqual(net(self.h.journal.state()["ledger"]["positions"][self.h.instrument]), 2)
        self.assertEqual(self.h.journal.state()["ledger"]["cash"]["KRW"], "999960")
        self.audit_history()

    def test_bust_reverses_original_fill_and_costs_without_losing_raw_history(self):
        self.h.target(); self.h.claim(); self.h.report()
        self.h.step("MARK", {"price": "102"})
        key = next(iter(self.h.journal.state()["fills"]))
        self.h.report("CORRECTION", correction_of=key, reason="BUST", qty="0")
        result = self.audit_history()
        self.assertEqual(result["independent_economics"]["balances"]["KRW"], "1000000")
        self.assertEqual(result["independent_economics"]["positions"][self.h.instrument], "0")
        self.assertTrue(result["producer_faults"])

    def test_unknown_submission_blocks_retry_and_keeps_reservation(self):
        self.h.target(); self.h.claim(); self.h.step("UNCERTAIN", {"order_id": "intent:0"})
        self.assertEqual(self.h.target(intent="retry")["reason"], "RECONCILE_OR_RESUME_REQUIRED")
        self.assertEqual(reserved(self.h.journal.state()["orders"]["intent:0"]), 2)

    def test_resume_alone_cannot_clear_disconnect(self):
        self.h.step("DISCONNECT"); self.assertEqual(self.h.step("RESUME")["status"], "BLOCKED")
        self.assertEqual(self.h.target()["reason"], "RECONCILE_OR_RESUME_REQUIRED")

    def test_reversal_must_close_before_opposite_target(self):
        self.h.target(); self.h.claim(); self.h.report()
        self.assertEqual(self.h.target("-2", intent="reverse")["reason"], "CLOSE_BEFORE_REVERSAL")
        self.assertEqual(net(self.h.journal.state()["ledger"]["positions"][self.h.instrument]), 2)

    def test_live_order_blocks_second_order(self):
        self.h.target(); self.assertEqual(self.h.target(intent="second")["reason"], "LIVE_OR_UNCONFIRMED_ORDER")
        self.assertEqual(len(self.h.journal.state()["commands"]), 1)

    def test_account_gross_limit_includes_other_instrument(self):
        cfg = config(); cfg["max_gross_notional"] = "300000"; cfg["instruments"]["SIM:SECOND"] = deepcopy(cfg["instruments"][self.h.instrument]); cfg["instruments"]["SIM:SECOND"]["instrument_spec_ref"] = "second-spec"
        with JournalStore(Path(self.temp.name) / "multi.db") as store:
            h = Harness(store, cfg=cfg); h.target(); h.claim(); h.report(); h.instrument = "SIM:SECOND"
            self.assertEqual(h.target(intent="second-instrument")["reason"], "ACCOUNT_GROSS_NOTIONAL_LIMIT")

    def test_durable_claim_cannot_be_sent_twice(self):
        self.h.target(); event = self.h.event("DISPATCH", {"command_id": "intent:0:new:0"}); self.h.apply(event)
        self.assertEqual(self.h.apply(event)["status"], "ALREADY_CLAIMED")
        self.assertEqual(len(self.h.journal.state()["commands"]), 1)

    def test_journal_restart_preserves_position_and_blocks_orders(self):
        self.h.target(); self.h.claim(); self.h.report(qty="1", cum="1", fee="20")
        self.h.journal = ExecutionJournal(self.store, self.h.config)
        self.assertEqual(net(self.h.journal.state()["ledger"]["positions"][self.h.instrument]), 1)
        self.assertEqual(self.h.target(intent="after-restart")["reason"], "RECONCILE_OR_RESUME_REQUIRED")
        self.assertTrue(self.h.journal.verify()["integrity"])


if __name__ == "__main__": unittest.main()
