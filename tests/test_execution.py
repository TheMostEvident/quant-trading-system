from copy import deepcopy
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from quant_system.contracts import validate_message
from quant_system.execution.journal import ExecutionJournal
from quant_system.execution.ledger import amount, net, reserved, valuation, validate_config
from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.models import Capture, Version
from quant_system.foundation.references import referenced_ids
from quant_system.foundation.replay import build_manifest, ReplaySession
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode

from oms_cases import Harness, config
from policy_cases import at

ROOT = Path(__file__).resolve().parents[1]


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "history.db"
        self.store = JournalStore(self.path)
        self.h = Harness(self.store)

    def tearDown(self):
        self.store.close(); self.directory.cleanup()

    def state(self):
        return self.h.journal.state()

    def qty(self):
        return net(self.state()["ledger"]["positions"][self.h.instrument])

    def value(self):
        return valuation(self.h.config, self.state())

    def opened(self, target="2", **kwargs):
        self.assertEqual(self.h.target(target)["status"], "QUEUED")
        self.h.claim()
        return self.h.report(qty=kwargs.pop("qty", str(abs(int(target)))), **kwargs)

    def spot(self):
        self.store.close()
        self.path = Path(self.directory.name) / "spot.db"
        self.store = JournalStore(self.path)
        self.h = Harness(self.store, cfg=config(spot=True))

    def test_O01_intent_and_ack_do_not_change_holdings(self):
        self.h.target(); self.assertEqual(self.qty(), 0)
        self.assertEqual(self.value()["order_reserved"], "40000")
        self.h.claim(); self.h.report("ACK")
        self.assertEqual(self.qty(), 0)
        self.assertEqual(self.state()["orders"]["intent:0"]["status"], "WORKING")

    def test_O02_partial_fill_has_quantity_margin_and_fee_postings(self):
        self.h.target(); self.h.claim(); self.h.report(qty="1", cum="1", fee="20")
        self.assertEqual(self.qty(), 1)
        self.assertEqual(self.state()["ledger"]["cash"]["KRW"], "999980")
        self.assertEqual({k: self.value()[k] for k in ("initial_used", "order_reserved", "available_margin")}, {"initial_used": "20000", "order_reserved": "20000", "available_margin": "959880"})

    def test_O03_full_fill_does_not_expense_futures_notional(self):
        self.opened()
        self.assertEqual(self.qty(), 2)
        self.assertEqual(self.state()["ledger"]["cash"]["KRW"], "999960")
        self.assertEqual(self.value()["initial_used"], "40000")
        self.assertEqual(self.value()["order_reserved"], "0")

    def test_O04_mark_and_settlement_preserve_equity(self):
        self.opened(); self.h.step("MARK", {"price": "102"})
        self.assertEqual((self.value()["equity"], self.value()["unsettled_pnl"]), ("1001960", "2000"))
        self.h.step("SETTLE", {"price": "102"})
        self.assertEqual((self.state()["ledger"]["cash"]["KRW"], self.value()["equity"], self.value()["unsettled_pnl"]), ("1001960", "1001960", "0"))
        p = self.h.journal.snapshots()[1]["payload"]
        self.assertEqual((p["avg_entry_price"], p["pnl_reference_price"]), ("101", "102"))

    def test_O05_settlement_not_counted_again_when_closing(self):
        self.opened(); self.h.step("SETTLE", {"price": "102"})
        self.h.target("0", intent="close"); self.h.claim("close:0")
        self.h.report(order="close:0", qty="2", price="103", fee="40")
        self.assertEqual(self.qty(), 0)
        self.assertEqual(self.state()["ledger"]["cash"]["KRW"], "1003920")

    def test_O06_short_profit_and_close(self):
        self.opened("-2", qty="2")
        self.assertEqual(self.qty(), -2)
        self.h.target("0", intent="close"); self.h.claim("close:0")
        self.h.report(order="close:0", qty="2", price="100", fee="40")
        self.assertEqual(self.qty(), 0)
        self.assertEqual(self.state()["ledger"]["cash"]["KRW"], "1001920")

    def test_O07_exact_report_retry_and_new_message_same_fill(self):
        self.opened()
        original = deepcopy(self.h.events[-1])
        self.h.journal.apply(original)
        duplicate = deepcopy(original); duplicate["event_id"] = "duplicate-report"
        duplicate["at"] = at(self.h.ms + 1); duplicate["clock"]["monotonic_ns"] += 1_000_000
        duplicate["data"]["report"]["message_id"] = "same-fill-new-message"
        self.assertEqual(self.h.apply(duplicate)["status"], "DUPLICATE_FILL")
        self.assertEqual((self.qty(), self.state()["ledger"]["cash"]["KRW"]), (2, "999960"))

    def test_O08_conflicting_fill_freezes_without_double_posting(self):
        self.opened(); b = deepcopy(self.h.events[-1])
        b["event_id"] = "conflicting-fill"; b["data"]["report"]["payload"]["last_fill"]["price"] = "102"
        self.assertEqual(self.h.apply(b)["status"], "REVIEW")
        self.assertTrue(self.state()["faults"])
        self.assertEqual((self.qty(), self.state()["ledger"]["cash"]["KRW"]), (2, "999960"))

    def test_O09_distinct_fill_ids_same_economic_values_are_not_duplicates(self):
        self.h.target(); self.h.claim()
        self.h.report(qty="1", cum="1", fee="20", fill_id="one")
        self.h.report(qty="1", cum="2", fee="20", fill_id="two")
        self.assertEqual((self.qty(), len(self.state()["fills"])), (2, 2))

    def test_O10_cancel_does_not_release_reservations_before_barrier(self):
        self.h.target(); self.h.claim(); self.h.report("ACK")
        self.h.step("CANCEL", {"order_id": "intent:0"}); self.h.claim(kind="CANCEL")
        self.h.report("CANCEL_ACK")
        self.assertEqual(self.qty(), 0)
        self.assertEqual(self.value()["order_reserved"], "40000")
        self.assertEqual(self.h.target(intent="replacement")["reason"], "LIVE_OR_UNCONFIRMED_ORDER")

    def test_O11_fill_during_cancel_is_posted_once(self):
        self.h.target(); self.h.claim(); self.h.report("ACK")
        self.h.step("CANCEL", {"order_id": "intent:0"})
        self.h.report(qty="1", cum="1", fee="20")
        self.assertEqual(self.state()["orders"]["intent:0"]["status"], "CANCEL_PENDING")
        self.assertEqual(self.qty(), 1)

    def test_O12_late_fill_after_cancel_ack_is_not_lost(self):
        self.h.target(); self.h.claim(); self.h.report("ACK")
        self.h.step("CANCEL", {"order_id": "intent:0"}); self.h.claim(kind="CANCEL")
        self.h.report("CANCEL_ACK")
        self.h.report(qty="1", cum="1", fee="20")
        self.assertEqual(self.qty(), 1)
        self.assertEqual(self.value()["order_reserved"], "20000")

    def test_O13_complete_cancel_reconciliation_releases_and_retries_residual(self):
        self.h.target(); self.h.claim(); self.h.report(qty="1", cum="1", fee="20")
        self.h.step("CANCEL", {"order_id": "intent:0"}); self.h.claim(kind="CANCEL")
        self.h.report("CANCEL_ACK")
        r = self.h.reconcile(open_orders=[], margin={"asset": "KRW", "initial_used": "20000", "maintenance_required": "15000", "order_reserved": "0", "available_margin": "979980"})
        self.assertEqual(r["status"], "MATCHED")
        r = self.h.target("2")
        self.assertEqual((r["status"], r["quantity"], r["order_id"]), ("QUEUED", "1", "intent:1"))

    def test_O14_cancel_reject_keeps_original_order_and_no_replacement(self):
        self.h.target(); self.h.claim(); self.h.report("ACK")
        self.h.step("CANCEL", {"order_id": "intent:0"}); self.h.claim(kind="CANCEL")
        self.h.report("CANCEL_REJECT")
        self.assertEqual(self.state()["orders"]["intent:0"]["status"], "WORKING")
        self.assertEqual(self.h.target(intent="new")["status"], "BLOCKED")

    def test_O15_dispatch_claim_is_not_repeated_even_on_same_receipt(self):
        self.h.target(); self.h.claim()
        self.assertEqual(self.h.journal.apply(self.h.events[-1]), {"status": "ALREADY_CLAIMED", "reason": "DO_NOT_RESEND"})
        r = self.h.step("DISPATCH", {"command_id": "intent:0:new:0"})
        self.assertEqual(r["reason"], "COMMAND_ALREADY_CLAIMED")

    def test_O16_unknown_send_does_not_free_reserve_or_resend(self):
        self.h.target(); self.h.claim(); self.h.step("UNCERTAIN", {"order_id": "intent:0"})
        self.assertEqual(self.value()["order_reserved"], "40000")
        self.assertEqual(self.h.target(intent="new")["reason"], "RECONCILE_OR_RESUME_REQUIRED")

    def test_O17_unknown_send_recovered_by_matching_open_order(self):
        self.h.target(); self.h.claim(); self.h.step("UNCERTAIN", {"order_id": "intent:0"})
        self.assertEqual(self.h.reconcile()["status"], "MATCHED")
        self.assertEqual(self.state()["orders"]["intent:0"]["status"], "WORKING")

    def test_O18_snapshot_recovers_missed_execution_details(self):
        self.h.target(); self.h.claim()
        missing = self.h.report(qty="2", price="101", fee="40", submit=False)
        self.h.step("UNCERTAIN", {"order_id": "intent:0"})
        r = self.h.reconcile(executions=[missing["data"]["report"]], open_orders=[], positions=[{"instrument_id": self.h.instrument, "net_qty": "2"}], balances={"KRW": "999960"},
                             margin={"asset": "KRW", "initial_used": "40000", "maintenance_required": "30000", "order_reserved": "0", "available_margin": "959960"})
        self.assertEqual(r["status"], "MATCHED")
        self.assertEqual(self.qty(), 2)

    def test_O19_snapshot_position_mismatch_does_not_overwrite_local_ledger(self):
        self.opened()
        self.assertEqual(self.h.reconcile(positions=[{"instrument_id": self.h.instrument, "net_qty": "0"}])["status"], "MISMATCH")
        self.assertEqual(self.qty(), 2)
        self.assertEqual(self.h.target("0", intent="close")["status"], "BLOCKED")

    def test_O20_balance_and_margin_mismatch_freeze(self):
        self.opened()
        self.assertEqual(self.h.reconcile(balances={"KRW": "1000000"})["reason"], "BALANCE_RECONCILIATION_MISMATCH")
        self.assertEqual(self.h.reconcile(margin={"asset": "KRW", "initial_used": "0", "maintenance_required": "0", "order_reserved": "0", "available_margin": "0"})["reason"], "MARGIN_RECONCILIATION_MISMATCH")

    def test_O21_incomplete_snapshot_cannot_recover(self):
        self.h.step("DISCONNECT")
        e = self.h.reconcile(submit=False); e["data"]["complete"] = False
        self.assertEqual(self.h.apply(e)["status"], "REVIEW")
        self.assertTrue(self.state()["faults"])

    def test_O22_transport_must_be_quiesced_before_absence_proves_cancel(self):
        self.h.target(); self.h.claim(); self.h.step("UNCERTAIN", {"order_id": "intent:0"})
        e = self.h.reconcile(open_orders=[], submit=False); e["data"]["transport_quiesced"] = False
        self.assertEqual(self.h.apply(e)["status"], "REVIEW")
        self.assertEqual(self.value()["order_reserved"], "40000")

    def test_O23_existing_fills_cannot_be_omitted_from_complete_snapshot(self):
        self.opened()
        self.assertEqual(self.h.reconcile(executions=[])["status"], "REVIEW")
        self.assertEqual(self.qty(), 2)

    def test_O24_reversal_requires_close_first(self):
        self.opened()
        self.assertEqual(self.h.target("-2", intent="reverse")["reason"], "CLOSE_BEFORE_REVERSAL")
        self.h.target("0", intent="close"); self.h.claim("close:0")
        self.h.report(order="close:0", qty="1", cum="1", fee="20")
        self.assertEqual(self.h.target("-2", intent="reverse")["status"], "BLOCKED")

    def test_O25_partial_close_replacement_cannot_overshoot(self):
        self.opened(); self.h.target("0", intent="close"); self.h.claim("close:0")
        self.h.report(order="close:0", qty="1", cum="1", fee="20")
        self.assertEqual(self.qty(), 1)
        self.assertEqual(self.h.target("0", intent="extra-close")["reason"], "LIVE_OR_UNCONFIRMED_ORDER")

    def test_O26_new_direction_after_fully_filled_close(self):
        self.opened(); self.h.target("0", intent="close"); self.h.claim("close:0")
        self.h.report(order="close:0", qty="2", fee="40")
        self.assertEqual(self.h.target("-2", intent="short")["status"], "QUEUED")
        self.assertEqual(self.qty(), 0)

    def test_O27_unsent_order_cancellation_is_local_and_safe(self):
        self.h.target(); r = self.h.step("CANCEL", {"order_id": "intent:0"})
        self.assertEqual(r["result"], "UNSENT_ORDER_CANCELED")
        self.assertEqual(self.value()["order_reserved"], "0")

    def test_O28_unsent_expiry_does_not_dispatch(self):
        self.h.target(ms=10, ttl=100)
        self.assertEqual(self.h.claim(ms=110)["status"], "EXPIRED_UNSENT")
        self.assertEqual(self.value()["order_reserved"], "0")

    def test_O29_ttl_expired_working_order_queues_one_cancel(self):
        self.h.target(ms=10, ttl=100); self.h.claim(ms=20); self.h.report("ACK", ms=30)
        self.h.step("TICK", ms=110); self.h.step("TICK", ms=120)
        self.assertEqual(sum(c["kind"] == "CANCEL" for c in self.state()["commands"].values()), 1)
        self.assertEqual(self.value()["order_reserved"], "40000")

    def test_O30_retry_cannot_reset_anchor_or_lifetime(self):
        self.h.target(); self.h.step("CANCEL", {"order_id": "intent:0"})
        r = self.h.target(price="101.2", bid="101.1", ask="101.2")
        self.assertEqual(r["reason"], "ORIGINAL_PRICE_ANCHOR_LIMIT")
        r = self.h.target(ms=1010)
        self.assertEqual(r["reason"], "INTENT_EXPIRED")

    def test_O31_retry_count_is_bounded(self):
        self.h.target(); self.h.step("CANCEL", {"order_id": "intent:0"})
        self.assertEqual(self.h.target()["order_id"], "intent:1")
        self.h.step("CANCEL", {"order_id": "intent:1"})
        self.assertEqual(self.h.target()["reason"], "RETRY_LIMIT")

    def test_O32_policy_expiry_checked_again_before_dispatch(self):
        self.h.target(ms=10, expires=50)
        self.assertEqual(self.h.claim(ms=50)["status"], "EXPIRED_UNSENT")

    def test_O33_stale_quote_and_changed_position_block(self):
        e = self.h.target(submit=False)
        e["clock"]["monotonic_ns"] += 1_000_000_000; e["at"] = at(1010)
        self.assertEqual(self.h.apply(e)["reason"], "QUOTE_EXPIRED_OR_CLOCK_UNMAPPED")

    def test_O34_halt_disconnect_and_resume_require_reconciliation(self):
        self.h.step("HALT")
        self.assertEqual(self.h.target()["status"], "BLOCKED")
        self.h.step("RESUME"); self.assertEqual(self.h.target()["status"], "QUEUED")
        self.h.step("DISCONNECT")
        self.assertEqual(self.h.step("RESUME")["status"], "BLOCKED")

    def test_O35_out_of_order_cumulative_reports_do_not_undo_fills(self):
        self.h.target(); self.h.claim()
        self.h.report(qty="1", cum="2", price="100.9", fee="20", ms=40, execution_ms=35, execution_sequence=2)
        self.assertIn("MISSING_EXECUTION_DETAILS", self.state()["faults"])
        self.h.report(qty="1", cum="1", price="101", fee="20", ms=50, execution_ms=30, execution_sequence=1)
        self.assertEqual(self.qty(), 2)
        self.assertEqual(self.state()["orders"]["intent:0"]["cum_high"], "2")
        self.assertEqual(self.state()["ledger"]["positions"][self.h.instrument]["lots"][0]["entry_price"], "101")

    def test_O36_missing_fill_details_in_ack_cause_unknown_state(self):
        self.h.target(); self.h.claim(); self.h.report("ACK", cum="1")
        self.assertTrue(self.state()["faults"]); self.assertEqual(self.qty(), 0)

    def test_O37_unknown_order_and_conflicting_broker_binding_quarantine(self):
        self.h.target(); self.h.claim()
        e = self.h.report("ACK", submit=False); e["data"]["report"]["payload"]["client_order_id"] = "unknown"
        self.assertEqual(self.h.apply(e)["status"], "REVIEW")
        self.assertEqual(self.qty(), 0)

    def test_O38_fill_outside_limit_is_recorded_and_freezes(self):
        self.opened(price="102")
        self.assertEqual(self.qty(), 2)
        self.assertIn("FILL_OUTSIDE_LIMIT_PRICE", self.state()["faults"])

    def test_O39_fill_bust_reverses_fees_and_quantity_then_requires_reconcile(self):
        r = self.opened()
        correction = self.h.report("CORRECTION", cum="0", leaves="0", correction_of=r["fill_key"], reason="BUST")
        self.assertEqual(correction["status"], "BUST_RECORDED")
        self.assertEqual(correction["reversing_postings"], [{"asset": "KRW", "amount": "40"}])
        self.assertEqual((self.qty(), self.state()["ledger"]["cash"]["KRW"]), (0, "1000000"))
        self.assertTrue(self.state()["faults"])

    def test_O40_unsupported_correction_cannot_silently_rewrite_history(self):
        self.opened()
        self.assertEqual(self.h.report("CORRECTION", correction_of="missing", reason="PRICE_CHANGE")["status"], "REVIEW")
        self.assertEqual(self.qty(), 2)

    def test_O41_fee_rebate_tax_and_foreign_asset_retained(self):
        self.h.target(); self.h.claim()
        self.h.report(charges=[{"kind": "FEE", "asset": "KRW", "amount": "-5"}, {"kind": "TAX", "asset": "KRW", "amount": "2"}, {"kind": "FEE", "asset": "BNB", "amount": "0.001"}])
        self.assertEqual(self.state()["ledger"]["cash"], {"KRW": "1000003", "BNB": "-0.001"})
        self.assertTrue(self.state()["faults"])

    def test_O42_spot_decimal_buy_sell_principal_and_fee(self):
        self.spot()
        self.h.target("0.5", fee="1"); self.h.claim()
        self.h.report(qty="0.5", cum="0.5", price="101", fee="0.5")
        self.assertEqual((self.qty(), self.state()["ledger"]["cash"]), (amount("0.5"), {"USDT": "9949", "BTC": "0.5"}))
        self.h.target("0", intent="sell", fee="1"); self.h.claim("sell:0")
        self.h.report(order="sell:0", qty="0.5", cum="0.5", price="102", fee="0.5")
        self.assertEqual(self.state()["ledger"]["cash"], {"USDT": "9999.5", "BTC": "0"})

    def test_O43_spot_base_asset_fee_changes_actual_inventory(self):
        self.spot(); self.h.target("0.5", fee="0"); self.h.claim()
        self.h.report(qty="0.5", cum="0.5", fee="0", charges=[{"kind": "FEE", "asset": "BTC", "amount": "0.001"}])
        self.assertEqual(self.qty(), amount("0.499"))
        self.assertEqual(self.state()["ledger"]["cash"]["BTC"], "0.499")

    def test_O44_spot_short_oversell_and_base_fee_reservations_block(self):
        self.spot()
        self.assertEqual(self.h.target("-0.5", fee="0")["status"], "BLOCKED")
        self.h.target("0.5", fee="0"); self.h.claim(); self.h.report(qty="0.5", cum="0.5", fee="0")
        self.assertEqual(self.h.target("0", intent="sell", fee="0.001", fee_asset="BTC")["reason"], "CASH_OR_INVENTORY_RESERVE_LIMIT")

    def test_O45_spot_insufficient_quote_cash_blocks_before_command(self):
        self.spot()
        self.assertEqual(self.h.target("9", bid="1999.9", ask="2000", fee="1")["status"], "BLOCKED")
        self.assertEqual(len(self.state()["commands"]), 0)

    def test_O46_quantity_units_and_no_implicit_money_conversion(self):
        e = self.h.target(submit=False); e["data"]["signal"]["quantity_unit"] = "BASE_ASSET"
        self.assertEqual(self.h.apply(e)["status"], "REJECTED")
        e = self.h.target(submit=False); e["data"]["policy"]["payload"]["execution_limits"]["max_order_notional"]["asset"] = "USD"
        self.assertEqual(self.h.apply(e)["reason"], "QUANTITY_OR_NOTIONAL_LIMIT")

    def test_O47_margin_notional_and_fee_limits_block(self):
        for field, amount_ in (("max_initial_margin", "1"), ("max_order_notional", "1")):
            e = self.h.target(submit=False); e["data"]["policy"]["payload"]["execution_limits"][field]["amount"] = amount_
            self.assertEqual(self.h.apply(e)["status"], "BLOCKED")
        self.assertEqual(self.h.target(fee="1000001")["status"], "BLOCKED")

    def test_O48_atomic_failure_retains_raw_without_state_or_command(self):
        e = self.h.target(submit=False)
        before = encode(self.state())
        def fail(): raise OSError("injected commit interruption")
        with self.assertRaises(OSError):
            self.h.journal.apply(e, before_commit=fail)
        self.assertEqual(encode(self.state()), before)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM oms_events").fetchone()[0], 0)
        self.assertEqual(self.h.journal.apply(e)["status"], "QUEUED")

    def test_O49_reopen_preserves_state_and_blocks_new_orders_until_reconcile(self):
        self.opened(); self.store.close(); self.store = JournalStore(self.path)
        self.h.journal = ExecutionJournal(self.store, self.h.config)
        self.assertEqual(self.qty(), 2)
        self.assertIn("JOURNAL_REOPEN_RECONCILIATION_REQUIRED", self.state()["faults"])
        self.assertEqual(self.h.target("0", intent="close")["status"], "BLOCKED")
        self.assertEqual(self.h.reconcile()["status"], "MATCHED")
        self.assertEqual(self.h.target("0", intent="close")["status"], "QUEUED")

    def test_O50_input_identity_conflict_is_a_durable_fault(self):
        e = self.h.target(submit=False); self.h.apply(e)
        e["data"]["limit_price"] = "100.9"
        self.assertEqual(self.h.apply(e)["reason"], "EVENT_ID_CONFLICT")
        self.assertTrue(self.state()["faults"])
        self.assertTrue(self.h.journal.verify()["integrity"])

    def test_O51_snapshots_contracts_and_foundation_publication(self):
        self.opened(); self.h.step("MARK", {"price": "102"})
        output = self.h.journal.publish_snapshots(["audit"])
        for m in output: validate_message(m)
        self.assertEqual(output[1]["payload"]["account_state_ref"], output[0]["message_id"])
        self.store.dispatch(self.h.config["run_id"], "audit", seen_at=at(100), consumer_epoch="audit-1", monotonic_ns=100_000_000)
        b = self.store.boundary(self.h.config["run_id"], "audit", at=at(100), decision_id="audit-boundary")
        manifest = build_manifest(self.store, b, seed=1)
        self.assertEqual(len(ReplaySession(manifest).play(target_run_id="oms-replay")), 2)
        self.assertTrue(self.h.journal.verify()["integrity"])

    def test_O52_history_is_immutable_and_causally_recomputed(self):
        self.opened(); self.h.step("MARK", {"price": "102"})
        self.assertTrue(self.h.journal.verify()["integrity"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM oms_events")

    def test_O53_no_network_transport_or_automatic_fill_generation(self):
        self.h.target(); self.h.claim()
        self.assertEqual(self.qty(), 0)
        self.assertEqual(self.state()["fills"], {})

    def test_O54_synthetic_model_and_nonlinear_or_hedge_rejected(self):
        c = config(); c["data_origin"] = "OBSERVED"
        with self.assertRaises(ValidationError): validate_config(c)
        c = config(); next(iter(c["instruments"].values()))["pnl_model"] = "INVERSE"
        with self.assertRaises(ValidationError): validate_config(c)

    def test_O55_negative_futures_price_has_signed_pnl_and_abs_notional(self):
        self.h.target(price="-1", bid="-1.1", ask="-1"); self.h.claim()
        self.h.report(price="-1")
        self.h.step("MARK", {"price": "0"})
        self.assertEqual(self.value()["equity"], "1001960")

    def test_O56_prefix_result_is_unchanged_by_future_inputs(self):
        self.opened(); before = encode(self.state())
        self.h.step("MARK", {"price": "102"})
        row = self.store.connection.execute("SELECT state_sha FROM oms_events ORDER BY seq LIMIT 1 OFFSET 2").fetchone()
        self.assertEqual(row[0], digest(before))

    def boundary_setup(self):
        e = self.h.target(submit=False)
        for index, name in enumerate(("policy", "quote"), 1):
            m = e["data"][name]
            for ref in referenced_ids(m):
                if not self.store.connection.execute("SELECT 1 FROM refs WHERE ref_id=?", (ref,)).fetchone():
                    self.store.put_artifact(ref, self.h.config["run_id"], {"synthetic_fixture_only": True}, ready_at=at(0))
            c = Capture("boundary-raw-" + name, self.h.config["run_id"], "boundary-fixture", "epoch", index, "fixture", name, encode(m), m["ready_at"], m["ready_at"], index)
            self.store.record_capture(c)
            p = m["payload"]
            v = Version("policy-key" if name == "policy" else "book-key", 1, p["valid_from"] if name == "policy" else m["ready_at"], owner_epoch=p["provider_epoch"] if name == "policy" else "initial")
            self.store.publish(c.capture_id, m, ["execution"], version=v)
        self.store.put_artifact("boundary-signal", self.h.config["run_id"], e["data"]["signal"], ready_at=e["at"])
        return e

    def submit_boundary(self, e, *, seen=True, declared=True, key="boundary"):
        refs = [e["data"]["policy"]["message_id"], e["data"]["quote"]["message_id"]]
        if seen:
            self.store.dispatch(self.h.config["run_id"], "execution", seen_at=e["at"], consumer_epoch="consumer-1", monotonic_ns=e["clock"]["monotonic_ns"])
        b = self.store.boundary(self.h.config["run_id"], "execution", at=e["at"], decision_id=key, input_refs=refs if seen and declared else [])
        return self.h.journal.submit_at_boundary(b, signal_ref="boundary-signal", policy_ref=refs[0], quote_ref=refs[1], event=e)

    def test_O57_cli_golden_worksheet_and_manifest(self):
        expected = decode((ROOT / "tests/fixtures/oms-expected.json").read_bytes())
        out = Path(self.directory.name) / "cli"
        process = subprocess.run([sys.executable, "-m", "quant_system", "oms-scenario", str(ROOT / "tests/fixtures/oms.synthetic.json"), "--output-dir", str(out)], capture_output=True, text=True, check=True)
        summary = decode(process.stdout)
        trace = decode((out / "trace.json").read_bytes())
        actual = [[row["result"]["status"], row["positions"][self.h.instrument], row["balances"]["KRW"], row["valuation"]["equity"], row["valuation"]["initial_used"], row["valuation"]["order_reserved"]] for row in trace]
        self.assertEqual(actual, expected["rows"])
        self.assertEqual(summary["broker_network_calls"], 0)
        self.assertTrue(summary["delivery_replay_speed_independent"])
        self.assertEqual(summary["integrity"]["events"], 20)
        manifest = decode((out / "manifest.json").read_bytes())
        self.assertEqual(len(ReplaySession(manifest).play(target_run_id="oms-check")), 40)
        from quant_system.execution.scenario import run_scenario
        again = run_scenario(out / "inputs.json", Path(self.directory.name) / "replay")
        self.assertEqual((summary["trace_sha256"], summary["state_sha256"]), (again["trace_sha256"], again["state_sha256"]))

    def test_O58_only_delivered_inputs_at_t_W_D_are_accepted(self):
        e = self.boundary_setup()
        with self.assertRaises(ValidationError): self.submit_boundary(e, seen=False, key="invisible")
        self.assertEqual(self.submit_boundary(e, key="visible")["status"], "QUEUED")

    def test_O59_boundary_inputs_must_be_explicitly_declared(self):
        e = self.boundary_setup()
        with self.assertRaises(ValidationError): self.submit_boundary(e, declared=False)

    def test_O60_superseded_policy_cannot_be_selected_from_visible_history(self):
        e = self.boundary_setup(); p = deepcopy(e["data"]["policy"])
        p["message_id"] = "new-policy"; p["payload"]["revision"] = 2
        c = Capture("new-policy-raw", self.h.config["run_id"], "new-policy", "epoch", 1, "fixture", "policy", encode(p), at(0), at(0), 0)
        self.store.record_capture(c)
        self.store.publish(c.capture_id, p, ["execution"], version=Version("policy-key", 2, p["payload"]["valid_from"], owner_epoch=p["payload"]["provider_epoch"]))
        with self.assertRaises(ValidationError): self.submit_boundary(e)

    def test_O61_later_book_invalidation_cannot_resurrect_old_quote(self):
        e = self.boundary_setup(); q = deepcopy(e["data"]["quote"])
        q["message_id"] = "halted-book"; q["payload"].update(update_kind="STATUS", book=None, market_status="HALTED")
        c = Capture("halted-book-raw", self.h.config["run_id"], "halt", "epoch", 1, "fixture", "status", encode(q), e["at"], e["at"], 0)
        self.store.record_capture(c); self.store.publish(c.capture_id, q, ["execution"], version=Version("book-key", 2, e["at"]))
        with self.assertRaises(ValidationError): self.submit_boundary(e)

    def test_O62_snapshot_watermark_cannot_rewrite_evidence(self):
        self.assertEqual(self.h.reconcile(watermark=1)["status"], "MATCHED")
        self.assertEqual(self.h.reconcile(watermark=1)["status"], "REVIEW")

    def test_O63_invalid_fill_does_not_partially_charge_assets(self):
        self.spot(); self.h.target("0.5", fee="0"); self.h.claim()
        r = self.h.report(qty="0.5", cum="0.5", charges=[{"kind": "FEE", "asset": "USDT", "amount": "1"}, {"kind": "FEE", "asset": "BTC", "amount": "1"}])
        self.assertEqual(r["status"], "REVIEW")
        self.assertEqual(self.state()["ledger"]["cash"], {"USDT": "10000", "BTC": "0"})
        self.assertEqual(self.qty(), 0)

    def test_O64_fifo_close_uses_exact_lots_not_display_average(self):
        self.h.target(); self.h.claim()
        self.h.report(qty="1", cum="1", price="100", fee="0")
        self.h.report(qty="1", cum="2", price="101", fee="0")
        self.h.target("1", intent="reduce"); self.h.claim("reduce:0")
        self.h.report(order="reduce:0", qty="1", cum="1", price="102", fee="0")
        self.assertEqual((self.qty(), self.state()["ledger"]["cash"]["KRW"], self.value()["equity"]), (1, "1002000", "1003000"))

    def test_O65_late_execution_restates_settlement_without_double_credit(self):
        self.h.target(); self.h.claim()
        self.h.report(qty="1", cum="2", price="101", fee="0", ms=40, execution_ms=35, execution_sequence=2)
        self.h.step("SETTLE", {"price": "102"}, ms=50)
        self.h.report(qty="1", cum="1", price="100", fee="0", ms=60, execution_ms=30, execution_sequence=1)
        self.assertEqual((self.qty(), self.state()["ledger"]["cash"]["KRW"], self.value()["unsettled_pnl"]), (2, "1003000", "0"))

    def test_O66_barrier_violation_records_actual_fill_and_stops_claims(self):
        self.h.target(); self.h.claim(); self.h.report("CANCEL_ACK")
        self.h.reconcile(open_orders=[], margin={"asset": "KRW", "initial_used": "0", "maintenance_required": "0", "order_reserved": "0", "available_margin": "1000000"})
        self.h.target(intent="replacement")
        self.h.report(qty="1", cum="1", fee="20")
        self.assertEqual(self.qty(), 1)
        self.assertIn("FILL_AFTER_RECONCILIATION_BARRIER", self.state()["faults"])
        self.assertEqual(self.h.claim("replacement:0")["status"], "BLOCKED")

    def test_O67_fractional_futures_execution_is_quarantined(self):
        self.h.target(); self.h.claim()
        self.assertEqual(self.h.report(qty="0.5", cum="0.5")["status"], "REVIEW")
        self.assertEqual(self.qty(), 0)

    def test_O68_clock_epoch_change_requires_reconciliation(self):
        e = self.h.target(submit=False); self.h.apply(e)
        e = self.h.event("DISPATCH", {"command_id": "intent:0:new:0"}); e["clock"]["epoch"] = "new-epoch"
        self.assertEqual(self.h.apply(e)["status"], "BLOCKED")

    def test_O69_abrupt_exit_before_commit_retains_raw_only(self):
        other = Path(self.directory.name) / "crash.db"
        cfg = Path(self.directory.name) / "config.json"; cfg.write_bytes(encode(config()))
        script = """import os,sys
from pathlib import Path
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode
from quant_system.execution.journal import ExecutionJournal
from oms_cases import Harness
with JournalStore(sys.argv[1]) as store:
 h=Harness(store,cfg=decode(Path(sys.argv[2]).read_bytes()))
 e=h.target(submit=False)
 h.journal.apply(e,before_commit=lambda:os._exit(23))
"""
        # Test helper availability, without importing project source into the
        # subprocess when the suite itself runs against the installed wheel.
        process = subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0," + repr(str(ROOT / "tests")) + ");" + script, str(other), str(cfg)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 23, process.stderr)
        with JournalStore(other) as store:
            j = ExecutionJournal(store, config())
            self.assertEqual(j.state()["orders"], {})
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 1)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM oms_events").fetchone()[0], 0)

    def test_O70_rehashed_projection_tampering_fails_history_check(self):
        self.opened(); state = self.state(); state["ledger"]["cash"]["KRW"] = "5"
        raw = encode(state)
        self.store.connection.execute("UPDATE oms_projection SET state=?,state_sha=?", (raw, digest(raw)))
        with self.assertRaises(IntegrityFailure): self.h.journal.verify()

    def test_O71_config_cannot_change_on_reopen(self):
        c = deepcopy(self.h.config); c["balances"]["KRW"] = "1"
        with self.assertRaises(ValidationError): ExecutionJournal(self.store, c)

    def test_O72_account_reservations_are_shared_between_instruments(self):
        self.store.close(); self.path = Path(self.directory.name) / "multi.db"; self.store = JournalStore(self.path)
        c = config(); c["balances"]["KRW"] = "70000"
        spec = deepcopy(next(iter(c["instruments"].values()))); spec["instrument_spec_ref"] = "spec-second"
        c["instruments"]["SIM:SECOND"] = spec
        self.h = Harness(self.store, cfg=c)
        self.assertEqual(self.h.target()["status"], "QUEUED")
        self.h.instrument = "SIM:SECOND"
        self.assertEqual(self.h.target(intent="second")["reason"], "MARGIN_LIMIT")
        self.assertEqual(len(self.h.journal.snapshots()), 3)

    def test_O73_partial_reduction_keeps_episode_first_fill_clock(self):
        self.opened("1", fee="0")
        before = self.state()["ledger"]["positions"][self.h.instrument]["first_fill_clock"]
        self.h.target("2", intent="increase"); self.h.claim("increase:0"); self.h.report(order="increase:0", qty="1", cum="1", fee="0")
        self.h.target("1", intent="reduce"); self.h.claim("reduce:0"); self.h.report(order="reduce:0", qty="1", cum="1", fee="0")
        self.assertEqual(self.state()["ledger"]["positions"][self.h.instrument]["first_fill_clock"], before)

    def test_O74_invalid_local_command_is_logged_without_poisoning_ledger(self):
        e = self.h.target(submit=False); e["data"]["limit_price"] = "101.01"
        self.assertEqual(self.h.apply(e)["status"], "BLOCKED")
        self.assertEqual((self.qty(), self.state()["faults"]), (0, []))

    def test_O75_foundation_fault_blocks_outbox_claim(self):
        self.h.target()
        self.store.set_health(self.h.config["run_id"], False, "TEST_STORAGE_GATE", at=at(15))
        self.assertEqual(self.h.claim()["status"], "BLOCKED")
        self.assertTrue(self.h.journal.verify()["integrity"])

    def test_O76_repeated_settlement_at_same_price_does_not_add_cash(self):
        self.opened(); self.h.step("SETTLE", {"price": "102"})
        self.h.step("SETTLE", {"price": "102"})
        self.assertEqual(self.state()["ledger"]["cash"]["KRW"], "1001960")

    def test_O77_expired_entry_policy_still_allows_bounded_reduction(self):
        self.opened()
        self.assertEqual(self.h.target("0", intent="close", expires=30)["status"], "QUEUED")
        self.assertEqual(self.h.claim("close:0")["status"], "CLAIMED")

    def test_O78_backup_contains_replayable_oms_history(self):
        self.opened()
        target = Path(self.directory.name) / "backup.db"
        self.store.backup(target)
        with JournalStore(target) as backup:
            journal = ExecutionJournal(backup, self.h.config)
            self.assertEqual(journal.verify()["events"], 3)

    def test_O79_snapshot_cannot_erase_acknowledged_missing_cumulative_fill(self):
        self.h.target(); self.h.claim(); self.h.report("ACK", cum="2", leaves="0")
        r = self.h.reconcile(open_orders=[], margin={"asset": "KRW", "initial_used": "0", "maintenance_required": "0", "order_reserved": "0", "available_margin": "1000000"})
        self.assertEqual(r["status"], "REVIEW")
        self.assertTrue(self.state()["faults"])

    def test_O80_later_identity_conflict_never_backdates_derived_snapshot(self):
        e = self.h.target(submit=False); self.h.apply(e)
        e["data"]["limit_price"] = "100.9"; e["at"] = at(100); e["clock"]["monotonic_ns"] = 100_000_000
        self.h.apply(e)
        messages = self.h.journal.publish_snapshots()
        self.assertEqual(messages[0]["ready_at"], at(100))
        self.assertTrue(self.h.journal.verify()["integrity"])

    def test_O81_margin_shortfall_stops_increase_but_permits_close(self):
        self.opened(); self.h.step("MARK", {"price": "-500"})
        self.assertEqual(self.h.target("3", intent="increase")["reason"], "MARGIN_LIMIT")
        self.assertEqual(self.h.target("0", intent="close")["status"], "QUEUED")

    def test_O82_fill_cannot_predate_first_transport_claim(self):
        self.h.target(ms=10); self.h.claim(ms=20)
        self.assertEqual(self.h.report(ms=30, execution_ms=15)["status"], "REVIEW")
        self.assertEqual(self.qty(), 0)

    def test_O83_reconciliation_cannot_assign_a_retired_broker_id_to_new_order(self):
        self.opened(); self.h.target("3", intent="increase"); self.h.claim("increase:0")
        item = {"client_order_id": "increase:0", "broker_order_id": "broker:intent:0", "side": "BUY", "position_effect": "OPEN", "order_qty": "1", "cum_filled_qty": "0", "leaves_qty": "1"}
        self.assertEqual(self.h.reconcile(open_orders=[item])["status"], "REVIEW")

    def test_O84_spot_cash_projection_and_provenance_manifest(self):
        self.spot(); self.h.target("0.5", fee="0"); self.h.claim()
        self.h.report(qty="0.5", cum="0.5", charges=[{"kind": "FEE", "asset": "BTC", "amount": "0.001"}])
        m = self.h.journal.publish_snapshots(["cash-audit"])
        self.assertIsNone(m[0]["payload"]["margin"])
        self.assertEqual(m[1]["payload"]["net_qty"], "0.499")
        self.store.dispatch(self.h.config["run_id"], "cash-audit", seen_at=at(100), consumer_epoch="cash-1", monotonic_ns=100_000_000)
        b = self.store.boundary(self.h.config["run_id"], "cash-audit", at=at(100), decision_id="cash-boundary")
        manifest = build_manifest(self.store, b)
        self.assertEqual(len(ReplaySession(manifest).play(target_run_id="cash-replay")), 2)


if __name__ == "__main__":
    unittest.main()
