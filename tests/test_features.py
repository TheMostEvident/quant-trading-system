from copy import deepcopy
from decimal import localcontext
from fractions import Fraction
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from quant_system.contracts import validate_message
from quant_system.foundation.errors import IntegrityFailure, StorageFailure, ValidationError
from quant_system.foundation.replay import ReplaySession, build_manifest
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode, timestamp, utc_us
from quant_system.market.feature_replay import verify_feature_replay
from quant_system.market.features import DEFAULT_CONFIG, FeatureClock, FeatureEngine, top1_ofi, validate_config, weighted_obi
from quant_system.market.feed import MarketFeed

ROOT = Path(__file__).resolve().parents[1]
WORKSHEET = decode((ROOT / "tests/fixtures/microstructure-hand-checked.json").read_bytes())
PROFILE = decode((ROOT / "config/kiwoom.synthetic.json").read_bytes())
FRAMES = (ROOT / "tests/fixtures/kiwoom-features.synthetic.jsonl").read_bytes().splitlines(keepends=True)
BASE = utc_us("2026-11-04T00:00:00.000000Z")
SPEC = {"quantity_unit": "CONTRACT", "price_unit": "INDEX_POINT", "tick_size": "0.5"}


def config(**changes):
    value = deepcopy(DEFAULT_CONFIG)
    value.update(depth=2, weights=["1", "0.5"], smoothing={"method": "TIME_EMA", "tau_ms": 1000, "alpha": None})
    value.update(changes)
    return value


