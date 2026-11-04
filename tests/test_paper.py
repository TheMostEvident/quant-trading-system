"""Hand-expected synthetic matching tests. No Windows/market acceptance claims."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from quant_system.execution.paper import MODEL, PaperBroker
from quant_system.execution.paper_scenario import run_paper_scenario
from quant_system.foundation.errors import ValidationError
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, encode
from oms_cases import config
from policy_cases import at, fixture

ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT = "SIM:KR-FUT1"


def clock(ms):
    return {"producer_id": "paper-clock", "epoch": "session-1", "monotonic_ns": ms * 1000000}


def parameters(cfg=None):
    cfg = cfg or config()
    return {"model": MODEL, "data_origin": "SYNTHETIC", "order_latency_ms": 10, "cancel_latency_ms": 20,
            "report_latency_ms": 5, "max_quote_age_ms": 1000, "participation": "1", "slippage_ticks": 0,
            "max_contracts_per_book": 100, "costs": {key: {"asset": s["pnl_asset"], "per_contract": "20", "notional_rate": "0", "tax_rate": "0"} for key, s in cfg["instruments"].items()},
            "contract_expiry": {key: at(60000) for key in cfg["instruments"]},
            "evidence": "Synthetic test assumptions, not actual exchange costs or latency"}


def command(ms=0, *, identity="one", qty="2", side="BUY", price="101.2", kind="NEW", instrument=INSTRUMENT):
    order = {"order_id": identity + ":0", "instrument_id": instrument, "quantity": qty, "side": side,
             "position_effect": "OPEN", "limit_price": price}
    return {"command_id": identity + ":" + kind, "order_id": order["order_id"], "kind": kind, "status": "SENDING", "dispatched_at": at(ms), "payload": order}


def book(ms, *, qty="10", ask="101", bid="100.9", received=None, instrument=INSTRUMENT):
    value = fixture("MarketSnapshot")
    value.update(message_id="book-" + str(ms), instrument_id=instrument, ready_at=at(ms), received_at=at(ms if received is None else received))
    value["payload"].update(update_kind="BOOK", market_status="CONTINUOUS", features=None)
    value["payload"]["book"].update(bids=[{"price": bid, "quantity": qty}], asks=[{"price": ask, "quantity": qty}], sync_status="SYNCED")
    return value


def fills(broker):
    return [x["report"]["payload"]["last_fill"] for x in broker.report_log if x["report"]["payload"]["event_type"] == "FILL"]


class MatchingTests(unittest.TestCase):
    def broker(self, **changes):
        p = parameters(); p.update(changes)
        return PaperBroker(config(), p)

    def submit(self, broker, ms=0, **kwargs):
        return broker.submit(command(ms, **kwargs), at=at(ms), clock=clock(ms))

    def observe(self, broker, ms, **kwargs):
        return broker.on_book(book(ms, **kwargs), clock=clock(ms))

    def test_no_decision_midpoint_or_pre_arrival_fill(self):
        b = self.broker(); self.submit(b)
        self.observe(b, 0); self.observe(b, 5)
        self.assertEqual(fills(b), [])
        self.observe(b, 10)
        self.assertEqual(fills(b)[0]["price"], "101")
        self.assertEqual(fills(b)[0]["executed_at"], at(10))

    def test_cached_pre_arrival_quote_cannot_fill_when_delivered_late(self):
        b = self.broker(); self.submit(b)
        self.observe(b, 20, received=5)
        self.assertEqual(fills(b), [])

    def test_report_delay_does_not_change_execution_time(self):
        b = self.broker(); self.submit(b)
        reports = self.observe(b, 20)
        self.assertEqual([r["report"]["payload"]["event_type"] for r in reports], ["ACK"])
        self.assertEqual(b.pending_reports[0]["at"], at(25))
        self.assertEqual(b.advance(at=at(25), clock=clock(25))[0]["report"]["payload"]["last_fill"]["executed_at"], at(20))

    def test_sell_uses_bid_and_supports_short_contracts(self):
        b = self.broker(); self.submit(b, side="SELL", price="100.8")
        self.observe(b, 20)
        self.assertEqual(fills(b)[0]["price"], "100.9")

    def test_depth_is_consumed_at_each_price_not_at_midpoint(self):
        b = self.broker(); self.submit(b, qty="3")
        value = book(20)
        value["payload"]["book"]["asks"] = [{"price": p, "quantity": "1"} for p in ("101", "101.1", "101.2")]
        b.on_book(value, clock=clock(20))
        self.assertEqual([f["price"] for f in fills(b)], ["101", "101.1", "101.2"])
        self.assertEqual([f["quantity"] for f in fills(b)], ["1", "1", "1"])

    def test_multiple_orders_share_observed_depth(self):
        b = self.broker(); self.submit(b, identity="first"); self.submit(b, identity="second")
        self.observe(b, 20, qty="3")
        self.assertEqual([o["filled"] for o in b.orders.values()], ["2", "1"])

    def test_identical_and_repeated_snapshots_do_not_replenish_liquidity(self):
        b = self.broker(); self.submit(b)
        self.observe(b, 20, qty="1"); self.observe(b, 20, qty="1"); self.observe(b, 30, qty="1")
        self.assertEqual([f["quantity"] for f in fills(b)], ["1"])
        self.observe(b, 40, qty="2")
        self.assertEqual([f["quantity"] for f in fills(b)], ["1", "1"])

    def test_lower_snapshot_does_not_allow_more_than_current_quantity(self):
        b = self.broker(); self.observe(b, 0, qty="10"); self.submit(b, 1, qty="10")
        self.observe(b, 20, qty="1")
        self.assertEqual(fills(b)[0]["quantity"], "1")

    def test_participation_rounds_down_to_contract_step(self):
        b = self.broker(participation="0.5"); self.submit(b, qty="4")
        self.observe(b, 20, qty="3")
        self.assertEqual(fills(b)[0]["quantity"], "1")

    def test_global_per_book_cap_is_shared(self):
        b = self.broker(max_contracts_per_book=1); self.submit(b, identity="first"); self.submit(b, identity="second")
        self.observe(b, 20)
        self.assertEqual([o["filled"] for o in b.orders.values()], ["1", "0"])

    def test_passive_touch_has_no_invented_queue_fill(self):
        b = self.broker(); self.submit(b, price="100.9")
        self.observe(b, 20)
        self.assertEqual(fills(b), [])

    def test_adverse_slippage_still_respects_limit(self):
        b = self.broker(slippage_ticks=3); self.submit(b)
        self.observe(b, 20)
        self.assertEqual(fills(b), [])
        c = self.broker(slippage_ticks=2); self.submit(c); self.observe(c, 20)
        self.assertEqual(fills(c)[0]["price"], "101.2")

    def test_fees_tax_use_quantity_and_multiplier_with_exact_amounts(self):
        p = parameters(); p["costs"][INSTRUMENT].update(notional_rate="0.0001", tax_rate="0.0002")
        b = PaperBroker(config(), p); self.submit(b); self.observe(b, 20)
        self.assertEqual([c["amount"] for c in fills(b)[0]["charges"]], ["60.2", "40.4"])

    def test_cancel_in_flight_allows_earlier_fill_then_stops_future_fill(self):
        b = self.broker(); self.submit(b); b.advance(at=at(20), clock=clock(20))
        self.submit(b, 20, kind="CANCEL")
        self.observe(b, 25, qty="1"); self.observe(b, 40, qty="10")
        self.assertEqual([f["quantity"] for f in fills(b)], ["1"])
        self.assertTrue(b.orders["one:0"]["canceled"])

    def test_full_fill_wins_before_cancel_arrival(self):
        b = self.broker(); self.submit(b); b.advance(at=at(20), clock=clock(20)); self.submit(b, 20, kind="CANCEL")
        self.observe(b, 25); b.advance(at=at(50), clock=clock(50))
        self.assertEqual(b.report_log[-1]["report"]["payload"]["event_type"], "CANCEL_REJECT")

    def test_stale_unknown_and_flagged_book_do_not_fill(self):
        for change in ("stale", "status", "quality", "sync"):
            b = self.broker(); self.submit(b)
            value = book(2000, received=1000) if change == "stale" else book(20)
            if change == "status": value["payload"]["market_status"] = "UNKNOWN"
            if change == "quality": value["quality_flags"] = ["GAP"]
            if change == "sync": value["payload"]["book"]["sync_status"] = "UNSYNCED"
            b.on_book(value, clock=clock(2000 if change == "stale" else 20))
            self.assertEqual(fills(b), [], change)

    def test_observed_origin_and_foreign_run_are_refused(self):
        for field, value in (("data_origin", "OBSERVED"), ("run_id", "other-run")):
            b = self.broker(); self.submit(b); message = book(20); message[field] = value
            with self.assertRaises(ValidationError): b.on_book(message, clock=clock(20))

    def test_command_requires_claim_and_is_idempotent(self):
        b = self.broker(); c = command(); c["status"] = "QUEUED"
        with self.assertRaises(ValidationError): b.submit(c, at=at(0), clock=clock(0))
        self.assertTrue(self.submit(b)); self.assertFalse(self.submit(b))
        changed = command(qty="3")
        with self.assertRaises(ValidationError): b.submit(changed, at=at(0), clock=clock(0))

    def test_market_identity_conflict_is_not_new_liquidity(self):
        b = self.broker(); self.submit(b); self.observe(b, 20, qty="1")
        with self.assertRaises(ValidationError): self.observe(b, 20, qty="2")

    def test_clock_domain_regression_and_inconsistent_delta_fail_closed(self):
        for next_clock in (dict(clock(10), epoch="new"), clock(-1), clock(11)):
            b = self.broker(); self.submit(b)
            with self.assertRaises(ValidationError): b.advance(at=at(10), clock=next_clock)

    def test_fractional_contract_and_off_tick_book_are_rejected(self):
        b = self.broker()
        with self.assertRaises(ValidationError): self.submit(b, qty="1.5")
        c = self.broker(); self.submit(c)
        with self.assertRaises(ValidationError): self.observe(c, 20, ask="101.05")

    def test_invalid_assumptions_cannot_become_real_costs(self):
        for changes in ({"order_latency_ms": 0}, {"data_origin": "OBSERVED"}, {"participation": "1.1"}, {"evidence": ""}):
            with self.assertRaises(ValidationError): self.broker(**changes)

    def test_expiry_stops_old_contract_and_rejects_late_arrival(self):
        b = self.broker(contract_expiry={INSTRUMENT: at(30)})
        self.submit(b); self.observe(b, 20, qty="1"); self.observe(b, 30)
        self.assertEqual([f["quantity"] for f in fills(b)], ["1"])
        self.assertEqual(b.report_log[-1]["report"]["payload"]["event_type"], "EXPIRE")
        c = self.broker(contract_expiry={INSTRUMENT: at(5)})
        self.submit(c); self.observe(c, 20)
        self.assertEqual(fills(c), [])
        self.assertEqual(c.report_log[0]["report"]["payload"]["event_type"], "REJECT")

    def test_new_contract_observation_cannot_fill_old_contract_order(self):
        cfg = config(); other = "SIM:KR-FUT2"
        cfg["instruments"][other] = deepcopy(cfg["instruments"][INSTRUMENT])
        cfg["instruments"][other]["instrument_spec_ref"] = "spec-fut-v2"
        b = PaperBroker(cfg, parameters(cfg)); self.submit(b)
        message = book(20, instrument=other); message["payload"]["instrument_spec_ref"] = "spec-fut-v2"
        b.on_book(message, clock=clock(20))
        self.assertEqual(fills(b), [])
        self.submit(b, 20, identity="new-contract", instrument=other)
        message = book(40, instrument=other); message["payload"]["instrument_spec_ref"] = "spec-fut-v2"
        b.on_book(message, clock=clock(40))
        self.assertEqual(b.orders["one:0"]["filled"], "0")
        self.assertEqual(b.orders["new-contract:0"]["filled"], "2")


class PaperScenarioTests(unittest.TestCase):
    def test_hand_expected_positions_cash_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_paper_scenario(ROOT / "tests/fixtures/paper.synthetic.json", Path(directory) / "run")
            expected = decode((ROOT / "tests/fixtures/paper-expected.json").read_bytes())
            for field, value in expected.items(): self.assertEqual(result[field], value, field)
            self.assertEqual(result["valuation"]["equity"], "1005880")
            rerun = run_paper_scenario(Path(directory) / "run/inputs.json", Path(directory) / "rerun")
            for name in ("state_sha256", "trace_sha256", "paper_state_sha256"):
                self.assertEqual(result[name], rerun[name], name)
            self.assertTrue(result["delivery_replay_speed_independent"])
            with JournalStore(Path(directory) / "run/history.db", read_only=True) as store:
                self.assertTrue(store.verify_integrity())

    def test_external_fill_injection_and_nonempty_output_are_rejected(self):
        value = decode((ROOT / "tests/fixtures/paper.synthetic.json").read_bytes())
        value["events"][0]["event"]["kind"] = "REPORT"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.json"; source.write_bytes(encode(value))
            with self.assertRaises(ValidationError): run_paper_scenario(source, Path(directory) / "output")
            occupied = Path(directory) / "occupied"; occupied.mkdir(); (occupied / "keep").write_text("keep", encoding="utf-8")
            with self.assertRaises(ValidationError): run_paper_scenario(ROOT / "tests/fixtures/paper.synthetic.json", occupied)

    def run_modified(self, value):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "inputs.json"; source.write_bytes(encode(value))
            result = run_paper_scenario(source, Path(directory) / "out")
            return result, decode((Path(directory) / "out/trace.json").read_bytes())

    def test_insufficient_margin_blocks_order_without_changing_target(self):
        value = decode((ROOT / "tests/fixtures/paper.synthetic.json").read_bytes())
        value["config"]["balances"]["KRW"] = "100"
        value["events"] = [value["events"][0], value["events"][3]]
        result, trace = self.run_modified(value)
        self.assertEqual(trace[0]["result"]["reason"], "MARGIN_LIMIT")
        self.assertEqual(result["generated_fills"], 0)
        self.assertEqual(result["positions"][INSTRUMENT], "0")

    def test_expired_unsent_intent_never_reaches_paper_transport(self):
        value = decode((ROOT / "tests/fixtures/paper.synthetic.json").read_bytes())
        value["events"][0]["event"]["data"]["policy"]["payload"]["execution_limits"]["order_ttl_ms"] = 1
        value["events"] = value["events"][:4]
        result, trace = self.run_modified(value)
        self.assertEqual(trace[1]["result"]["status"], "EXPIRED_UNSENT")
        self.assertEqual(result["generated_fills"], 0)

    def test_cancel_race_reports_reach_oms_once_without_extra_position(self):
        value = decode((ROOT / "tests/fixtures/paper.synthetic.json").read_bytes())
        source = decode((ROOT / "tests/fixtures/oms.synthetic.json").read_bytes())
        events = value["events"][:4]  # partial fill at 40, report available at 45
        events += [{"kind": "OMS", "event": source["events"][4]}, {"kind": "OMS", "event": source["events"][5]}]
        market = deepcopy(value["events"][5]); market["message"].update(message_id="cancel-race-book", ready_at=at(70), received_at=at(70))
        market["clock"]["monotonic_ns"] = 70000000
        events += [market, {"kind": "OMS", "event": source["events"][8]}]
        value["events"] = events
        result, trace = self.run_modified(value)
        self.assertEqual(result["positions"][INSTRUMENT], "2")
        self.assertEqual(result["balances"]["KRW"], "999960")
        self.assertEqual(result["generated_fills"], 2)
        self.assertEqual(result["faults"], [])

    def test_pending_fill_report_is_not_applied_at_input_eof(self):
        value = decode((ROOT / "tests/fixtures/paper.synthetic.json").read_bytes())
        value["events"] = value["events"][:4]
        result, trace = self.run_modified(value)
        self.assertEqual(result["pending_reports"], 1)
        self.assertEqual(result["positions"][INSTRUMENT], "0")
        self.assertEqual(result["balances"]["KRW"], "1000000")

    def test_cli_runs_offline_without_broker_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run([sys.executable, "-m", "quant_system", "paper-scenario", str(ROOT / "tests/fixtures/paper.synthetic.json"), "--output-dir", str(Path(directory) / "out")], text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["broker_network_calls"], 0)


if __name__ == "__main__":
    unittest.main()
