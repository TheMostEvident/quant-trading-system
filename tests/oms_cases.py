"""Independent synthetic broker inputs. No matching/fill simulation lives in OMS."""

from copy import deepcopy

from quant_system.execution.journal import ExecutionJournal
from quant_system.execution.ledger import amount, net, reserved, text, valuation
from quant_system.foundation.values import encode

from policy_cases import Case, at, fixture


def config(spot=False):
    case = Case(); spec = deepcopy(case.spec)
    spec.update(initial_per_contract="20000", maintenance_per_contract="15000", instrument_spec_ref=case.policy["payload"]["instrument_spec_ref"])
    value = {"model": "synthetic-fifo-v1", "data_origin": "SYNTHETIC", "ready_at": at(0), "run_id": case.policy["run_id"], "account_id": case.policy["payload"]["account_id"],
             "execution_scope": "synthetic-broker/session-1", "ledger_model": "FUTURES_MARGIN", "valuation_asset": "KRW", "balances": {"KRW": "1000000"},
             "margin_rule_ref": "synthetic-margin-v1", "max_gross_notional": "5000000", "instruments": {case.policy["instrument_id"]: spec}}
    if spot:
        value.update(ledger_model="CASH", valuation_asset="USDT", balances={"USDT": "10000", "BTC": "0"}, margin_rule_ref=None)
        spec.update(asset_class="CRYPTO_SPOT", quantity_unit="BASE_ASSET", price_unit="USDT_PER_BTC", qty_step="0.001", min_qty="0.001", contract_multiplier="1", pnl_asset="USDT", quote_asset="USDT", base_asset="BTC", can_open_short=False, margin_rule_ref=None, initial_per_contract=None, maintenance_per_contract=None)
    return value


