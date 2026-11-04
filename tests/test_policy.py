from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from quant_system.contracts import validate_message
from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decode, digest, encode
from quant_system.strategy.engine import StaticSignalEngine
from quant_system.strategy.scenario import evaluate_scenario, run_scenario

from policy_cases import Case, at, golden_scenario

ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.case = Case()
        self.engine = StaticSignalEngine()

    def step(self, ms, **kwargs):
        return self.engine.process(self.case.bundle(ms, **kwargs))

    def single(self, edit, ms=0, **kwargs):
        b = self.case.bundle(ms, **kwargs)
        edit(b)
        return self.engine.process(b)

    def reason(self, result, expected):
        self.assertIn(expected, result["reason_codes"])
        self.assertFalse(result["entry_allowed"])
        self.assertEqual(result["orders_created"], 0)

    def test_P01_golden_long_hold_exit_reversal_short(self):
        expected = ["WAIT", "WAIT", "ENTER", "WAIT", "HOLD", "EXIT", "EXIT", "WAIT", "WAIT", "ENTER"]
        scenario = golden_scenario()
        actual = evaluate_scenario(scenario)
        self.assertEqual([d["action"] for d in actual["decisions"]], expected)
        self.assertEqual([d["target_position_qty"] for d in actual["decisions"]], [None, None, "2", None, "2", "0", "0", None, None, "-2"])
        self.assertEqual(actual["decisions"][-1]["provider_kind"], "STATIC")
        self.assertEqual(actual["decisions"][2]["observations"], 3)
        self.assertEqual(actual["decisions"][2]["duration_ns"], 200_000_000)

    def test_P02_n_and_duration_are_both_required(self):
        for t in (0, 50, 100, 199):
            self.reason(self.step(t), "PERSISTENCE_NOT_MET")
        self.assertEqual(self.step(200)["action"], "ENTER")

    def test_P03_same_instant_distinct_observations_not_elapsed_time(self):
        for _ in range(5):
            self.reason(self.step(0), "PERSISTENCE_NOT_MET")
        self.assertEqual(self.step(200)["action"], "ENTER")

    def test_P04_duplicate_book_does_not_increment_or_extend(self):
        b = self.case.bundle(0)
        self.engine.process(b)
        duplicate = deepcopy(b)
        duplicate.update(decision_id="timer", as_of=at(200))
        duplicate["clock"]["monotonic_ns"] = 200_000_000
        r = self.engine.process(duplicate)
        self.reason(r, "DUPLICATE_OBSERVATION")
        self.assertEqual((r["observations"], r["duration_ns"]), (1, 0))
        self.reason(self.step(200), "PERSISTENCE_NOT_MET")
        self.assertEqual(self.step(300)["action"], "ENTER")

    def test_P05_exact_decision_retry_is_idempotent_and_frozen(self):
        b = self.case.bundle(0)
        first = self.engine.process(b)
        first["reason_codes"] = ["mutated"]
        self.assertEqual(self.engine.process(b)["reason_codes"], ["PERSISTENCE_NOT_MET"])
        b["as_of"] = at(1)
        with self.assertRaises(ValidationError):
            self.engine.process(b)

    def test_P06_subthreshold_resets_consecutive_evidence(self):
        self.step(0); self.step(100)
        self.reason(self.step(150, bid_qty="7", ask_qty="3"), "ENTRY_THRESHOLD_NOT_MET")
        self.reason(self.step(200), "PERSISTENCE_NOT_MET")
        self.reason(self.step(300), "PERSISTENCE_NOT_MET")
        self.assertEqual(self.step(400)["action"], "ENTER")

    def test_P07_entry_threshold_equality_passes(self):
        for t in (0, 100):
            self.step(t, bid_qty="8", ask_qty="2")
        self.assertEqual(self.step(200, bid_qty="8", ask_qty="2")["action"], "ENTER")

    def test_P08_gap_boundary_resets(self):
        self.step(0); self.step(100)
        r = self.step(600)
        self.assertEqual((r["observations"], r["duration_ns"]), (1, 0))

    def test_P09_stale_gap_halt_and_trade_reset_evidence(self):
        for args in ({"flags": ["GAP"]}, {"flags": ["STALE"]}, {"status": "HALTED"}, {"kind": "TRADE"}):
            with self.subTest(args=args):
                self.setUp(); self.step(0); self.step(100)
                self.step(150, **args)
                r = self.step(200)
                self.assertEqual(r["observations"], 1)

    def test_P10_epoch_resets_persistence(self):
        self.step(0); self.step(100)
        r = self.step(200, epoch="epoch-2")
        self.assertEqual(r["observations"], 1)

    def test_P11_policy_revision_resets_persistence(self):
        self.step(0); self.step(100)
        self.case.policy_target("2", ms=150)
        self.assertEqual(self.step(200)["observations"], 1)

    def test_P12_policy_rollback_conflict_and_owner_transition(self):
        self.step(0)
        self.case.policy_target("2", revision=2, ms=50)
        self.step(100)
        self.case.policy_target("2", revision=1, ms=150)
        with self.assertRaises(ValidationError):
            self.step(200)
        self.case.policy_target("2", revision=3, ms=150)
        self.case.policy["payload"]["provider_epoch"] = "new-owner"
        b = self.case.bundle(200)
        with self.assertRaises(ValidationError):
            self.engine.process(b)
        b["policy_transition_from"] = "demo-epoch-1"
        self.assertEqual(self.engine.process(b)["observations"], 1)
        self.case.policy["payload"]["provider_epoch"] = "demo-epoch-1"
        b = self.case.bundle(300); b["policy_transition_from"] = "new-owner"
        with self.assertRaises(ValidationError):
            self.engine.process(b)

    def test_P13_layer2_and_fake_regime_rejected(self):
        for edit in (lambda p: p.update(provider_kind="LAYER2"), lambda p: p.update(regime="SQUEEZE")):
            b = self.case.bundle(0); edit(b["policy"]["payload"])
            with self.assertRaises(ValidationError):
                self.engine.process(b)

    def test_P14_policy_valid_from_inclusive_expiry_exclusive(self):
        self.case.policy["payload"].update(valid_from=at(100), expires_at=at(300))
        self.reason(self.step(0), "POLICY_NOT_ACTIVE")
        self.assertEqual(self.step(100)["observations"], 1)
        self.step(200)
        self.reason(self.step(300), "POLICY_EXPIRED_NO_INCREASE")

    def test_P15_expired_policy_still_holds_or_exits_existing(self):
        self.case.policy["payload"]["expires_at"] = at(100)
        self.case.position("2")
        self.assertEqual(self.step(100, bid_qty="6", ask_qty="4")["action"], "HOLD")
        r = self.step(200, bid_qty="119", ask_qty="81")
        self.assertEqual(r["action"], "EXIT"); self.assertTrue(r["policy_expired"])

    def test_P16_hold_boundary_inclusive_and_negative_slope_not_exit(self):
        self.case.position("2")
        self.step(0)
        r = self.step(100, bid_qty="6", ask_qty="4")
        self.assertEqual(r["action"], "HOLD")
        self.assertLess(self.case.producer._states[(self.case.pos["run_id"], self.case.pos["instrument_id"])].book.smooth, .8)

    def test_P17_short_hold_boundary(self):
        self.case.policy_target("-2")
        self.case.position("-2")
        self.assertEqual(self.step(100, bid_qty="4", ask_qty="6")["action"], "HOLD")
        self.assertEqual(self.step(200, bid_qty="81", ask_qty="119")["action"], "EXIT")

    def test_P18_slope_optional_and_directional(self):
        self.case.policy["payload"]["entry"].update(min_observations=1, min_duration_ms=0, slope_min_per_second=0)
        self.reason(self.step(0), "ENTRY_THRESHOLD_NOT_MET")
        self.assertEqual(self.step(100)["action"], "ENTER")

    def test_P19_first_fill_deadline_not_policy_or_position_update(self):
        self.case.position("1")
        self.case.policy["payload"]["hold_exit"]["max_hold_ms"] = 200
        self.assertEqual(self.step(100)["action"], "HOLD")
        self.case.policy_target("2", ms=150)
        self.case.position("1", version=3, ms=150)
        r = self.step(200)
        self.reason(r, "MAX_HOLD_REACHED"); self.assertEqual(r["target_position_qty"], "0")

    def test_P20_missing_changed_and_cross_clock_first_fill(self):
        self.case.position("2")
        self.reason(self.single(lambda b: b["evidence"].update(position_lifecycle=None), 100), "FIRST_FILL_EVIDENCE_REQUIRED")
        self.step(200)
        self.reason(self.single(lambda b: b["evidence"]["position_lifecycle"]["first_fill_clock"].update(monotonic_ns=1), 300), "FIRST_FILL_OR_DIRECTION_CHANGED_WITHOUT_FLAT")
        self.reason(self.single(lambda b: b["clock"].update(epoch="unmapped"), 400), "HOLD_CLOCK_UNVERIFIED")

    def test_P21_reversal_closes_first_and_waits_for_new_flat_version(self):
        self.case.position("2")
        self.case.policy_target("-2")
        self.reason(self.step(0, bid_qty="1", ask_qty="9"), "REVERSAL_CLOSE_FIRST")
        self.assertEqual(self.step(100, bid_qty="1", ask_qty="9")["target_position_qty"], "0")
        self.case.position("0", version=3, ms=150)
        self.assertEqual(self.step(200, bid_qty="1", ask_qty="9")["observations"], 1)

    def test_P22_pending_reservations_and_active_orders_block(self):
        for key, value in (("reserved_buy_qty", "1"), ("reserved_sell_qty", "1"), ("active_orders", ["pending-order"])):
            with self.subTest(key=key):
                self.setUp(); self.case.pos["payload"][key] = value
                self.reason(self.step(0), "PENDING_ORDERS_REQUIRE_RECONCILIATION")

    def test_P23_absolute_signed_contract_limit(self):
        self.case.policy_target("-2")
        self.reason(self.single(lambda b: b["evidence"]["risk"].update(max_abs_position_qty="1"), bid_qty="1", ask_qty="9"), "QUANTITY_LIMIT")

    def test_P24_margin_limit_and_net_reserved_not_double_subtracted(self):
        self.case.policy["payload"]["entry"].update(min_observations=1, min_duration_ms=0)
        self.case.account["payload"]["margin"].update(order_reserved="10000", available_margin="40000")
        self.assertEqual(self.step(0)["action"], "ENTER")

    def test_P25_margin_missing_unknown_negative_or_insufficient_blocks(self):
        edits = [lambda b: b["evidence"]["risk"].update(margin_quote=None),
                 lambda b: b["account"]["payload"]["margin"].update(status="UNKNOWN"),
                 lambda b: b["account"]["payload"]["margin"].update(available_margin="-1"),
                 lambda b: b["account"]["payload"]["margin"].update(available_margin="39999")]
        for edit in edits:
            self.setUp(); r = self.single(edit)
            self.assertIn(r["reason_codes"][0], ("MARGIN_UNVERIFIED", "MARGIN_LIMIT"))

    def test_P26_margin_quote_is_bound_to_state_target_and_quote(self):
        for key, value in (("account_state_ref", "old-account"), ("target_position_qty", "1"), ("quote_ref", "old-quote"), ("available_basis", "GROSS")):
            self.setUp()
            self.reason(self.single(lambda b: b["evidence"]["risk"]["margin_quote"].update({key: value})), "MARGIN_QUOTE_MISMATCH")

    def test_P27_margin_cannot_understate_incremental_required(self):
        self.reason(self.single(lambda b: b["evidence"]["risk"]["margin_quote"]["incremental_required"].update(amount="1")), "MARGIN_QUOTE_INCONSISTENT")

    def test_P28_notional_uses_multiplier_slippage_and_other_exposure(self):
        r = self.step(0)
        self.assertEqual(r["risk_checks"]["order_notional"], "200400")
        self.setUp()
        self.reason(self.single(lambda b: b["evidence"]["risk"]["other_gross_notional"].update(amount="100000")), "NOTIONAL_LIMIT")

    def test_P29_asset_mismatch_no_implicit_fx(self):
        with self.assertRaises(ValidationError):
            self.single(lambda b: b["evidence"]["risk"]["max_gross_notional"].update(asset="USDT"))

    def test_P30_spread_limit_and_quote_age_boundaries(self):
        self.case.policy["payload"]["execution_limits"]["max_spread_ticks"] = 0
        self.reason(self.step(0), "SPREAD_LIMIT")
        self.setUp()
        b = self.case.bundle(0); b["as_of"] = at(1000); b["clock"]["monotonic_ns"] = 1_000_000_000
        self.reason(self.engine.process(b), "QUOTE_CLOCK_OR_AGE_INVALID")

    def test_P31_unknown_capability_and_disallowed_short(self):
        self.case.policy_target("-2")
        self.reason(self.single(lambda b: b["evidence"]["account_capability"].update(can_open_short=None), bid_qty="1", ask_qty="9"), "DIRECTION_OR_CAPABILITY_BLOCKED")
        self.setUp(); self.case.policy_target("-2")
        self.reason(self.single(lambda b: b["evidence"]["specification"].update(can_open_short=False)), "DIRECTION_OR_CAPABILITY_BLOCKED")

    def test_P32_signal_does_not_mutate_positions_and_total_not_delta(self):
        self.case.policy["payload"]["entry"].update(min_observations=1, min_duration_ms=0)
        self.case.position("1")
        b = self.case.bundle(0); before = encode(b)
        r = self.engine.process(b)
        self.assertEqual(encode(b), before)
        self.assertEqual((r["action"], r["actual_position_qty"], r["target_position_qty"]), ("INCREASE", "1", "2"))

    def test_P33_future_input_unseen_and_wrong_scope_rejected(self):
        for edit in (lambda b: b["seen_at"].update({b["market"]["message_id"]: at(1)}), lambda b: b["market"].update(run_id="other"), lambda b: b["position"]["payload"].update(account_state_ref="old")):
            self.setUp()
            with self.assertRaises(ValidationError):
                self.single(edit)

    def test_P34_unreconciled_stale_and_evidence_expiry_block(self):
        self.reason(self.single(lambda b: b["account"]["payload"].update(reconciliation="MISMATCH")), "STATE_UNCERTAIN")
        self.setUp(); self.reason(self.step(5000), "STATE_STALE")
        self.setUp(); self.reason(self.single(lambda b: b["evidence"].update(expires_at=at(0))), "EVIDENCE_EXPIRED_OR_NOT_ACTIVE")

    def test_P35_state_version_rollback_and_content_conflict(self):
        self.step(0)
        self.case.pos["payload"]["net_qty"] = "1"
        with self.assertRaises(ValidationError):
            self.step(100)

    def test_P36_prepare_commit_failure_and_conflicting_transitions(self):
        one = self.engine.prepare(self.case.bundle(0))
        two = self.engine.prepare(self.case.bundle(100))
        self.engine.commit(one)
        with self.assertRaises(ValidationError):
            self.engine.commit(two)
        self.assertEqual(self.step(200)["observations"], 2)

    def test_P37_policy_contract_strict_and_legacy_unchanged(self):
        for change in (lambda p: p["entry"].update(slope_min_per_second=-1), lambda p: p["entry"].update(max_observation_gap_ms=0),
                       lambda p: p["hold_exit"].update(exit_target_qty="1"), lambda p: p["execution_limits"].update(price_reference_mode="FIXED_REFERENCE", price_reference_ref="anchor")):
            b = self.case.bundle(0); change(b["policy"]["payload"])
            with self.assertRaises(ValidationError):
                validate_message(b["policy"])
        legacy = deepcopy(self.case.policy)
        for key in ("strategy_version", "feature_config_ref", "state_max_age_ms"):
            legacy["payload"].pop(key)
        validate_message(legacy)
        with self.assertRaises(ValidationError):
            self.single(lambda b: b.update(policy=legacy))

    def test_P38_feature_config_smoothing_and_missing_values(self):
        self.reason(self.single(lambda b: b["evidence"]["feature_config"]["smoothing"].update(alpha=.5)), "FEATURE_DEFINITION_MISMATCH")
        self.setUp(); self.reason(self.single(lambda b: b["market"]["payload"].update(features=None)), "SUPPORTED_FEATURES_REQUIRED")

    def test_P39_missing_inputs_no_false_flat(self):
        r = self.single(lambda b: b.update(position=None))
        self.reason(r, "MISSING_STATE_OR_POLICY"); self.assertIsNone(r["actual_position_qty"])

    def test_P40_clock_regression_and_unknown_mapping(self):
        self.step(100)
        with self.assertRaises(ValidationError):
            self.step(50)
        self.setUp(); self.reason(self.single(lambda b: b["clock"].update(producer_id="consumer")), "QUOTE_CLOCK_OR_AGE_INVALID")

    def test_P41_synthetic_cannot_authorize_observed_account(self):
        with self.assertRaises(ValidationError):
            self.single(lambda b: b["account"].update(data_origin="OBSERVED"))

    def test_P42_snapshot_feature_reset_cannot_bridge_streak(self):
        self.step(0); self.step(100)
        r = self.single(lambda b: b["market"]["payload"]["features"]["details"]["flags"].append("GAP_RESET"), 200)
        self.assertEqual(r["observations"], 1)

    def test_P43_replay_prefix_unchanged_by_future_extension(self):
        value = golden_scenario()
        full = evaluate_scenario(value)
        prefix = evaluate_scenario(dict(value, bundles=value["bundles"][:4]))
        self.assertEqual(prefix["decisions"], full["decisions"][:4])

    def test_P44_cli_installed_path_and_saved_replay(self):
        value = golden_scenario()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"; path.write_bytes(encode(value))
            out = Path(directory) / "out"
            result = subprocess.run([sys.executable, "-m", "quant_system", "signal-scenario", str(path), "--output-dir", str(out)], capture_output=True, text=True, check=True)
            r = decode(result.stdout)
            self.assertTrue(r["deterministic_replay"])
            self.assertEqual(r["orders_created"], 0)
            self.assertEqual(r["positions_written"], 0)
            self.assertEqual(r["decisions_sha256"], digest(encode(decode((out / "decisions.json").read_bytes()))))
            self.assertEqual(run_scenario(out / "inputs.json")["decisions"], r["decisions"])
            with self.assertRaises(ValidationError):
                run_scenario(path, output_dir=out)

    def spot(self, qty="1.25", target="0.5"):
        b = self.case.bundle(100)
        b["position"]["payload"].update(quantity_unit="BASE_ASSET", net_qty=qty, position_status="OPEN", contract_multiplier="1", pnl_asset="USDT")
        b["policy"]["payload"].update(target_position_qty=target)
        b["evidence"]["specification"].update(asset_class="CRYPTO_SPOT", quantity_unit="BASE_ASSET", qty_step="0.01", min_qty="0.01", contract_multiplier="1", pnl_asset="USDT", quote_asset="USDT", base_asset="BTC", can_open_short=False)
        b["evidence"]["position_lifecycle"] = {"position_state_ref": b["position"]["message_id"], "first_fill_ref": "spot-fixture-fill", "first_fill_clock": {"producer_id": "producer", "epoch": "epoch-1", "monotonic_ns": 0}}
        b["account"]["payload"].update(ledger_model="CASH", margin=None)
        return b

    def test_P45_spot_reduction_and_exit_use_inventory_not_margin(self):
        r = self.engine.process(self.spot())
        self.assertEqual((r["action"], r["actual_position_qty"], r["target_position_qty"], r["quantity_unit"]), ("REDUCE", "1.25", "0.5", "BASE_ASSET"))
        self.setUp()
        self.assertEqual(self.engine.process(self.spot(target="0"))["target_position_qty"], "0")

    def test_P46_spot_short_and_oversell_reserved_block(self):
        self.reason(self.engine.process(self.spot(target="-1")), "SPOT_SHORT_UNSUPPORTED")
        self.setUp(); b = self.spot(); b["position"]["payload"]["reserved_sell_qty"] = "1.26"
        self.reason(self.engine.process(b), "SPOT_OVERSELL_RESERVED")

    def test_P47_spot_quantity_step_not_contract_integer(self):
        self.reason(self.engine.process(self.spot(target="0.501")), "TARGET_QUANTITY_INVALID")

    def test_P48_reduction_and_deadline_do_not_require_fresh_entry_quote(self):
        self.case.position("2")
        self.case.policy_target("1")
        self.reason(self.single(lambda b: b.update(market=None), 100), "POLICY_TARGET_REDUCTION")
        self.setUp(); self.case.position("2")
        self.case.policy["payload"]["hold_exit"]["max_hold_ms"] = 100
        r = self.single(lambda b: b.update(market=None), 100)
        self.assertEqual(r["action"], "EXIT"); self.assertIsNone(r["quote_ref"])

    def test_P49_expired_policy_cannot_top_up_but_allows_reduction(self):
        self.case.policy["payload"]["expires_at"] = at(100)
        self.case.position("1")
        self.reason(self.step(100), "POLICY_EXPIRED_NO_INCREASE")
        self.setUp(); self.case.policy_target("1"); self.case.position("2")
        self.case.policy["payload"]["expires_at"] = at(100)
        self.assertEqual(self.step(100)["action"], "REDUCE")

    def test_P50_pending_close_never_becomes_new_opposite_open(self):
        self.case.position("2"); self.case.policy_target("-2")
        self.case.pos["payload"].update(reserved_sell_qty="2", active_orders=["closing-order"])
        r = self.step(100, bid_qty="1", ask_qty="9")
        self.assertEqual(r["action"], "CANCEL_REVIEW"); self.assertIsNone(r["target_position_qty"])

    def test_P51_old_quote_cannot_start_new_policy_streak(self):
        b = self.case.bundle(0)
        b["policy"].update(ready_at=at(100), received_at=at(100))
        b["seen_at"][b["policy"]["message_id"]] = at(100)
        b.update(as_of=at(100)); b["clock"]["monotonic_ns"] = 100_000_000
        self.reason(self.engine.process(b), "QUOTE_PRECEDES_POLICY")

    def test_P52_persistence_does_not_count_decision_processing_delay(self):
        self.case.policy["payload"]["entry"].update(min_observations=1)
        b = self.case.bundle(0); b.update(as_of=at(200)); b["clock"]["monotonic_ns"] = 200_000_000
        r = self.engine.process(b)
        self.reason(r, "PERSISTENCE_NOT_MET"); self.assertEqual(r["duration_ns"], 0)

    def test_P53_prepare_only_never_changes_filter_evidence(self):
        self.engine.prepare(self.case.bundle(0))
        self.assertEqual(self.step(100)["observations"], 1)

    def test_P54_multiple_instruments_and_accounts_are_isolated(self):
        self.step(0); self.step(100)
        b = self.case.bundle(200)
        b["instrument_id"] = "SIM:OTHER"
        b["evidence"]["instrument_id"] = "SIM:OTHER"
        for field in ("policy", "market", "position"):
            b[field]["instrument_id"] = "SIM:OTHER"
        self.assertEqual(self.engine.process(b)["observations"], 1)

    def test_P55_explicit_wait_policy_does_not_infer_short(self):
        self.case.policy["payload"].update(allowed_position_directions=[], target_position_qty="0")
        r = self.step(0, bid_qty="1", ask_qty="9")
        self.reason(r, "TARGET_FLAT")

    def test_P56_fixture_is_fixed_and_matches_generated_inputs(self):
        path = ROOT / "tests/fixtures/static-policy.synthetic.json"
        self.assertEqual(decode(path.read_bytes()), golden_scenario())
        expected = decode((ROOT / "tests/fixtures/static-policy-expected.json").read_bytes())
        actual = evaluate_scenario(decode(path.read_bytes()))["decisions"]
        self.assertEqual([[r["action"], r["target_position_qty"], r["actual_position_qty"]] for r in actual], expected["expected"])

    def test_P57_unconfirmed_flat_transition_blocks_opening(self):
        self.case.pos["payload"]["position_status"] = "PENDING"
        self.reason(self.step(0), "POSITION_TRANSITION_UNCONFIRMED")

    def test_P58_existing_quantity_over_risk_limit_requires_review(self):
        self.case.position("2")
        self.reason(self.single(lambda b: b["evidence"]["risk"].update(max_abs_position_qty="1")), "EXISTING_QUANTITY_LIMIT")

    def test_P59_reference_content_cannot_silently_change(self):
        self.step(0)
        with self.assertRaises(ValidationError):
            self.single(lambda b: b["evidence"]["specification"].update(tick_size="0.2"), 100)

    def test_P60_wrong_feature_price_unit_coverage_or_raw_lineage(self):
        for edit in (lambda b: b["market"]["payload"]["features"]["details"].update(price_unit="USD"),
                     lambda b: b["market"]["payload"]["book"].update(coverage="DIFFERENT"),
                     lambda b: b["market"].update(lineage=["unrelated-raw"])):
            self.setUp(); self.reason(self.single(edit), "FEATURE_DEFINITION_MISMATCH")

    def test_P61_held_observations_cannot_replay_older_market(self):
        self.case.position("2")
        first = self.case.bundle(0)
        self.engine.process(first)
        self.step(100)
        first.update(as_of=at(200), decision_id="late-old-book")
        first["clock"]["monotonic_ns"] = 200_000_000
        with self.assertRaises(ValidationError):
            self.engine.process(first)

    def test_P62_spot_futures_margin_ledger_is_not_accepted(self):
        b = self.spot()
        b["account"]["payload"].update(ledger_model="FUTURES_MARGIN", margin=self.case.account["payload"]["margin"])
        self.reason(self.engine.process(b), "SPOT_REQUIRES_CASH_LEDGER")

    def test_P63_risk_failure_breaks_persistence(self):
        self.step(0); self.step(100)
        self.reason(self.single(lambda b: b["evidence"]["account_capability"].update(verified=False), 150), "DIRECTION_OR_CAPABILITY_BLOCKED")
        self.assertEqual(self.step(200)["observations"], 1)

    def test_P64_deadline_does_not_restart_on_pending_order_review(self):
        self.case.position("2")
        self.case.policy["payload"]["hold_exit"]["max_hold_ms"] = 200
        self.case.pos["payload"].update(reserved_sell_qty="1", active_orders=["close"])
        self.reason(self.step(100), "PENDING_ORDERS_REQUIRE_RECONCILIATION")
        self.case.position("2", version=3, ms=150)
        self.case.pos["payload"].update(reserved_sell_qty="0", active_orders=[])
        self.reason(self.step(200), "MAX_HOLD_REACHED")

    def test_P65_empty_scenario_and_duplicate_decisions_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate_scenario({"scenario_version": "static-scenario-v1", "bundles": []})
        b = self.case.bundle(0)
        with self.assertRaises(ValidationError):
            evaluate_scenario({"scenario_version": "static-scenario-v1", "bundles": [b, b]})

    def test_P66_existing_margin_failure_is_not_reported_as_healthy_hold(self):
        self.case.position("2")
        self.reason(self.single(lambda b: b["account"]["payload"]["margin"].update(available_margin="-1")), "EXISTING_MARGIN_REVIEW_REQUIRED")

    def test_P67_existing_notional_limit_requires_review(self):
        self.case.position("2")
        self.reason(self.single(lambda b: b["evidence"]["risk"]["other_gross_notional"].update(amount="100000")), "EXISTING_NOTIONAL_LIMIT")

    def test_P68_margin_review_does_not_block_explicit_flat_target(self):
        self.case.position("2"); self.case.policy_target("0")
        self.reason(self.single(lambda b: b["account"]["payload"].update(margin=None)), "POLICY_TARGET_FLAT")

    def test_P69_time_ema_policy_consumes_matching_producer(self):
        from quant_system.market.features import FeatureEngine
        self.case.config["smoothing"] = {"method": "TIME_EMA", "tau_ms": 500, "alpha": None}
        self.case.policy["payload"]["smoothing"] = deepcopy(self.case.config["smoothing"])
        self.case.producer = FeatureEngine(self.case.config, config_ref="feature-config")
        self.step(0); self.step(100)
        self.assertEqual(self.step(200)["action"], "ENTER")

    def test_P70_short_slope_filter_uses_directional_sign(self):
        self.case.policy_target("-2")
        self.case.policy["payload"]["entry"].update(min_observations=1, min_duration_ms=0, slope_min_per_second=.1)
        self.reason(self.step(0, bid_qty="2", ask_qty="8"), "ENTRY_THRESHOLD_NOT_MET")
        self.assertEqual(self.step(100, bid_qty="1", ask_qty="9")["action"], "ENTER")

    def test_P71_negative_future_price_uses_absolute_notional(self):
        self.case.policy["payload"]["entry"].update(min_observations=1, min_duration_ms=0)
        r = self.step(0, bid="-1.1", ask="-1.0")
        self.assertEqual(r["action"], "ENTER")
        self.assertEqual(r["risk_checks"]["order_notional"], "2400")

    def test_P72_contract_features_cannot_authorize_spot_entry(self):
        b = self.spot(qty="0", target="1")
        b["position"]["payload"]["position_status"] = "FLAT"
        b["evidence"]["position_lifecycle"] = None
        self.reason(self.engine.process(b), "SPOT_ENTRY_OR_HOLD_NOT_IMPLEMENTED")

    def test_P73_flat_with_live_order_cannot_reset_first_fill(self):
        self.case.position("2")
        self.step(100)
        self.case.position("0", version=3, ms=150)
        self.case.pos["payload"].update(reserved_sell_qty="1", active_orders=["late-closing-order"])
        self.reason(self.step(150), "PENDING_ORDERS_REQUIRE_RECONCILIATION")
        self.case.position("2", version=4, ms=200, opened_ms=200)
        self.case.pos["payload"].update(reserved_sell_qty="0", active_orders=[])
        self.reason(self.step(250), "FIRST_FILL_OR_DIRECTION_CHANGED_WITHOUT_FLAT")


if __name__ == "__main__":
    unittest.main()
