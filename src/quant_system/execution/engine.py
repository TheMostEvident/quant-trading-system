"""Pure OMS transitions. Broker effects are durable commands, not network calls."""

from copy import deepcopy
from fractions import Fraction

from quant_system.contracts import validate_message
from quant_system.contracts.static_policy import validate_static_policy
from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decode, digest, encode, utc_us
from quant_system.strategy.engine import clock, same_domain

from .ledger import MODEL, amount, empty_ledger, net, rebuild, reserved, text, valuation

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


def initial(config):
    return {"model": MODEL, "revision": 0, "orders": {}, "commands": {}, "intents": {}, "fills": {}, "economic": [],
            "ledger": empty_ledger(config), "faults": [], "connected": True, "halted": False,
            "reconciliation": "INTERNAL_ONLY", "last_clock": None, "last_at": None, "watermark": -1, "snapshot_hash": None, "input_refs": []}


def fault(state, reason):
    if reason not in state["faults"]:
        state["faults"].append(reason)
    state["reconciliation"] = "UNRECONCILED"


def conflict_transition(old, event, event_ref):
    state = deepcopy(old); state["revision"] += 1
    fault(state, "EVENT_ID_CONFLICT"); state["input_refs"].append(event_ref)
    if state["last_at"] is None or utc_us(event["at"]) >= utc_us(state["last_at"]):
        state.update(last_at=event["at"], last_clock=event["clock"])
    return state, {"status": "REVIEW", "reason": "EVENT_ID_CONFLICT"}


def active_orders(state, instrument=None):
    return [o for o in state["orders"].values() if not o["finalized"] and (instrument is None or o["instrument_id"] == instrument)]


def command(state, order, kind, event):
    seq = sum(c["order_id"] == order["order_id"] and c["kind"] == kind for c in state["commands"].values())
    key = order["order_id"] + ":" + kind.lower() + ":" + str(seq)
    state["commands"][key] = {"command_id": key, "order_id": order["order_id"], "kind": kind, "status": "QUEUED",
                               "created_at": event["at"], "created_clock": event["clock"], "payload": deepcopy(order)}
    return key


def cancel(state, order, event):
    if order["finalized"] or order["status"] in TERMINAL:
        return "NOT_LIVE"
    if order["status"] == "PENDING_SEND":
        order.update(status="CANCELED", finalized=True)
        for c in state["commands"].values():
            if c["order_id"] == order["order_id"] and c["status"] == "QUEUED":
                c["status"] = "OBSOLETE"
        return "UNSENT_ORDER_CANCELED"
    if not order["broker_order_id"]:
        fault(state, "SUBMISSION_OUTCOME_UNKNOWN")
        order["status"] = "UNKNOWN"
        return "QUERY_REQUIRED"
    if any(c["order_id"] == order["order_id"] and c["kind"] == "CANCEL" and c["status"] in ("QUEUED", "SENDING") for c in state["commands"].values()):
        return "CANCEL_ALREADY_PENDING"
    order["status"] = "CANCEL_PENDING"
    return command(state, order, "CANCEL", event)


def finish_commands(state, order_id, kinds=("NEW", "CANCEL")):
    for c in state["commands"].values():
        if c["order_id"] == order_id and c["kind"] in kinds and c["status"] in ("QUEUED", "SENDING"):
            c["status"] = "DONE"


