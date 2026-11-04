"""Synthetic producers for policy tests; position changes are explicit inputs."""

from copy import deepcopy
from pathlib import Path

from quant_system.foundation.values import decode, timestamp, utc_us
from quant_system.market.features import DEFAULT_CONFIG, FeatureClock, FeatureEngine

BASE = utc_us("2026-11-04T00:00:00.000000Z")
FIXTURES = decode((Path(__file__).parent / "fixtures/contracts-v0.2.json").read_bytes())


def at(ms):
    return timestamp(BASE + ms * 1000)


def fixture(kind):
    m = deepcopy(next(x for x in FIXTURES if x["message_type"] == kind))
    m.update(received_at=at(0), ready_at=at(0), event_at=None, source_event_id=None, lineage=[], quality_flags=[])
    return m


class Case:
    def __init__(self):
        self.config = deepcopy(DEFAULT_CONFIG)
        self.config.update(depth=1, weights=["1"], smoothing={"method": "EVENT_EMA", "tau_ms": None, "alpha": 1})
        self.spec = {"asset_class": "FUTURE", "quantity_unit": "CONTRACT", "price_unit": "INDEX_POINT", "qty_step": "1", "min_qty": "1",
                     "contract_multiplier": "1000", "pnl_asset": "KRW", "quote_asset": "KRW", "base_asset": None, "tick_size": "0.1",
                     "position_mode": "NET", "pnl_model": "LINEAR", "can_open_long": True, "can_open_short": True, "margin_rule_ref": "synthetic-margin-v1"}
        self.policy = fixture("TradingPolicy")
        self.policy["payload"].update(strategy_version="static-obi-v1", feature_config_ref="feature-config", state_max_age_ms=5000,
                                      valid_from=at(0), expires_at=at(60000), smoothing=deepcopy(self.config["smoothing"]))
        self.account = fixture("AccountState")
        self.account["message_id"] = "account-1"
        self.account["payload"].update(account_version=1)
        self.account["payload"].update(balances=[{"asset": "KRW", "wallet": "1000000", "free": "1000000", "locked": "0"}], unsettled_pnl=[], adjustments=[])
        self.account["payload"]["valuation"].update(equity="1000000", as_of=at(0))
        self.account["payload"]["margin"].update(initial_used="0", maintenance_required="0", order_reserved="0", available_margin="50000")
        self.pos = fixture("PositionState")
        self.pos["payload"].update(net_qty="0", position_status="FLAT", position_version=1, account_state_ref=self.account["message_id"], avg_entry_price=None,
                                   pnl_reference_price=None, mark_price=None, unrealized_pnl_gross="0", charges_paid=[])
        self.opened_ms = None
        self.producer = FeatureEngine(self.config, config_ref="feature-config")
        self.counter = 0

    def position(self, qty, version=2, ms=0, opened_ms=None):
        if qty == "0":
            self.opened_ms = None
        elif self.opened_ms is None:
            self.opened_ms = opened_ms or 0
        self.account["message_id"] = "account-" + str(version)
        self.account.update(ready_at=at(ms), received_at=at(ms))
        initial = abs(int(qty)) * 20000
        self.account["payload"].update(account_version=version)
        self.account["payload"]["valuation"]["as_of"] = at(ms)
        self.account["payload"]["margin"].update(initial_used=str(initial), maintenance_required=str(abs(int(qty)) * 15000), available_margin=str(50000 - initial))
        self.account["payload"]["balances"][0].update(free=str(1000000 - initial), locked=str(initial))
        self.pos["message_id"] = "position-" + str(version)
        self.pos.update(ready_at=at(ms), received_at=at(ms))
        self.pos["payload"].update(net_qty=qty, position_status="FLAT" if qty == "0" else "OPEN", position_version=version, account_state_ref=self.account["message_id"],
                                   avg_entry_price="100" if qty != "0" else None, pnl_reference_price="100" if qty != "0" else None, mark_price="100" if qty != "0" else None)

    def policy_target(self, qty, revision=2, ms=0):
        self.policy["message_id"] = "policy-" + str(revision)
        self.policy.update(ready_at=at(ms), received_at=at(ms))
        self.policy["payload"].update(target_position_qty=qty, revision=revision)

    def bundle(self, ms, *, bid_qty="9", ask_qty="1", kind="BOOK", flags=(), status="CONTINUOUS", epoch="epoch-1", bid="100", ask="100.1"):
        self.counter += 1
        index = self.counter
        m = fixture("MarketSnapshot")
        m.update(message_id="market-" + str(index), ready_at=at(ms), received_at=at(ms), lineage=["raw-" + str(index)], quality_flags=list(flags))
        m["payload"].update(update_kind=kind, market_status=status, features=None)
        m["payload"]["book"] = {"coverage": "TOP_5_CALLBACK_SNAPSHOT", "sync_status": "SYNCED", "source_sequence": None,
                                  "bids": [{"price": bid, "quantity": bid_qty}], "asks": [{"price": ask, "quantity": ask_qty}]} if kind == "BOOK" else None
        m["payload"]["trade"] = {"trade_id": None, "price": "100", "quantity": "1", "aggressor": "UNKNOWN"} if kind == "TRADE" else None
        m["payload"]["features"] = self.producer.process(m, FeatureClock("producer", epoch, index, ms * 1_000_000, m["lineage"][0]), self.spec)
        p, pos = self.policy["payload"], self.pos["payload"]
        e = {"evidence_version": "static-context-v1", "evidence_id": "context-" + str(index), "run_id": m["run_id"], "account_id": p["account_id"], "instrument_id": m["instrument_id"],
             "data_origin": "SYNTHETIC", "ready_at": at(ms), "valid_from": at(0), "expires_at": at(10000),
             "instrument_spec_ref": p["instrument_spec_ref"], "universe_ref": p["universe_ref"], "risk_budget_ref": p["risk_budget_ref"], "feature_config_ref": p["feature_config_ref"],
             "specification": deepcopy(self.spec), "feature_config": deepcopy(self.config),
             "account_capability": {"verified": True, "can_open_long": True, "can_open_short": True},
             "risk": {"max_abs_position_qty": "2", "max_gross_notional": {"asset": "KRW", "amount": "300000"}, "other_gross_notional": {"asset": "KRW", "amount": "0"},
                      "margin_quote": {"account_state_ref": self.account["message_id"], "position_state_ref": self.pos["message_id"], "quote_ref": m["message_id"],
                                       "target_position_qty": p["target_position_qty"], "margin_rule_ref": "synthetic-margin-v1", "available_basis": "NET_OF_ORDER_RESERVED",
                                       "total_initial_required": {"asset": "KRW", "amount": "40000"}, "incremental_required": {"asset": "KRW", "amount": str(max(40000 - int(self.account['payload']['margin']['initial_used']), 0))}}},
             "position_lifecycle": {"position_state_ref": self.pos["message_id"], "first_fill_ref": "external-fixture-fill", "first_fill_clock": {"producer_id": "producer", "epoch": "epoch-1", "monotonic_ns": (self.opened_ms or 0) * 1_000_000}} if pos["net_qty"] != "0" else None}
        bundle = {"decision_id": "decision-" + str(index), "run_id": m["run_id"], "account_id": p["account_id"], "instrument_id": m["instrument_id"], "as_of": at(ms),
                  "clock": {"producer_id": "producer", "epoch": epoch, "monotonic_ns": ms * 1_000_000},
                  "policy": deepcopy(self.policy), "account": deepcopy(self.account), "position": deepcopy(self.pos), "market": m, "evidence": e}
        bundle["seen_at"] = {x["message_id"]: x["ready_at"] for x in (bundle["policy"], bundle["account"], bundle["position"], m)}
        bundle["seen_at"][e["evidence_id"]] = e["ready_at"]
        return bundle


def golden_scenario():
    c = Case()
    bundles = [c.bundle(0), c.bundle(100), c.bundle(200)]
    # Re-evaluation after ENTER sees exactly the same externally supplied flat
    # state. It cannot assume the signal was filled.
    bundles.append(c.bundle(300))
    c.position("2", ms=350, opened_ms=350)
    bundles.extend([c.bundle(400, bid_qty="6", ask_qty="4"), c.bundle(500, bid_qty="119", ask_qty="81")])
    c.policy_target("-2", ms=550)
    bundles.append(c.bundle(600, bid_qty="1", ask_qty="9"))
    c.position("0", version=3, ms=650)
    bundles.extend([c.bundle(700, bid_qty="1", ask_qty="9"), c.bundle(800, bid_qty="1", ask_qty="9"), c.bundle(900, bid_qty="1", ask_qty="9")])
    return {"scenario_version": "static-scenario-v1", "bundles": bundles}