def message(index, ns, sample=None, *, kind="BOOK", qty="2", aggressor="UNKNOWN", flags=(), status="CONTINUOUS", instrument="SIM:UNIT", run="unit"):
    book = deepcopy(sample or WORKSHEET["samples"][0])
    at = timestamp(BASE + ns // 1000)
    m = {"schema_version": "0.2.0", "message_type": "MarketSnapshot", "message_id": "m" + str(index), "run_id": run,
         "instrument_id": instrument, "source": "arithmetic-fixture", "source_event_id": None, "event_at": None,
         "received_at": at, "ready_at": at, "data_origin": "SYNTHETIC", "quality_flags": list(flags), "lineage": ["raw:" + str(index)],
         "payload": {"instrument_spec_ref": "spec-unit", "trading_date": "2026-11-04", "update_kind": kind, "market_status": status,
                     "book": {"coverage": "TOP_5_CALLBACK_SNAPSHOT", "sync_status": "SYNCED", "source_sequence": None,
                              "bids": book["bids"], "asks": book["asks"]} if kind == "BOOK" else None,
                     "trade": {"trade_id": None, "price": "100", "quantity": qty, "aggressor": aggressor} if kind == "TRADE" else None,
                     "features": None}}
    return m


def clock(m, index, ns, epoch="epoch-1"):
    return FeatureClock("producer", epoch, index, ns, m["lineage"][0])


def compute(engine, index, ns, sample=None, *, specification=SPEC, epoch="epoch-1", **kwargs):
    m = message(index, ns, sample, **kwargs)
    result = engine.process(m, clock(m, index, ns, epoch), specification)
    if result is not None:
        check = deepcopy(m); check["payload"]["features"] = result
        validate_message(check)
    return result


def simple(bid_qty, ask_qty, bid="100", ask="101"):
    return {"bids": [{"price": bid, "quantity": str(bid_qty)}], "asks": [{"price": ask, "quantity": str(ask_qty)}]}


class FeatureMathTests(unittest.TestCase):
    def test_F01_frozen_arithmetic_worksheet(self):
        engine = FeatureEngine(config())
        for i, row in enumerate(WORKSHEET["samples"], 1):
            with self.subTest(row=row["id"]):
                result = compute(engine, i, row["monotonic_ns"], row)
                for key in ("obi", "smooth_obi", "obi_slope_per_second", "ofi_top1_delta", "ofi_window_sum"):
                    expected = row["expected"][key]
                    if expected is None:
                        self.assertIsNone(result[key])
                    else:
                        self.assertAlmostEqual(result[key], expected, places=12)
                rate = row["expected"]["raw_obi_slope_per_second"]
                if rate is not None:
                    self.assertAlmostEqual(result["details"]["raw_obi_slope_per_second"], rate, places=12)
                self.assertEqual(result["details"]["ofi_window_complete"], row["expected"]["ofi_window_complete"])
        self.assertEqual(result["details"]["ofi_window_count"], 2)

    def test_F02_top1_ofi_all_price_and_size_branches(self):
        old = (Fraction(100), 10, Fraction(102), 6)
        cases = [((101, 7, 102, 6), 7), ((99, 9, 102, 6), -10), ((100, 13, 102, 6), 3),
                 ((100, 10, 101, 4), -4), ((100, 10, 103, 9), 6), ((100, 10, 102, 4), 2),
                 ((101, 7, 103, 4), 13), ((99, 9, 101, 4), -14), ((100, 10, 102, 6), 0)]
        for top, expected in cases:
            with self.subTest(top=top):
                self.assertEqual(top1_ofi(old, top), expected)

    def test_F03_depth_and_weights_change_obi_with_declared_units(self):
        one = compute(FeatureEngine(config(depth=1, weights=["1"])), 1, 0)
        two = compute(FeatureEngine(config()), 1, 0)
        five = compute(FeatureEngine(), 1, 0)
        self.assertEqual(one["obi"], .25)
        self.assertAlmostEqual(two["obi"], 1 / 11)
        self.assertEqual(five["obi"], 0)
        d = two["details"]
        self.assertEqual((d["bid_depth"], d["ask_depth"], d["weighted_bid_depth"], d["weighted_ask_depth"]), ("14", "14", "12", "10"))
        self.assertEqual((d["spread"], d["mid_price"], d["spread_ticks"]), ("1", "100.5", 2))

    def test_F04_zero_denominator_is_missing_and_reseeds(self):
        self.assertIsNone(weighted_obi([], [], ["1"])[0])
        engine = FeatureEngine(config(depth=3, weights=["0", "0", "1"]))
        empty = compute(engine, 1, 0)
        self.assertIsNone(empty["obi"]); self.assertIsNone(empty["smooth_obi"])
        self.assertIn("ZERO_WEIGHTED_DEPTH", empty["details"]["flags"])
        full = deepcopy(WORKSHEET["samples"][0])
        full["bids"].append({"price": "98", "quantity": "3"})
        full["asks"].append({"price": "103", "quantity": "1"})
        result = compute(engine, 2, 100_000_000, full)
        self.assertEqual(result["obi"], .5)
        self.assertIsNone(result["ofi_top1_delta"])
        self.assertEqual(result["details"]["book_observations"], 1)

    def test_F05_time_ema_and_rates_match_independent_decimal_reference(self):
        engine = FeatureEngine(config())
        compute(engine, 1, 0)
        row = WORKSHEET["samples"][1]
        result = compute(engine, 2, 1_000_000_000, row)
        self.assertAlmostEqual(result["smooth_obi"], row["expected"]["smooth_obi"], places=14)
        self.assertNotAlmostEqual(result["obi_slope_per_second"], result["details"]["raw_obi_slope_per_second"])
        self.assertAlmostEqual(result["details"]["obi_delta_per_observation"], 54 / 253, places=14)

    def test_F06_event_ema_is_explicit_and_not_a_time_rate(self):
        engine = FeatureEngine(config(smoothing={"method": "EVENT_EMA", "tau_ms": None, "alpha": .5}))
        compute(engine, 1, 0)
        row = WORKSHEET["samples"][1]
        result = compute(engine, 2, 1_000_000_000, row)
        self.assertAlmostEqual(result["smooth_obi"], (1 / 11 + 7 / 23) / 2)
        self.assertNotAlmostEqual(result["smooth_obi"], row["expected"]["smooth_obi"])

    def test_F07_same_clock_time_has_ofi_but_no_division_by_zero(self):
        for method in ("TIME_EMA", "EVENT_EMA"):
            with self.subTest(method=method):
                smoothing = {"method": method, "tau_ms": 1000 if method == "TIME_EMA" else None, "alpha": .5 if method == "EVENT_EMA" else None}
                engine = FeatureEngine(config(depth=1, weights=["1"], smoothing=smoothing))
                compute(engine, 1, 0, simple(1, 1))
                result = compute(engine, 2, 0, simple(2, 1))
                self.assertEqual(result["ofi_top1_delta"], 1)
                self.assertIsNone(result["obi_slope_per_second"])
                self.assertIsNone(result["details"]["raw_obi_slope_per_second"])
                self.assertEqual(result["smooth_obi"], 0 if method == "TIME_EMA" else 1 / 6)
                self.assertIn("ZERO_ELAPSED_TIME", result["details"]["flags"])

    def test_F08_nanosecond_interval_keeps_small_time_ema_weight(self):
        engine = FeatureEngine(config(depth=1, weights=["1"]))
        compute(engine, 1, 0, simple(1, 1))
        result = compute(engine, 2, 1, simple(2, 1))
        self.assertGreater(result["smooth_obi"], 0)
        self.assertAlmostEqual(result["smooth_obi"], 3.3333333316666665e-10, places=20)
        self.assertTrue(math.isfinite(result["obi_slope_per_second"]))

    def test_F09_irregular_interval_semigroup_differs_from_event_count(self):
        for method, expected in (("TIME_EMA", .31606027941427883), ("EVENT_EMA", .375)):
            smoothing = {"method": method, "tau_ms": 1000 if method == "TIME_EMA" else None, "alpha": .5 if method == "EVENT_EMA" else None}
            engine = FeatureEngine(config(depth=1, weights=["1"], smoothing=smoothing))
            compute(engine, 1, 0, simple(1, 1))
            compute(engine, 2, 100_000_000, simple(3, 1))
            result = compute(engine, 3, 1_000_000_000, simple(3, 1))
            self.assertAlmostEqual(result["smooth_obi"], expected, places=14)

    def test_F10_half_open_ofi_window_excludes_exact_left_endpoint(self):
        engine = FeatureEngine(config())
        output = [compute(engine, i, row["monotonic_ns"], row) for i, row in enumerate(WORKSHEET["samples"], 1)]
        self.assertEqual([row["ofi_top1_delta"] for row in output], [None, 5, 11, -12])
        self.assertEqual([row["ofi_window_sum"] for row in output], [None, 5, 16, -1])

    def test_F11_trade_direction_unknown_is_not_signed_and_does_not_update_ema(self):
        engine = FeatureEngine(config())
        compute(engine, 1, 0)
        second = compute(engine, 2, 1_000_000_000, WORKSHEET["samples"][1])
        for i, ns, amount, direction in ((3, 1_050_000_000, "2", "BUY"), (4, 1_100_000_000, "1", "SELL"), (5, 1_150_000_000, "4", "UNKNOWN")):
            trade = compute(engine, i, ns, kind="TRADE", qty=amount, aggressor=direction)
            self.assertIsNone(trade["obi"]); self.assertIsNone(trade["smooth_obi"])
            self.assertIsNone(trade["ofi_top1_delta"])
        self.assertEqual(trade["details"]["trade_volume"], {"BUY": "2", "SELL": "1", "UNKNOWN": "4"})
        self.assertIsNone(trade["details"]["signed_trade_volume"])
        current = compute(engine, 6, 1_250_000_000, WORKSHEET["samples"][2])
        self.assertEqual(current["ofi_top1_delta"], 11)
        self.assertEqual(current["details"]["book_observations"], 3)
        self.assertAlmostEqual(current["smooth_obi"], WORKSHEET["samples"][2]["expected"]["smooth_obi"])
        self.assertIn("m5", current["basis"])
        self.assertNotEqual(second["smooth_obi"], current["smooth_obi"])

    def test_F12_trade_window_excludes_left_boundary_and_preserves_volume_unit(self):
        engine = FeatureEngine(config())
        compute(engine, 1, 0, kind="TRADE", qty="2", aggressor="BUY")
        result = compute(engine, 2, 1_000_000_000, kind="TRADE", qty="1", aggressor="SELL")
        self.assertEqual(result["details"]["trade_volume"], {"BUY": "0", "SELL": "1", "UNKNOWN": "0"})
        self.assertEqual(result["details"]["signed_trade_volume"], "-1")
        self.assertTrue(result["details"]["trade_window_complete"])

    def test_F13_quality_failures_and_noncontinuous_states_reset_history(self):
        for flag in ("GAP", "STALE", "OUT_OF_ORDER", "INVALID_BOOK", "CLOCK_UNVERIFIED", "INSTRUMENT_UNVERIFIED"):
            with self.subTest(flag=flag):
                engine = FeatureEngine(config()); compute(engine, 1, 0)
                self.assertIsNone(compute(engine, 2, 100_000_000, kind="STATUS", flags=[flag]))
                fresh = compute(engine, 3, 200_000_000, WORKSHEET["samples"][1])
                self.assertIsNone(fresh["ofi_top1_delta"])
                self.assertEqual(fresh["smooth_obi"], fresh["obi"])
        for status in ("UNKNOWN", "HALTED", "AUCTION", "CLOSED"):
            engine = FeatureEngine(config()); compute(engine, 1, 0)
            self.assertIsNone(compute(engine, 2, 100_000_000, kind="STATUS", status=status))
            self.assertIsNone(compute(engine, 3, 200_000_000)["ofi_top1_delta"])

    def test_F14_same_session_status_does_not_count_as_a_book(self):
        engine = FeatureEngine(config()); compute(engine, 1, 0)
        self.assertIsNone(compute(engine, 2, 100_000_000, kind="STATUS"))
        result = compute(engine, 3, 200_000_000, WORKSHEET["samples"][1])
        self.assertEqual(result["ofi_top1_delta"], 5)
        self.assertEqual(result["details"]["book_observations"], 2)

    def test_F15_epoch_spec_date_or_price_unit_change_reseeds(self):
        for change in ("epoch", "spec", "date", "price_unit"):
            engine = FeatureEngine(config()); compute(engine, 1, 0)
            m = message(2, 100_000_000, WORKSHEET["samples"][1]); spec = deepcopy(SPEC)
            if change == "spec": m["payload"]["instrument_spec_ref"] = "spec-new"
            if change == "date": m["payload"]["trading_date"] = "2026-11-05"
            if change == "price_unit": spec["price_unit"] = "OTHER_PRICE_UNIT"
            result = engine.process(m, clock(m, 2, 100_000_000, "new" if change == "epoch" else "epoch-1"), spec)
            self.assertIsNone(result["ofi_top1_delta"])
            self.assertEqual(result["details"]["book_observations"], 1)
            self.assertNotIn("m1", result["basis"])

    def test_F16_gap_boundary_and_source_freshness_limit_reseed(self):
        for gap in (2_000_000_000, 2_000_000_001):
            engine = FeatureEngine(config()); compute(engine, 1, 0)
            result = compute(engine, 2, gap)
            self.assertIsNone(result["ofi_top1_delta"])
            self.assertIn("GAP_RESET", result["details"]["flags"])
        engine = FeatureEngine(config(), max_gap_ms=50)
        compute(engine, 1, 0)
        self.assertIsNone(compute(engine, 2, 50_000_000)["ofi_top1_delta"])

    def test_F17_reversed_clock_or_counter_cannot_mutate_state(self):
        for sequence, ns in ((2, 1), (1, 300)):
            engine = FeatureEngine(config()); compute(engine, 1, 100)
            m = message(2, ns)
            with self.assertRaises(ValidationError):
                engine.process(m, clock(m, sequence, ns), SPEC)
            result = compute(engine, 3, 500, WORKSHEET["samples"][1])
            self.assertEqual(result["ofi_top1_delta"], 5)
            self.assertEqual(result["details"]["book_observations"], 2)

    def test_F18_duplicate_id_is_idempotent_and_result_cache_is_immutable(self):
        engine = FeatureEngine(config()); m = message(1, 0); c = clock(m, 1, 0)
        result = engine.process(m, c, SPEC); result["obi"] = 999
        again = engine.process(m, c, SPEC)
        self.assertAlmostEqual(again["obi"], 1 / 11)
        self.assertEqual(again["details"]["book_observations"], 1)
        bad = deepcopy(m); bad["payload"]["book"]["bids"][0]["quantity"] = "11"
        with self.assertRaises(ValidationError): engine.process(bad, c, SPEC)

    def test_F19_prepare_without_commit_and_stale_transition(self):
        engine = FeatureEngine(config()); m = message(1, 0)
        first = engine.prepare(m, clock(m, 1, 0), SPEC)
        other = message(2, 100)
        pending = engine.prepare(other, clock(other, 2, 100), SPEC)
        self.assertIsNone(pending.features["ofi_top1_delta"])
        engine.commit(first)
        with self.assertRaises(ValidationError): engine.commit(pending)

    def test_F20_bounded_ofi_window_reports_missing_sum_until_recovery(self):
        engine = FeatureEngine(config(depth=1, weights=["1"], max_window_events=2))
        for i in range(1, 5): result = compute(engine, i, (i - 1) * 100_000_000, simple(9 + i, 6))
        self.assertIsNone(result["ofi_window_sum"])
        self.assertEqual(result["details"]["ofi_window_count"], 2)
        self.assertIn("OFI_WINDOW_OVERFLOW", result["details"]["flags"])
        recovered = compute(engine, 5, 1_300_000_000, simple(14, 6))
        self.assertEqual(recovered["ofi_window_sum"], 1)
        self.assertTrue(recovered["details"]["ofi_window_complete"])

    def test_F21_bounded_trade_window_never_labels_partial_volume_as_complete(self):
        engine = FeatureEngine(config(max_window_events=2))
        for i in range(1, 4): result = compute(engine, i, (i - 1) * 100_000_000, kind="TRADE", qty="1", aggressor="BUY")
        self.assertEqual(result["details"]["trade_volume"], {"BUY": None, "SELL": None, "UNKNOWN": None})
        self.assertFalse(result["details"]["trade_window_complete"])
        recovered = compute(engine, 4, 1_300_000_000, kind="TRADE", qty="2", aggressor="SELL")
        self.assertEqual(recovered["details"]["signed_trade_volume"], "-2")
        self.assertTrue(recovered["details"]["trade_window_complete"])

    def test_F22_decimal_context_cannot_round_prices_or_weighted_depth(self):
        engine = FeatureEngine(config())
        with localcontext() as context:
            context.prec = 3
            result = compute(engine, 1, 0)
            high = simple(10, 6, "1000000.123456789", "1000000.123456791")
            prices = compute(FeatureEngine(config(depth=1, weights=["1"])), 1, 0, high)
        self.assertAlmostEqual(result["obi"], 1 / 11)
        self.assertEqual(prices["details"]["spread"], "0.000000002")
        self.assertEqual(prices["details"]["mid_price"], "1000000.12345679")

    def test_F23_negative_normalized_prices_are_preserved(self):
        engine = FeatureEngine(config(depth=1, weights=["1"]))
        result = compute(engine, 1, 0, simple(2, 1, "-2", "-1"))
        self.assertEqual(result["details"]["mid_price"], "-1.5")
        current = compute(engine, 2, 100, simple(3, 1, "-1", "0"))
        self.assertEqual(current["ofi_top1_delta"], 4)
        self.assertEqual(current["details"]["spread"], "1")

    def test_F24_unknown_or_nonintegral_tick_is_not_fabricated(self):
        for tick, flag in ((None, "TICK_SIZE_UNAVAILABLE"), ("0.3", "TICK_SIZE_MISMATCH")):
            spec = dict(SPEC, tick_size=tick)
            result = compute(FeatureEngine(config()), 1, 0, specification=spec)
            self.assertIsNone(result["details"]["spread_ticks"])
            self.assertIn(flag, result["details"]["flags"])

    def test_F25_units_coverage_and_unstable_configuration_are_rejected(self):
        m = message(1, 0)
        for spec in ({"quantity_unit": "SHARE", "price_unit": "KRW"}, {"quantity_unit": "CONTRACT"}):
            with self.assertRaises(ValidationError): FeatureEngine(config()).process(m, clock(m, 1, 0), spec)
        m["payload"]["book"]["coverage"] = "UNVERIFIED_DEPTH"
        with self.assertRaises(ValidationError): FeatureEngine(config()).process(m, clock(m, 1, 0), SPEC)
        for changes in ({"weights": ["0", "0"]}, {"weights": ["1", "-1"]}, {"depth": 3}, {"window_ms": 0},
                        {"max_window_events": 4097}, {"smoothing": {"method": "EVENT_EMA", "tau_ms": None, "alpha": float("nan")}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError): validate_config(config(**changes))

    def test_F26_feature_contract_rejects_fractional_ofi_and_invalid_rates(self):
        m = message(1, 0); m["payload"]["features"] = compute(FeatureEngine(config()), 1, 0)
        for key, value in (("ofi_top1_delta", .5), ("obi_slope_per_second", 1.0), ("time_basis", "EVENT_TIME")):
            bad = deepcopy(m); bad["payload"]["features"][key] = value
            with self.assertRaises(ValidationError): validate_message(bad)
        bad = deepcopy(m); bad["payload"]["features"]["details"]["kind"] = "TRADE"
        with self.assertRaises(ValidationError): validate_message(bad)

    def test_F27_causal_prefix_and_input_are_unchanged_by_later_observations(self):
        engine = FeatureEngine(config()); m = message(1, 0); before = encode(m)
        result = engine.process(m, clock(m, 1, 0), SPEC); frozen = encode(result)
        compute(engine, 2, 100, simple(100, 1))
        self.assertEqual(encode(result), frozen)
        self.assertEqual(encode(m), before)
        fresh = FeatureEngine(config()).process(m, clock(m, 1, 0), SPEC)
        self.assertEqual(encode(fresh), frozen)

    def test_F28_runs_and_instruments_have_independent_state(self):
        engine = FeatureEngine(config()); compute(engine, 1, 0)
        other = compute(engine, 2, 100, instrument="SIM:OTHER")
        another_run = compute(engine, 3, 200, run="another")
        self.assertIsNone(other["ofi_top1_delta"]); self.assertIsNone(another_run["ofi_top1_delta"])
        result = compute(engine, 4, 300, WORKSHEET["samples"][1])
        self.assertEqual(result["ofi_top1_delta"], 5)
        self.assertNotIn("m2", result["basis"])

    def test_F29_sparse_snapshot_ofi_is_not_claimed_to_be_full_event_flow(self):
        a, b, c = (100, 10, 103, 6), (101, 7, 103, 6), (100, 9, 103, 6)
        self.assertEqual(top1_ofi(a, b) + top1_ofi(b, c), 0)
        self.assertEqual(top1_ofi(a, c), -1)
        result = compute(FeatureEngine(config()), 1, 0)
        self.assertIn("OBSERVED_SNAPSHOT_OFI", result["details"]["flags"])


class FeatureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.store = JournalStore(self.path / "features.db")
        self.feed = MarketFeed(self.store, "run", PROFILE)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def ingest(self, frames=FRAMES):
        messages = []
        for raw in frames:
            messages += self.feed.process(raw, ready_at=decode(raw)["received_at"])
        return messages

    def manifest(self, *, consumer="market", duplicate=False):
        at = timestamp(self.feed.last_ready_us)
        self.store.dispatch("run", consumer, seen_at=at, consumer_epoch="consumer", monotonic_ns=1, limit=1000)
        if duplicate:
            self.store.dispatch("run", consumer, seen_at=at, consumer_epoch="consumer", monotonic_ns=2, limit=1000)
        boundary = self.store.boundary("run", consumer, at=at, decision_id="decision")
        return build_manifest(self.store, boundary)

    def test_F30_pipeline_has_nontrivial_obi_ofi_and_unknown_trade_volume(self):
        messages = self.ingest()
        features = [m["payload"]["features"] for m in messages if m["payload"]["features"] is not None]
        self.assertEqual(len(features), 4)
        self.assertEqual(features[1]["ofi_top1_delta"], 3)
        self.assertAlmostEqual(features[1]["obi"], 13 / 51)
        self.assertEqual(features[2]["details"]["trade_volume"]["UNKNOWN"], "2")
        self.assertIsNone(features[-1]["ofi_top1_delta"])
        self.assertEqual(features[-1]["details"]["book_observations"], 1)

    def test_F31_offline_recomputes_using_raw_clock_and_remapped_provenance(self):
        self.ingest(); manifest = self.manifest()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("time.sleep", side_effect=AssertionError("sleep forbidden")):
            one = verify_feature_replay(manifest, speed=1)
            fast = verify_feature_replay(manifest, speed=1000)
        self.assertEqual(one, fast)
        self.assertEqual((one["book_features"], one["trade_features"]), (3, 1))
        self.assertTrue(one["verified"])

    def test_F32_recomputation_includes_undelivered_producer_dependencies(self):
        self.feed.consumer = "upstream"
        self.ingest(FRAMES[:3])
        self.feed.consumer = "selected"
        self.ingest(FRAMES[3:4])
        result = verify_feature_replay(self.manifest(consumer="selected"))
        self.assertEqual(result["consumer_deliveries"], 1)
        self.assertEqual(result["book_features"], 2)

    def test_F33_lost_ack_does_not_double_count_feature_updates(self):
        self.ingest()
        result = verify_feature_replay(self.manifest(duplicate=True))
        self.assertEqual(result["consumer_deliveries"], 22)
        self.assertEqual(result["feature_messages"], 4)

    @staticmethod
    def rehash(manifest):
        for record in manifest["payload"]["records"].values():
            if "data" in record: record["sha256"] = digest(encode(record["data"]))
        manifest["sha256"] = digest(encode(manifest["payload"]))
        manifest["manifest_id"] = "manifest:" + manifest["sha256"]

    def test_F34_internally_rehashed_false_cache_is_detected(self):
        self.ingest(); original = self.manifest()
        for mutation in ("obi", "clock"):
            manifest = deepcopy(original)
            record = next(r for r in manifest["payload"]["records"].values() if r["kind"] == "MESSAGE" and r["data"]["payload"].get("features") is not None)
            if mutation == "obi": record["data"]["payload"]["features"]["obi"] = -.5
            else: record["data"]["payload"]["features"]["details"]["clock"]["monotonic_ns"] += 1
            self.rehash(manifest); ReplaySession(manifest)
            with self.subTest(mutation=mutation), self.assertRaises(IntegrityFailure): verify_feature_replay(manifest)

    def test_F35_rehashed_future_basis_is_rejected_before_calculation(self):
        self.ingest(); manifest = self.manifest()
        records = [r for r in manifest["payload"]["records"].values() if r["kind"] == "MESSAGE" and r["data"]["payload"].get("features") is not None]
        records.sort(key=lambda r: r["journal_seq"])
        records[0]["data"]["payload"]["features"]["basis"].append(records[1]["data"]["message_id"])
        self.rehash(manifest)
        with self.assertRaisesRegex(IntegrityFailure, "future/unavailable"): verify_feature_replay(manifest)

    def test_F36_failed_publish_cannot_commit_feature_state(self):
        self.ingest(FRAMES[:3])
        before = self.feed.features._states.copy()
        with patch.object(self.store, "publish", side_effect=StorageFailure("injected before commit")):
            with self.assertRaises(StorageFailure): self.ingest(FRAMES[3:4])
        self.assertEqual(self.feed.features._states, before)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 4)

    def test_F37_source_gap_reseeds_smoothing_and_ofi(self):
        self.ingest(FRAMES[:3])
        gap = decode(FRAMES[3]); gap["sequence"] += 1
        self.assertTrue(all(m["payload"]["features"] is None for m in self.ingest([encode(gap)])))
        fresh = deepcopy(gap); fresh["sequence"] += 1; fresh["received_at"] = "2026-11-04T00:00:00.260000Z"; fresh["monotonic_ns"] = 260_000_000
        result = self.ingest([encode(fresh)])[-1]["payload"]["features"]
        self.assertIsNone(result["ofi_top1_delta"])
        self.assertEqual(result["smooth_obi"], result["obi"])

    def test_F38_cli_installed_paths_and_feature_verification(self):
        manifest = self.path / "manifest.json"
        command = [sys.executable, "-m", "quant_system", "market-ingest", "--input", str(ROOT / "tests/fixtures/kiwoom-features.synthetic.jsonl"),
                   "--database", str(self.path / "cli.db"), "--run-id", "cli", "--config", str(ROOT / "config/kiwoom.synthetic.json"),
                   "--feature-config", str(ROOT / "config/features.example.json"), "--simulate-clock", "--manifest", str(manifest)]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(decode(result.stdout)["metrics"]["messages"], 11)
        results = []
        for speed in (1, 1000):
            r = subprocess.run([sys.executable, "-m", "quant_system", "verify-features", str(manifest), "--speed", str(speed)], capture_output=True, text=True, check=True)
            results.append(decode(r.stdout))
        self.assertEqual(results[0], results[1])
        self.assertTrue(results[0]["verified"])

    def test_F39_nonadjacent_raw_retry_does_not_change_book_count(self):
        self.ingest(FRAMES[:4])
        self.assertEqual(self.feed.process(FRAMES[2], ready_at="2026-11-04T00:00:00.250000Z"), [])
        features = self.ingest(FRAMES[4:5])[-1]["payload"]["features"]
        self.assertEqual(features["details"]["book_observations"], 2)

    def test_F40_no_feature_data_cannot_be_reported_verified(self):
        self.ingest(FRAMES[:2])
        with self.assertRaises((ValidationError, IntegrityFailure)): verify_feature_replay(self.manifest())

    def test_F41_book_becoming_stale_during_calculation_cannot_commit_features(self):
        self.ingest(FRAMES[:4])
        frame = decode(FRAMES[3])
        frame.update(sequence=5, received_at="2026-11-04T00:00:00.300000Z", monotonic_ns=300_000_000)
        with patch("quant_system.market.feed.now_utc", side_effect=["2026-11-04T00:00:00.300000Z", "2026-11-04T00:00:01.310000Z"]):
            result = self.feed.process(encode(frame))[-1]
        self.assertIn("STALE", result["quality_flags"])
        self.assertIsNone(result["payload"]["features"])
        self.assertEqual(self.feed.features._states, {})
        self.assertEqual(result["ready_at"], "2026-11-04T00:00:01.310000Z")

    def test_F42_source_freshness_limit_caps_feature_continuity_without_watchdog(self):
        profile = deepcopy(PROFILE); profile["stale_after_ms"] = 50
        feed = MarketFeed(self.store, "short", profile)
        for raw in FRAMES[:4]: result = feed.process(raw, ready_at=decode(raw)["received_at"])[-1]
        f = result["payload"]["features"]
        self.assertIsNone(f["ofi_top1_delta"])
        self.assertEqual(f["smooth_obi"], f["obi"])
        self.assertIn("GAP_RESET", f["details"]["flags"])


if __name__ == "__main__":
    unittest.main()