def _target(config, state, event):
    d = event["data"]
    instrument = event["instrument_id"]
    spec = config["instruments"][instrument]
    signal, policy, quote = d["signal"], d["policy"], d["quote"]
    validate_message(policy); validate_static_policy(policy["payload"]); validate_message(quote)
    p, q = policy["payload"], quote["payload"]
    if state["faults"] or not state["connected"] or state["halted"]:
        return {"status": "BLOCKED", "reason": "RECONCILE_OR_RESUME_REQUIRED"}
    if signal["run_id"] != config["run_id"] or signal["account_id"] != config["account_id"] or signal["instrument_id"] != instrument or signal["provider_kind"] != "STATIC" or signal["signal_version"] != "static-obi-v1":
        raise ValidationError("Signal scope/version mismatch")
    if policy["message_type"] != "TradingPolicy" or quote["message_type"] != "MarketSnapshot" or any(m["run_id"] != config["run_id"] or m["instrument_id"] != instrument or m["data_origin"] != "SYNTHETIC" for m in (policy, quote)) or p["account_id"] != config["account_id"]:
        raise ValidationError("Policy/quote scope mismatch")
    if signal["policy_ref"] != policy["message_id"] or signal["quote_ref"] not in (None, quote["message_id"]) or p["instrument_spec_ref"] != spec["instrument_spec_ref"] or q["instrument_spec_ref"] != spec["instrument_spec_ref"]:
        raise ValidationError("Signal/policy/quote linkage mismatch")
    for m in (policy, quote):
        if not utc_us(m["ready_at"]) <= utc_us(d["seen_at"][m["message_id"]]) <= utc_us(event["at"]):
            raise ValidationError("Unavailable decision input")
    if utc_us(signal["as_of"]) > utc_us(event["at"]):
        raise ValidationError("Future signal")
    actual = net(state["ledger"]["positions"][instrument])
    if amount(signal["actual_position_qty"]) != actual:
        return {"status": "BLOCKED", "reason": "SIGNAL_POSITION_CHANGED"}
    target = amount(signal["target_position_qty"])
    if signal["action"] not in ("ENTER", "INCREASE", "EXIT", "REDUCE"):
        return {"status": "BLOCKED", "reason": "NO_ACTIONABLE_SIGNAL"}
    if target == actual:
        return {"status": "NOOP", "reason": "ALREADY_AT_TOTAL_TARGET"}
    if actual * target < 0:
        return {"status": "BLOCKED", "reason": "CLOSE_BEFORE_REVERSAL"}
    reducing = actual and (target == 0 or actual * target > 0 and abs(target) < abs(actual))
    if bool(reducing) != (signal["action"] in ("EXIT", "REDUCE")) or (signal["action"] == "EXIT" and target != 0):
        raise ValidationError("Signal action differs from total target")
    if not reducing and (not signal["entry_allowed"] or not utc_us(p["valid_from"]) <= utc_us(event["at"]) < utc_us(p["expires_at"]) or target != amount(p["target_position_qty"])):
        return {"status": "BLOCKED", "reason": "POLICY_EXPIRED_OR_ENTRY_DISALLOWED"}
    if not reducing and (signal["entry_allowed"] is not True or signal["quote_ref"] != quote["message_id"]):
        raise ValidationError("Opening signal requires an explicit entry gate and its original quote")
    if active_orders(state, instrument):
        return {"status": "BLOCKED", "reason": "LIVE_OR_UNCONFIRMED_ORDER"}
    if quote["quality_flags"] or q["market_status"] != "CONTINUOUS" or q["update_kind"] != "BOOK" or not q["book"] or q["book"]["sync_status"] != "SYNCED":
        return {"status": "BLOCKED", "reason": "CURRENT_TRADABLE_BOOK_REQUIRED"}
    now = clock(event["clock"])
    qc = clock(d["quote_clock"])
    lim = p["execution_limits"]
    if not same_domain(now, qc) or not 0 <= now[2] - qc[2] < lim["max_quote_age_ms"] * 1_000_000 or not 0 <= utc_us(event["at"]) - utc_us(quote["received_at"]) < lim["max_quote_age_ms"] * 1000:
        return {"status": "BLOCKED", "reason": "QUOTE_EXPIRED_OR_CLOCK_UNMAPPED"}
    if signal["quantity_unit"] != spec["quantity_unit"]:
        raise ValidationError("Quantity unit mismatch")
    qty = abs(target - actual)
    step = amount(spec["qty_step"])
    if qty < amount(spec["min_qty"]) or qty % step or (spec["quantity_unit"] in ("CONTRACT", "SHARE") and qty.denominator != 1):
        raise ValidationError("Order quantity violates unit/step")
    if config["ledger_model"] == "CASH" and target < 0:
        return {"status": "BLOCKED", "reason": "SPOT_SHORT_UNSUPPORTED"}
    direction = "LONG" if target > 0 else "SHORT"
    if not reducing and (direction not in p["allowed_position_directions"] or not spec["can_open_long" if target > 0 else "can_open_short"]):
        return {"status": "BLOCKED", "reason": "CAPABILITY_BLOCKED"}
    side = "BUY" if target > actual else "SELL"
    bid = amount(q["book"]["bids"][0]["price"]); ask = amount(q["book"]["asks"][0]["price"])
    tick = amount(spec["tick_size"])
    price = amount(d["limit_price"])
    if price % tick or (config["ledger_model"] == "CASH" and price <= 0) or (ask - bid) / tick > lim["max_spread_ticks"]:
        return {"status": "BLOCKED", "reason": "PRICE_OR_SPREAD_RULE"}
    intent_id = d["intent_id"]
    if not isinstance(intent_id, str) or not intent_id:
        raise ValidationError("Intent identity required")
    intent = state["intents"].get(intent_id)
    if intent:
        if intent["instrument_id"] != instrument or amount(intent["target"]) != target or intent["side"] != side:
            raise ValidationError("Intent cannot change scope/target/side")
        start = clock(intent["created_clock"])
        if not same_domain(start, now) or now[2] - start[2] >= intent["ttl_ms"] * 1_000_000 or utc_us(event["at"]) >= utc_us(intent["expires_at"]):
            return {"status": "BLOCKED", "reason": "INTENT_EXPIRED"}
        attempt = intent["attempt"] + 1
        if attempt > intent["max_retries"]:
            return {"status": "BLOCKED", "reason": "RETRY_LIMIT"}
    else:
        if any(i["signal_ref"] == signal["decision_id"] for i in state["intents"].values()):
            return {"status": "BLOCKED", "reason": "SIGNAL_ALREADY_HAS_INTENT"}
        attempt = 0
        intent = {"instrument_id": instrument, "target": text(target), "side": side, "signal_ref": signal["decision_id"],
                  "anchor": text(ask if side == "BUY" else bid), "anchor_ref": quote["message_id"], "created_clock": event["clock"],
                  "expires_at": d["intent_expires_at"], "policy_expires_at": p["expires_at"], "opening": not bool(reducing),
                  "ttl_ms": lim["order_ttl_ms"], "max_retries": lim["max_retries"], "max_slippage_ticks": lim["max_slippage_ticks"]}
        if utc_us(intent["expires_at"]) <= utc_us(event["at"]) or intent["ttl_ms"] <= 0:
            raise ValidationError("Empty intent validity")
    anchor = amount(intent["anchor"])
    if (price > anchor + intent["max_slippage_ticks"] * tick if side == "BUY" else price < anchor - intent["max_slippage_ticks"] * tick):
        return {"status": "BLOCKED", "reason": "ORIGINAL_PRICE_ANCHOR_LIMIT"}
    order_notional = qty * abs(price) * amount(spec["contract_multiplier"])
    maximum = lim["max_order_notional"]
    if maximum["asset"] != spec["pnl_asset"] or order_notional > amount(maximum["amount"]) or abs(target) > amount(lim["max_position_qty"]):
        return {"status": "BLOCKED", "reason": "QUANTITY_OR_NOTIONAL_LIMIT"}
    fee = d["fee_reserve"]
    if fee["asset"] not in state["ledger"]["cash"] or amount(fee["amount"]) < 0:
        return {"status": "BLOCKED", "reason": "FEE_RESERVE_UNAVAILABLE"}
    order_id = intent_id + ":" + str(attempt)
    order = {"order_id": order_id, "intent_id": intent_id, "instrument_id": instrument, "broker_order_id": None, "side": side,
             "position_effect": "CLOSE" if reducing else "OPEN", "quantity": text(qty), "filled": "0", "cum_high": "0", "reported_leaves": text(qty),
             "limit_price": text(price), "fee_reserve": fee, "status": "PENDING_SEND", "finalized": False, "created_at": event["at"], "created_clock": event["clock"]}
    candidate = deepcopy(state); candidate["orders"][order_id] = order
    v = valuation(config, candidate)
    if not reducing and amount(v["gross_notional"]) > amount(config["max_gross_notional"]):
        return {"status": "BLOCKED", "reason": "ACCOUNT_GROSS_NOTIONAL_LIMIT"}
    if not reducing and v["unvalued_assets"]:
        return {"status": "BLOCKED", "reason": "VALUATION_UNRESOLVED"}
    if config["ledger_model"] == "FUTURES_MARGIN":
        margin_limit = lim["max_initial_margin"]
        if not reducing and (margin_limit is None or margin_limit["asset"] != v["asset"] or amount(v["initial_used"]) + amount(v["order_reserved"]) > amount(margin_limit["amount"]) or amount(v["available_margin"]) < 0):
            return {"status": "BLOCKED", "reason": "MARGIN_LIMIT"}
    for asset, total in v["cash_reserved"].items():
        if total and amount(total) > amount(candidate["ledger"]["cash"].get(asset, "0")):
            return {"status": "BLOCKED", "reason": "CASH_OR_INVENTORY_RESERVE_LIMIT"}
    intent["attempt"] = attempt
    state["intents"][intent_id] = intent
    state["orders"][order_id] = order
    cmd = command(state, order, "NEW", event)
    return {"status": "QUEUED", "order_id": order_id, "command_id": cmd, "quantity": text(qty), "total_target": text(target)}