class Harness:
    def __init__(self, store, *, cfg=None, register=True):
        self.config = cfg or config()
        if register:
            store.register_run(self.config["run_id"], mode="SYNTHETIC_REPLAY", config=self.config, code_version="oms-test-v1")
        self.journal = ExecutionJournal(store, self.config)
        self.events = []; self.reports = []; self.counter = 0; self.ms = 0; self.fill_sequence = 0
        self.instrument = next(iter(self.config["instruments"]))

    def event(self, kind, data=None, *, ms=None, instrument=None, event_id=None):
        self.counter += 1
        self.ms = self.ms + 10 if ms is None else ms
        return {"event_id": event_id or "event-" + str(self.counter), "kind": kind, "at": at(self.ms),
                "clock": {"producer_id": "oms-test", "epoch": "epoch-1", "monotonic_ns": self.ms * 1_000_000},
                "instrument_id": instrument or self.instrument, "data": data or {}}

    def apply(self, event, **kwargs):
        self.events.append(deepcopy(event))
        return self.journal.apply(event, **kwargs)

    def step(self, kind, data=None, **kwargs):
        return self.apply(self.event(kind, data, **kwargs))

    def target(self, target="2", *, intent="intent", price=None, bid="100.9", ask="101", ms=None, retries=1, ttl=1000, expires=5000, action=None, fee="100", fee_asset=None, submit=True):
        e = self.event("TARGET", ms=ms)
        s = self.config["instruments"][self.instrument]
        actual = net(self.journal.state()["ledger"]["positions"][self.instrument])
        reducing = actual and (amount(target) == 0 or actual * amount(target) > 0 and abs(amount(target)) < abs(actual))
        action = action or ("EXIT" if reducing and amount(target) == 0 else "REDUCE" if reducing else "INCREASE" if actual else "ENTER")
        p = fixture("TradingPolicy")
        p.update(message_id="policy-" + str(self.counter), instrument_id=self.instrument)
        p["payload"].update(strategy_version="static-obi-v1", state_max_age_ms=5000, feature_config_ref="feature-config", target_position_qty=target, valid_from=at(0), expires_at=at(expires), instrument_spec_ref=s["instrument_spec_ref"])
        p["payload"]["execution_limits"].update(max_position_qty="10", max_order_notional={"asset": s["pnl_asset"], "amount": "3000000"}, max_initial_margin={"asset": s["pnl_asset"], "amount": "200000"}, order_ttl_ms=ttl, max_retries=retries)
        q = fixture("MarketSnapshot"); q.update(message_id="quote-" + str(self.counter), instrument_id=self.instrument, received_at=e["at"], ready_at=e["at"])
        q["payload"]["instrument_spec_ref"] = s["instrument_spec_ref"]
        q["payload"]["features"] = None
        q["payload"]["book"].update(bids=[{"price": bid, "quantity": "10"}], asks=[{"price": ask, "quantity": "10"}])
        signal = {"signal_version": "static-obi-v1", "decision_id": "signal-" + str(self.counter), "run_id": self.config["run_id"], "account_id": self.config["account_id"],
                  "instrument_id": self.instrument, "provider_kind": "STATIC", "as_of": e["at"], "policy_ref": p["message_id"], "quote_ref": q["message_id"],
                  "quantity_unit": s["quantity_unit"], "actual_position_qty": text(actual), "target_position_qty": target, "action": action, "entry_allowed": not bool(reducing),
                  "input_refs": [p["message_id"], q["message_id"]]}
        e["data"] = {"intent_id": intent, "intent_expires_at": at(5000), "signal": signal, "policy": p, "quote": q, "quote_clock": dict(e["clock"]),
                     "seen_at": {p["message_id"]: p["ready_at"], q["message_id"]: q["ready_at"]}, "limit_price": price or (ask if amount(target) > actual else bid),
                     "fee_reserve": {"asset": fee_asset or self.config["valuation_asset"], "amount": fee}}
        return self.apply(e) if submit else e

    def claim(self, order="intent:0", kind="NEW", **kwargs):
        command_id = next(key for key, c in self.journal.state()["commands"].items() if c["order_id"] == order and c["kind"] == kind and c["status"] == "QUEUED")
        return self.step("DISPATCH", {"command_id": command_id}, **kwargs)

    def report(self, kind="FILL", *, order="intent:0", qty="2", cum=None, price="101", fee="40", fill_id=None, leaves=None, ms=None, execution_ms=None, execution_sequence=None, broker_id=None, submit=True, charges=None, correction_of=None, reason=None):
        e = self.event("REPORT", ms=ms)
        o = self.journal.state()["orders"][order]
        p = {"account_id": self.config["account_id"], "client_order_id": order, "broker_order_id": broker_id or "broker:" + order,
             "event_type": kind, "order_status": "FILLED" if kind == "FILL" and (cum or qty) == o["quantity"] else "PARTIALLY_FILLED" if kind == "FILL" else "CANCELED" if kind == "CANCEL_ACK" else "REJECTED" if kind == "REJECT" else "OPEN",
             "side": o["side"], "position_effect": o["position_effect"], "order_qty": o["quantity"], "cum_filled_qty": cum if cum is not None else qty if kind == "FILL" else o["filled"],
             "leaves_qty": leaves if leaves is not None else "0" if kind in ("CANCEL_ACK", "REJECT", "EXPIRE") else text(amount(o["quantity"]) - amount(cum if cum is not None else qty if kind == "FILL" else o["filled"])),
             "last_fill": None, "correction_of": correction_of, "reason": reason}
        if kind == "FILL":
            self.fill_sequence += 1
            p["last_fill"] = {"fill_id": fill_id or "fill-" + str(self.fill_sequence), "quantity": qty, "price": price,
                              "charges": charges if charges is not None else [{"kind": "FEE", "asset": self.config["valuation_asset"], "amount": fee}],
                              "clock": {"producer_id": "oms-test", "epoch": "epoch-1", "monotonic_ns": (execution_ms if execution_ms is not None else self.ms) * 1_000_000},
                              "executed_at": at(execution_ms if execution_ms is not None else self.ms), "execution_sequence": execution_sequence if execution_sequence is not None else self.fill_sequence}
        m = fixture("ExecutionReport"); m.update(message_id="report-" + str(self.counter), instrument_id=o["instrument_id"], ready_at=e["at"], received_at=e["at"], payload=p)
        e["data"] = {"report": m}
        if kind == "FILL":
            self.reports.append(deepcopy(m))
        return self.apply(e) if submit else e

    def reconcile(self, *, executions=None, open_orders=None, positions=None, balances=None, margin=None, watermark=None, ms=None, submit=True):
        e = self.event("RECONCILE", ms=ms)
        state = self.journal.state()
        v = valuation(self.config, state)
        orders = []
        for o in state["orders"].values():
            if o["status"] not in ("CANCELED", "REJECTED", "EXPIRED", "FILLED", "PENDING_SEND"):
                orders.append({"client_order_id": o["order_id"], "broker_order_id": o["broker_order_id"] or "broker:" + o["order_id"], "side": o["side"], "position_effect": o["position_effect"],
                               "order_qty": o["quantity"], "cum_filled_qty": o["filled"], "leaves_qty": text(reserved(o))})
        e["data"] = {"complete": True, "transport_quiesced": True, "execution_scope": self.config["execution_scope"], "watermark": watermark if watermark is not None else state["watermark"] + 1,
                     "as_of": e["at"], "executions": deepcopy(self.reports if executions is None else executions), "open_orders": orders if open_orders is None else open_orders,
                     "positions": positions if positions is not None else [{"instrument_id": k, "net_qty": text(net(p))} for k, p in state["ledger"]["positions"].items()],
                     "balances": balances if balances is not None else dict(state["ledger"]["cash"]),
                     "margin": margin if margin is not None else {key: v[key] for key in ("asset", "initial_used", "maintenance_required", "order_reserved", "available_margin")}}
        return self.apply(e) if submit else e