def _report(config, state, report, event):
    validate_message(report)
    if report["message_type"] != "ExecutionReport" or report["run_id"] != config["run_id"] or report["data_origin"] != "SYNTHETIC" or report["payload"]["account_id"] != config["account_id"] or utc_us(report["ready_at"]) > utc_us(event["at"]):
        raise ValidationError("Report scope/origin/availability mismatch")
    p = report["payload"]
    order = state["orders"].get(p["client_order_id"])
    if not order:
        fault(state, "UNKNOWN_ORDER_REPORT")
        return {"status": "REVIEW", "reason": "UNKNOWN_ORDER_REPORT"}
    if report["instrument_id"] != order["instrument_id"] or p["side"] != order["side"] or p["position_effect"] != order["position_effect"] or amount(p["order_qty"]) != amount(order["quantity"]) or not p["broker_order_id"]:
        raise ValidationError("Execution/order binding mismatch")
    broker_id = p["broker_order_id"]
    if order["broker_order_id"] not in (None, broker_id) or any(o["order_id"] != order["order_id"] and o["broker_order_id"] == broker_id for o in state["orders"].values()):
        raise ValidationError("Broker order identity conflict")
    order["broker_order_id"] = broker_id
    kind = p["event_type"]
    if not any(c["order_id"] == order["order_id"] and c["kind"] == "NEW" and c.get("dispatched_at") for c in state["commands"].values()):
        raise ValidationError("Broker report for an order never claimed for transmission")
    if kind == "CORRECTION":
        key = p["correction_of"]
        if p["reason"] != "BUST" or key not in state["fills"] or state["fills"][key]["order_id"] != order["order_id"]:
            raise ValidationError("Only referenced fill bust is supported; other corrections require reconciliation")
        if not state["fills"][key]["busted"]:
            before_ledger = deepcopy(state["ledger"])
            state["fills"][key]["busted"] = True
            state["ledger"] = rebuild(config, state["economic"], state["fills"])
            order["filled"] = text(sum((amount(f["fill"]["quantity"]) for f in state["fills"].values() if f["order_id"] == order["order_id"] and not f["busted"]), Fraction(0)))
            order.update(status="UNKNOWN", finalized=False)
            reversing = [{"asset": asset, "amount": text(amount(state["ledger"]["cash"].get(asset, "0")) - amount(before_ledger["cash"].get(asset, "0")))}
                         for asset in sorted(set(state["ledger"]["cash"]) | set(before_ledger["cash"]))]
            state.setdefault("corrections", []).append({"reference": event.get("event_ref", event["event_id"]), "correction_of": key, "reversing_postings": reversing})
        fault(state, "EXECUTION_CORRECTION_REQUIRES_RECONCILIATION")
        return {"status": "BUST_RECORDED", "correction_of": key, "reversing_postings": state.get("corrections", [])[-1]["reversing_postings"]}
    if kind not in ("ACK", "FILL", "CANCEL_ACK", "CANCEL_REJECT", "REJECT", "EXPIRE"):
        raise ValidationError("Unsupported report transition")
    cum = amount(p["cum_filled_qty"])
    order["cum_high"] = text(max(cum, amount(order["cum_high"])))
    order["reported_leaves"] = p["leaves_qty"]
    duplicate_fill = False
    if kind == "FILL":
        f = p["last_fill"]
        spec = config["instruments"][order["instrument_id"]]
        if amount(f["quantity"]) % amount(spec["qty_step"]) or (spec["quantity_unit"] in ("CONTRACT", "SHARE") and amount(f["quantity"]).denominator != 1) or (config["ledger_model"] == "CASH" and amount(f["price"]) <= 0):
            raise ValidationError("Fill quantity/price violates units")
        key = digest(encode([config["execution_scope"], config["account_id"], broker_id, f["fill_id"]]))
        semantic = digest(encode({"order_id": order["order_id"], "instrument": order["instrument_id"], "side": p["side"], "fill": f}))
        existing = state["fills"].get(key)
        if existing:
            if existing["semantic"] != semantic or existing["busted"]:
                raise ValidationError("Fill identity conflict or resurrection after bust")
            duplicate_fill = True
        else:
            dispatched = min(utc_us(c["dispatched_at"]) for c in state["commands"].values() if c["order_id"] == order["order_id"] and c["kind"] == "NEW" and c.get("dispatched_at"))
            if type(f.get("execution_sequence")) is not int or f["execution_sequence"] < 1 or not dispatched <= utc_us(f["executed_at"]) <= utc_us(report["ready_at"]):
                raise ValidationError("Verified synthetic execution order/time required")
            if any(x["fill"]["execution_sequence"] == f["execution_sequence"] for x in state["fills"].values()):
                raise ValidationError("Execution sequence reused")
            if amount(order["filled"]) + amount(f["quantity"]) > amount(order["quantity"]):
                raise ValidationError("Distinct fills exceed original order quantity")
            before = net(state["ledger"]["positions"][order["instrument_id"]])
            signed = amount(f["quantity"]) * (1 if p["side"] == "BUY" else -1)
            fill_clock = clock(f["clock"])
            current_clock = clock(event["clock"])
            if not same_domain(fill_clock, current_clock) or fill_clock[2] > current_clock[2]:
                raise ValidationError("Synthetic fill requires a verified, nonfuture execution clock")
            record = {"order_id": order["order_id"], "instrument_id": order["instrument_id"], "side": p["side"], "fill": f,
                      "reference": event.get("event_ref", event["event_id"]), "clock": f["clock"], "semantic": semantic, "busted": False}
            state["fills"][key] = record
            state["economic"].append({"kind": "FILL", "instrument_id": order["instrument_id"], "fill_key": key})
            state["ledger"] = rebuild(config, state["economic"], state["fills"])
            order["filled"] = text(amount(order["filled"]) + amount(f["quantity"]))
            if order["position_effect"] == "CLOSE" and (before * signed >= 0 or abs(signed) > abs(before)):
                fault(state, "UNEXPECTED_CLOSE_EXPOSURE")
            if (amount(f["price"]) > amount(order["limit_price"]) if p["side"] == "BUY" else amount(f["price"]) < amount(order["limit_price"])):
                fault(state, "FILL_OUTSIDE_LIMIT_PRICE")
            if order["finalized"]:
                fault(state, "FILL_AFTER_RECONCILIATION_BARRIER")
    if amount(order["filled"]) == amount(order["quantity"]):
        order.update(status="FILLED", finalized=True)
        finish_commands(state, order["order_id"])
    elif kind in ("CANCEL_ACK", "REJECT", "EXPIRE"):
        order.update(status={"CANCEL_ACK": "CANCELED", "REJECT": "REJECTED", "EXPIRE": "EXPIRED"}[kind], finalized=False)
        finish_commands(state, order["order_id"])
    elif kind == "CANCEL_REJECT":
        finish_commands(state, order["order_id"], ("CANCEL",))
        if order["status"] not in TERMINAL:
            order["status"] = "PARTIALLY_FILLED" if amount(order["filled"]) else "WORKING"
    elif order["status"] not in TERMINAL and order["status"] != "CANCEL_PENDING":
        order["status"] = "PARTIALLY_FILLED" if amount(order["filled"]) else "WORKING"
        finish_commands(state, order["order_id"], ("NEW",))
    if amount(order["cum_high"]) > amount(order["filled"]):
        fault(state, "MISSING_EXECUTION_DETAILS")
    if any(amount(v) < 0 for v in state["ledger"]["cash"].values()):
        fault(state, "NEGATIVE_ASSET_BALANCE")
    if report["quality_flags"]:
        fault(state, "EXECUTION_QUALITY_REVIEW")
    return {"status": "DUPLICATE_FILL" if duplicate_fill else "REPORT_APPLIED", "order_id": order["order_id"], "fill_key": key if kind == "FILL" else None}


def _reconcile(config, state, event):
    d = event["data"]
    if d.get("complete") is not True or d.get("transport_quiesced") is not True or d.get("execution_scope") != config["execution_scope"] or type(d.get("watermark")) is not int or d["watermark"] < state["watermark"] or not utc_us(state["last_at"] or config["ready_at"]) <= utc_us(d["as_of"]) <= utc_us(event["at"]):
        raise ValidationError("A complete, current, quiesced execution/position/balance snapshot is required")
    snapshot_hash = digest(encode(d))
    if d["watermark"] == state["watermark"] and snapshot_hash != state["snapshot_hash"]:
        raise ValidationError("Reconciliation watermark reused with different evidence")
    candidate = deepcopy(state)
    keys = set()
    for report in d["executions"]:
        if report["payload"]["event_type"] != "FILL" or utc_us(report["ready_at"]) > utc_us(d["as_of"]):
            raise ValidationError("Snapshot executions require available fill details")
        result = _report(config, candidate, report, event)
        if result["status"] == "REVIEW" or result["fill_key"] in keys:
            raise ValidationError("Unknown/duplicate snapshot execution")
        keys.add(result["fill_key"])
    expected = {key for key, f in candidate["fills"].items() if not f["busted"]}
    if keys != expected:
        raise ValidationError("Reconciliation omits known fills")
    for order_id, o in candidate["orders"].items():
        known_gross = sum((amount(f["fill"]["quantity"]) for f in candidate["fills"].values() if f["order_id"] == order_id), Fraction(0))
        if amount(o["cum_high"]) > known_gross:
            raise ValidationError("Snapshot still omits cumulative execution evidence")
    # Missing fills are authoritative new economic input even if later position
    # comparison fails. Keep them and retain conservative reservations.
    state.update(fills=deepcopy(candidate["fills"]), economic=deepcopy(candidate["economic"]), ledger=deepcopy(candidate["ledger"]), orders=deepcopy(candidate["orders"]), commands=deepcopy(candidate["commands"]))
    open_orders = {}; broker_ids = set()
    for item in d["open_orders"]:
        key = item["client_order_id"]
        if key in open_orders or key not in candidate["orders"]:
            raise ValidationError("Unknown/duplicate open order")
        o = candidate["orders"][key]
        if not item["broker_order_id"] or item["broker_order_id"] in broker_ids or o["status"] == "PENDING_SEND":
            raise ValidationError("Broker identity conflicts or reports an unsent order")
        if any(other["order_id"] != key and other["broker_order_id"] == item["broker_order_id"] for other in candidate["orders"].values()):
            raise ValidationError("Reconciliation cannot reuse another broker order identity")
        broker_ids.add(item["broker_order_id"])
        if o["broker_order_id"] not in (None, item["broker_order_id"]) or o["side"] != item["side"] or o["position_effect"] != item["position_effect"] or amount(o["quantity"]) != amount(item["order_qty"]) or amount(o["filled"]) != amount(item["cum_filled_qty"]) or amount(item["leaves_qty"]) != amount(o["quantity"]) - amount(o["filled"]) or not amount(item["leaves_qty"]):
            raise ValidationError("Open-order reconciliation mismatch")
        open_orders[key] = item
    for key, o in candidate["orders"].items():
        if key in open_orders:
            o.update(broker_order_id=open_orders[key]["broker_order_id"], status="PARTIALLY_FILLED" if amount(o["filled"]) else "WORKING", finalized=False, cum_high=o["filled"], reported_leaves=open_orders[key]["leaves_qty"])
            finish_commands(candidate, key)
        elif o["status"] != "PENDING_SEND":
            o.update(status="FILLED" if amount(o["filled"]) == amount(o["quantity"]) else "CANCELED", finalized=True, cum_high=o["filled"], reported_leaves="0")
            finish_commands(candidate, key)
    positions = {x["instrument_id"]: x for x in d["positions"]}
    if len(positions) != len(d["positions"]) or set(positions) != set(config["instruments"]):
        raise ValidationError("Full position scope required")
    for instrument, row in positions.items():
        if amount(row["net_qty"]) != net(candidate["ledger"]["positions"][instrument]):
            fault(state, "POSITION_RECONCILIATION_MISMATCH")
            return {"status": "MISMATCH", "reason": "POSITION_RECONCILIATION_MISMATCH"}
    if set(d["balances"]) != set(candidate["ledger"]["cash"]) or any(amount(v) != amount(candidate["ledger"]["cash"][k]) for k, v in d["balances"].items()):
        fault(state, "BALANCE_RECONCILIATION_MISMATCH")
        return {"status": "MISMATCH", "reason": "BALANCE_RECONCILIATION_MISMATCH"}
    v = valuation(config, candidate)
    if config["ledger_model"] == "FUTURES_MARGIN" and (d["margin"]["asset"] != v["asset"] or any(amount(d["margin"][k]) != amount(v[k]) for k in ("initial_used", "maintenance_required", "order_reserved", "available_margin"))):
        fault(state, "MARGIN_RECONCILIATION_MISMATCH")
        return {"status": "MISMATCH", "reason": "MARGIN_RECONCILIATION_MISMATCH"}
    candidate.update(faults=[], reconciliation="MATCHED", connected=True, watermark=d["watermark"], snapshot_hash=snapshot_hash)
    state.clear(); state.update(candidate)
    return {"status": "MATCHED", "watermark": d["watermark"]}


def transition(config, old, event):
    state = deepcopy(old)
    now = clock(event["clock"])
    at = utc_us(event["at"])
    if old["last_at"] and at < utc_us(old["last_at"]):
        raise ValidationError("Event availability time regressed")
    if old["last_clock"]:
        previous = clock(old["last_clock"])
        if same_domain(now, previous) and now[2] < previous[2]:
            raise ValidationError("OMS clock regressed")
        if not same_domain(now, previous):
            fault(state, "CLOCK_EPOCH_RECONCILIATION_REQUIRED")
    state["revision"] += 1
    kind = event["kind"]
    try:
        if kind == "TARGET":
            result = _target(config, state, event)
        elif kind == "DISPATCH":
            c = state["commands"][event["data"]["command_id"]]
            o = state["orders"][c["order_id"]]
            if c["status"] != "QUEUED":
                result = {"status": "BLOCKED", "reason": "COMMAND_ALREADY_CLAIMED"}
            elif c["kind"] == "NEW" and (state["faults"] or state["halted"] or not state["connected"]):
                result = {"status": "BLOCKED", "reason": "RECONCILE_OR_RESUME_REQUIRED"}
            else:
                intent = state["intents"][o["intent_id"]]
                start = clock(intent["created_clock"])
                expired = (not same_domain(now, start) or now[2] - start[2] >= intent["ttl_ms"] * 1_000_000 or at >= utc_us(intent["expires_at"])
                           or intent["opening"] and at >= utc_us(intent["policy_expires_at"]))
                if c["kind"] == "NEW" and expired:
                    o.update(status="EXPIRED", finalized=True); c["status"] = "OBSOLETE"
                    result = {"status": "EXPIRED_UNSENT"}
                else:
                    c.update(status="SENDING", dispatched_at=event["at"])
                    o["status"] = "SENDING" if c["kind"] == "NEW" else "CANCEL_PENDING"
                    result = {"status": "CLAIMED", "command": deepcopy(c)}
        elif kind in ("CANCEL", "UNCERTAIN"):
            o = state["orders"][event["data"]["order_id"]]
            if kind == "CANCEL":
                result = {"status": "CANCEL_REQUESTED", "result": cancel(state, o, event)}
            else:
                o["status"] = "UNKNOWN"
                fault(state, "BROKER_RESPONSE_LOST")
                result = {"status": "REVIEW", "reason": "BROKER_RESPONSE_LOST"}
        elif kind == "REPORT":
            result = _report(config, state, event["data"]["report"], event)
        elif kind == "RECONCILE":
            result = _reconcile(config, state, event)
        elif kind in ("MARK", "SETTLE"):
            instrument = event["instrument_id"]; price = amount(event["data"]["price"])
            if instrument not in config["instruments"] or (config["ledger_model"] == "CASH" and (kind == "SETTLE" or price <= 0)):
                raise ValidationError("Unsupported mark/settlement")
            state["economic"].append({"kind": kind, "instrument_id": instrument, "price": text(price), "effective_at": event["at"], "reference": event.get("event_ref", event["event_id"])})
            state["ledger"] = rebuild(config, state["economic"], state["fills"])
            result = {"status": "MARKED" if kind == "MARK" else "SETTLED"}
        elif kind in ("DISCONNECT", "HALT", "RESUME"):
            if kind == "DISCONNECT":
                state["connected"] = False; fault(state, "DISCONNECTED")
            elif kind == "HALT":
                state["halted"] = True
            elif state["faults"] or not state["connected"]:
                result = {"status": "BLOCKED", "reason": "RECONCILE_REQUIRED"}
            else:
                state["halted"] = False
            if kind != "RESUME" or not state["faults"] and state["connected"]:
                result = {"status": kind}
        elif kind == "TICK":
            for o in state["orders"].values():
                if o["finalized"]:
                    continue
                intent = state["intents"][o["intent_id"]]; start = clock(intent["created_clock"])
                if not same_domain(now, start) or now[2] - start[2] >= intent["ttl_ms"] * 1_000_000 or at >= utc_us(intent["expires_at"]):
                    cancel(state, o, event)
            result = {"status": "TIMEOUTS_EVALUATED"}
        else:
            raise ValidationError("Unsupported OMS event")
    except (ValidationError, KeyError, TypeError, IndexError) as exc:
        # A malformed economic report must not partially change quantities.
        # Retain the input in the journal and stop orders pending reconciliation.
        state = deepcopy(old); state["revision"] += 1
        if kind in ("REPORT", "RECONCILE"):
            fault(state, "INVALID_" + kind + ":" + str(exc))
        result = {"status": "REVIEW" if kind in ("REPORT", "RECONCILE") else "REJECTED", "reason": str(exc)}
    state.update(last_at=event["at"], last_clock=event["clock"])
    if event.get("event_ref"):
        state["input_refs"].append(event["event_ref"])
    return state, result
