"""Single-writer STATIC policy evaluator, with staged and serializable state.

Inputs are an explicit availability bundle, not a live account adapter. Every
output is a total target for downstream review, never an order or a fill.
"""

from dataclasses import dataclass
from fractions import Fraction

from quant_system.contracts import validate_message
from quant_system.contracts.static_policy import VERSION, validate_static_policy
from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decode, digest, encode, decimal_value, utc_us
from quant_system.market.features import BLOCKED, decimal_string, validate_config


def number(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValidationError("Bounded decimal required")
    return Fraction(decimal_value(value))


def clock(value):
    if not isinstance(value, dict) or set(value) != {"producer_id", "epoch", "monotonic_ns"}:
        raise ValidationError("Explicit monotonic clock domain required")
    if any(not isinstance(value[k], str) or not value[k] for k in ("producer_id", "epoch")) or type(value["monotonic_ns"]) is not int or value["monotonic_ns"] < 0:
        raise ValidationError("Invalid monotonic clock")
    return value["producer_id"], value["epoch"], value["monotonic_ns"]


def same_domain(a, b):
    return a[:2] == b[:2]


def money(value, asset):
    if not isinstance(value, dict) or value.get("asset") != asset:
        raise ValidationError("Money assets must match; no implicit FX")
    amount = number(value["amount"])
    if amount < 0:
        raise ValidationError("Nonnegative money required")
    return amount


@dataclass(frozen=True)
class SignalUpdate:
    key: tuple
    expected: bytes | None
    state: bytes
    result: bytes

    @property
    def decision(self):
        return decode(self.result)


class StaticSignalEngine:
    def __init__(self):
        self._states = {}

    def prepare(self, bundle):
        # Freeze caller-owned dictionaries, including cached decisions.
        b = decode(encode(bundle))
        try:
            return self._prepare(b)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ValidationError("Malformed static signal input") from exc

    def _prepare(self, b):
        as_of = utc_us(b["as_of"])
        current = clock(b["clock"])
        for field in ("decision_id", "run_id", "account_id", "instrument_id"):
            if not isinstance(b[field], str) or not b[field]:
                raise ValidationError("Decision scope identifiers required")
        key = (b["run_id"], b["account_id"], b["instrument_id"])
        previous = self._states.get(key)
        old = decode(previous) if previous else {}
        fingerprint = digest(encode(b))
        if old.get("decision_id") == b["decision_id"]:
            if old["fingerprint"] != fingerprint:
                raise ValidationError("Decision ID reused with different inputs")
            return SignalUpdate(key, previous, previous, encode(old["decision"]))
        if old and (as_of < old["as_of_us"] or (same_domain(current, old["clock"]) and current[2] < old["clock"][2])):
            raise ValidationError("Decision clock regressed")
        state = dict(old, decision_id=b["decision_id"], fingerprint=fingerprint, as_of_us=as_of, clock=list(current), count=0, start_ns=None)
        result = {"signal_version": VERSION, "decision_id": b["decision_id"], "run_id": b["run_id"],
                  "account_id": b["account_id"], "instrument_id": b["instrument_id"], "as_of": b["as_of"],
                  "provider_kind": "STATIC", "policy_ref": None, "input_refs": [], "evidence_ref": None,
                  "action": "WAIT", "target_position_qty": None, "actual_position_qty": None,
                  "quantity_unit": None, "quote_ref": None, "entry_allowed": False, "policy_expired": False,
                  "observations": 0, "duration_ns": 0, "risk_checks": {}, "reason_codes": [], "orders_created": 0}

        def finish(action, target, *reasons):
            result.update(action=action, target_position_qty=None if target is None else decimal_string(target), reason_codes=list(reasons))
            result["observations"] = state["count"]
            result["duration_ns"] = 0 if state["start_ns"] is None else state["last_observation_ns"] - state["start_ns"]
            state["decision"] = result
            if result["entry_allowed"]:
                state["count"], state["start_ns"] = 0, None
            return SignalUpdate(key, previous, encode(state), encode(result))

        messages = {}
        for name, kind in (("policy", "TradingPolicy"), ("market", "MarketSnapshot"), ("account", "AccountState"), ("position", "PositionState")):
            m = b.get(name)
            if m is None:
                continue
            validate_message(m)
            if m["message_type"] != kind or m["run_id"] != b["run_id"] or (name != "account" and m["instrument_id"] != b["instrument_id"]):
                raise ValidationError("Input type/run/instrument mismatch")
            seen = utc_us(b["seen_at"][m["message_id"]])
            if not utc_us(m["ready_at"]) <= seen <= as_of:
                raise ValidationError("Input was unavailable at the decision boundary")
            messages[name] = m
            result["input_refs"].append(m["message_id"])
        observation = messages.get("market")
        f = observation["payload"]["features"] if observation else None
        if f and f["feature_version"] == "microstructure-v1":
            c = f["details"]["clock"]
            cursor = [c["producer_id"], c["epoch"], c["sequence"], c["monotonic_ns"], observation["message_id"], digest(encode(observation))]
            last = old.get("market_cursor")
            if last and cursor[:2] == last[:2] and (cursor[2] < last[2] or cursor[3] < last[3] or (cursor[2] == last[2] and cursor != last)):
                raise ValidationError("Market observation counter/time regressed or conflicts")
            state["market_cursor"] = cursor
        if not {"policy", "account", "position"} <= set(messages):
            return finish("REVIEW", None, "MISSING_STATE_OR_POLICY")
        policy, account, position = (messages[k] for k in ("policy", "account", "position"))
        p, a, pos = (m["payload"] for m in (policy, account, position))
        validate_static_policy(p)
        result["policy_ref"] = policy["message_id"]
        result["policy_expired"] = as_of >= utc_us(p["expires_at"])
        if any(x["account_id"] != b["account_id"] for x in (p, a, pos)) or pos["account_state_ref"] != account["message_id"]:
            raise ValidationError("Account/position linkage mismatch")
        qty, target = number(pos["net_qty"]), number(p["target_position_qty"])
        result.update(actual_position_qty=pos["net_qty"], quantity_unit=pos["quantity_unit"])
        # Versions never roll back. A policy owner transition must be declared
        # explicitly and may not return to an already retired epoch.
        for name, m, version in (("account", account, a["account_version"]), ("position", position, pos["position_version"])):
            identity = [version, digest(encode(m))]
            prior = old.get(name)
            if prior and (version < prior[0] or (version == prior[0] and identity != prior)):
                raise ValidationError("State version rollback or conflicting duplicate")
            state[name] = identity
        owner = [p["provider_epoch"], p["revision"], digest(encode(policy))]
        prior = old.get("policy")
        retired = old.get("retired_policy_epochs", [])
        if prior and owner[0] != prior[0]:
            if b.get("policy_transition_from") != prior[0] or owner[0] in retired or len(retired) >= 32:
                raise ValidationError("Unconfirmed/retired policy owner transition")
            retired = retired + [prior[0]]
        elif prior and (owner[1] < prior[1] or (owner[1] == prior[1] and owner != prior)):
            raise ValidationError("Policy revision rollback or conflict")
        state.update(policy=owner, retired_policy_epochs=retired)
        if any(m["quality_flags"] for m in (policy, account, position)) or a["reconciliation"] not in ("MATCHED", "INTERNAL_ONLY") or pos["reconciliation"] not in ("MATCHED", "INTERNAL_ONLY") or pos["position_status"] not in ("FLAT", "OPEN", "PARTIALLY_FILLED", "CLOSING", "PENDING"):
            return finish("REVIEW", None, "STATE_UNCERTAIN")
        if any(as_of - utc_us(m["ready_at"]) >= p["state_max_age_ms"] * 1000 for m in (account, position)):
            return finish("REVIEW", None, "STATE_STALE")
        # INTERNAL_ONLY is meaningful solely in explicitly synthetic scenarios.
        synthetic = all(m["data_origin"] == "SYNTHETIC" for m in messages.values())
        if not synthetic and any(m["data_origin"] == "SYNTHETIC" for m in messages.values()):
            raise ValidationError("Synthetic and observed execution evidence cannot be mixed")
        if not synthetic and (a["reconciliation"] != "MATCHED" or pos["reconciliation"] != "MATCHED"):
            return finish("REVIEW", None, "ACCOUNT_NOT_MATCHED")
        evidence = b["evidence"]
        if evidence["evidence_version"] != "static-context-v1" or any(evidence[k] != b[k] for k in ("run_id", "account_id", "instrument_id")):
            raise ValidationError("Evidence scope mismatch")
        result["evidence_ref"] = evidence["evidence_id"]
        if not utc_us(evidence["ready_at"]) <= utc_us(b["seen_at"][evidence["evidence_id"]]) <= as_of:
            raise ValidationError("Evidence unavailable at boundary")
        for ref in ("instrument_spec_ref", "risk_budget_ref", "universe_ref", "feature_config_ref"):
            if evidence[ref] != p[ref]:
                raise ValidationError("Policy evidence reference mismatch")
        if pos["instrument_spec_ref"] != p["instrument_spec_ref"]:
            raise ValidationError("Position specification mismatch")
        if not utc_us(evidence["valid_from"]) <= as_of < utc_us(evidence["expires_at"]):
            return finish("REVIEW", None, "EVIDENCE_EXPIRED_OR_NOT_ACTIVE")
        if evidence["data_origin"] != ("SYNTHETIC" if synthetic else "OBSERVED"):
            raise ValidationError("Synthetic evidence cannot authorize observed inputs")
        spec, risk = evidence["specification"], evidence["risk"]
        bindings = {}
        for name, ref, data in (("spec", p["instrument_spec_ref"], spec), ("features", p["feature_config_ref"], evidence["feature_config"]),
                                ("risk", p["risk_budget_ref"], {k: risk[k] for k in ("max_abs_position_qty", "max_gross_notional")})):
            binding = [ref, digest(encode(data))]
            prior_binding = old.get("bindings", {}).get(name)
            if prior_binding and prior_binding[0] == ref and prior_binding != binding:
                raise ValidationError("Immutable evidence reference reused with different data")
            bindings[name] = binding
        state["bindings"] = bindings
        unit, asset = spec["quantity_unit"], spec["pnl_asset"]
        if unit != pos["quantity_unit"] or spec["position_mode"] != "NET" or pos["position_mode"] != "NET":
            return finish("REVIEW", None, "UNSUPPORTED_UNIT_OR_POSITION_MODE")
        future = spec["asset_class"] == "FUTURE" and unit == "CONTRACT"
        spot = (spec["asset_class"], unit) in (("EQUITY", "SHARE"), ("CRYPTO_SPOT", "BASE_ASSET"))
        if not (future or spot) or (future and spec["pnl_model"] != "LINEAR"):
            return finish("REVIEW", None, "UNSUPPORTED_PRODUCT")
        if spot and (target < 0 or qty < 0):
            return finish("WAIT", None, "SPOT_SHORT_UNSUPPORTED")
        if spot and a["ledger_model"] != "CASH":
            return finish("REVIEW", None, "SPOT_REQUIRES_CASH_LEDGER")
        step, minimum, multiplier = (number(spec[x]) for x in ("qty_step", "min_qty", "contract_multiplier"))
        if step <= 0 or minimum <= 0 or multiplier <= 0 or (unit in ("CONTRACT", "SHARE") and step.denominator != 1) or (spot and multiplier != 1):
            raise ValidationError("Invalid specification quantity/multiplier")
        if number(pos["contract_multiplier"]) != multiplier or pos["pnl_asset"] != asset:
            raise ValidationError("Position economics differ from specification")
        if target and (abs(target) < minimum or target % step or (unit in ("CONTRACT", "SHARE") and target.denominator != 1)):
            return finish("WAIT", None, "TARGET_QUANTITY_INVALID")
        if qty % step:
            return finish("REVIEW", None, "POSITION_QUANTITY_INVALID")
        if (pos["position_status"] == "FLAT" and qty) or (pos["position_status"] == "OPEN" and not qty):
            return finish("REVIEW", None, "POSITION_STATUS_CONFLICT")
        reserved_buy, reserved_sell = (number(pos[k]) for k in ("reserved_buy_qty", "reserved_sell_qty"))
        if reserved_buy < 0 or reserved_sell < 0:
            raise ValidationError("Negative order reservation")
        if spot and reserved_sell > qty:
            return finish("REVIEW", None, "SPOT_OVERSELL_RESERVED")
        pending = bool(reserved_buy or reserved_sell or pos["active_orders"])
        result["risk_checks"]["worst_abs_qty_with_reservations"] = decimal_string(max(abs(qty + reserved_buy), abs(qty - reserved_sell), abs(target)))
        # An opening time is supplied by a fill/lifecycle record, not by a signal.
        lifecycle = evidence.get("position_lifecycle")
        if qty:
            if not lifecycle or lifecycle["position_state_ref"] != position["message_id"] or not lifecycle.get("first_fill_ref"):
                return finish("REVIEW", None, "FIRST_FILL_EVIDENCE_REQUIRED")
            opened = clock(lifecycle["first_fill_clock"])
            if not same_domain(opened, current) or opened[2] > current[2]:
                return finish("REVIEW", None, "HOLD_CLOCK_UNVERIFIED")
            origin = [opened[0], opened[1], opened[2], lifecycle["first_fill_ref"], 1 if qty > 0 else -1]
            if old.get("hold_origin") and old["hold_origin"] != origin:
                return finish("REVIEW", None, "FIRST_FILL_OR_DIRECTION_CHANGED_WITHOUT_FLAT")
            state["hold_origin"] = origin
        if pending:
            return finish("CANCEL_REVIEW", None, "PENDING_ORDERS_REQUIRE_RECONCILIATION")
        if pos["position_status"] not in ("FLAT", "OPEN"):
            return finish("REVIEW", None, "POSITION_TRANSITION_UNCONFIRMED")
        if not qty:
            state.pop("hold_origin", None)
        reversal_version = old.get("reversal_close_version")
        if not qty and reversal_version is not None:
            if pos["position_version"] <= reversal_version:
                return finish("REVIEW", None, "FLAT_CONFIRMATION_REQUIRED")
            state.pop("reversal_close_version", None)
        active = utc_us(p["valid_from"]) <= as_of < utc_us(p["expires_at"])
        if utc_us(p["valid_from"]) > as_of:
            return finish("REVIEW" if qty else "WAIT", None, "POLICY_NOT_ACTIVE")
        if qty and current[2] - state["hold_origin"][2] >= p["hold_exit"]["max_hold_ms"] * 1_000_000:
            return finish("EXIT", 0, "MAX_HOLD_REACHED")
        if qty and (target == 0 or qty * target < 0):
            if qty * target < 0:
                state["reversal_close_version"] = pos["position_version"]
            return finish("EXIT", 0, "REVERSAL_CLOSE_FIRST" if target else "POLICY_TARGET_FLAT")
        if qty and abs(target) < abs(qty):
            return finish("REDUCE", target, "POLICY_TARGET_REDUCTION")
        quantity_limit = min(number(p["execution_limits"]["max_position_qty"]), number(risk["max_abs_position_qty"]))
        if quantity_limit < 0:
            raise ValidationError("Nonnegative risk quantity limit required")
        if abs(qty) > quantity_limit:
            return finish("REVIEW", None, "EXISTING_QUANTITY_LIMIT")
        if qty and future:
            margin = a["margin"]
            if a["ledger_model"] != "FUTURES_MARGIN" or not margin or margin["status"] != "NORMAL" or margin["asset"] != asset or margin["rule_ref"] != spec["margin_rule_ref"] or number(margin["available_margin"]) < 0:
                return finish("REVIEW", None, "EXISTING_MARGIN_REVIEW_REQUIRED")
        if spot:
            return finish("REVIEW" if qty else "WAIT", None, "SPOT_ENTRY_OR_HOLD_NOT_IMPLEMENTED")
        m = messages.get("market")
        if m is None:
            return finish("REVIEW" if qty else "WAIT", None, "CURRENT_BOOK_REQUIRED")
        market = m["payload"]
        if market["instrument_spec_ref"] != p["instrument_spec_ref"]:
            raise ValidationError("Market specification mismatch")
        if set(m["quality_flags"]) & BLOCKED or market["market_status"] != "CONTINUOUS":
            return finish("REVIEW" if qty else "WAIT", None, "MARKET_UNUSABLE")
        if market["update_kind"] != "BOOK" or not market["book"] or market["book"]["sync_status"] != "SYNCED":
            return finish("REVIEW" if qty else "WAIT", None, "CURRENT_BOOK_REQUIRED")
        features = market["features"]
        if not features or features["feature_version"] != "microstructure-v1":
            return finish("REVIEW" if qty else "WAIT", None, "SUPPORTED_FEATURES_REQUIRED")
        if (features["config_ref"] != p["feature_config_ref"] or features["details"]["quantity_unit"] != unit or features["details"]["price_unit"] != spec["price_unit"] or
                features["details"]["coverage"] != market["book"]["coverage"] or features["details"]["clock"]["raw_ref"] not in m["lineage"]):
            return finish("REVIEW" if qty else "WAIT", None, "FEATURE_DEFINITION_MISMATCH")
        config = validate_config(evidence["feature_config"])
        if any(p["smoothing"].get(k) != config["smoothing"].get(k) for k in ("method", "tau_ms", "alpha")) or features["details"]["weights"] != config["weights"] or features["details"]["depth"] != config["depth"]:
            return finish("REVIEW" if qty else "WAIT", None, "FEATURE_DEFINITION_MISMATCH")
        c = features["details"]["clock"]
        quote_clock = (c["producer_id"], c["epoch"], c["monotonic_ns"])
        quote_age = current[2] - quote_clock[2]
        if not same_domain(current, quote_clock) or quote_age < 0 or quote_age >= p["execution_limits"]["max_quote_age_ms"] * 1_000_000:
            return finish("REVIEW" if qty else "WAIT", None, "QUOTE_CLOCK_OR_AGE_INVALID")
        if as_of - utc_us(m["received_at"]) >= p["execution_limits"]["max_quote_age_ms"] * 1000:
            return finish("REVIEW" if qty else "WAIT", None, "QUOTE_STALE_UTC_GUARD")
        result["quote_ref"] = m["message_id"]
        obi = features["smooth_obi"]
        if obi is None:
            return finish("REVIEW" if qty else "WAIT", None, "OBI_UNAVAILABLE")
        book = market["book"]
        if not book["bids"] or not book["asks"]:
            return finish("REVIEW" if qty else "WAIT", None, "EMPTY_BOOK")
        bid, ask = number(book["bids"][0]["price"]), number(book["asks"][0]["price"])
        tick = number(spec["tick_size"])
        if tick <= 0:
            raise ValidationError("Positive tick size required")
        limit = p["execution_limits"]
        worst_qty = max(abs(qty + reserved_buy), abs(qty - reserved_sell), abs(target))
        conservative_price = max(abs(bid), abs(ask)) + limit["max_slippage_ticks"] * tick
        other_notional = money(risk["other_gross_notional"], asset)
        max_gross = money(risk["max_gross_notional"], asset)
        order_notional = abs(target - qty) * conservative_price * multiplier
        gross = worst_qty * conservative_price * multiplier + other_notional
        result["risk_checks"].update(order_notional=decimal_string(order_notional), gross_notional=decimal_string(gross), asset=asset)
        if qty and abs(qty) * conservative_price * multiplier + other_notional > max_gross:
            return finish("REVIEW", None, "EXISTING_NOTIONAL_LIMIT")
        # Exit/hold hysteresis is independent of entry persistence and slope.
        if qty:
            hold = p["hold_exit"]["long_hold_obi" if qty > 0 else "short_hold_obi"]
            if hold is None or (obi < hold if qty > 0 else obi > hold):
                return finish("EXIT", 0, "HOLD_THRESHOLD_BROKEN")
            if qty == target:
                return finish("HOLD", qty, "WITHIN_HOLD_BAND")
        if not active:
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "POLICY_EXPIRED_NO_INCREASE")
        if utc_us(m["received_at"]) < max(utc_us(policy["ready_at"]), utc_us(p["valid_from"])):
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "QUOTE_PRECEDES_POLICY")
        direction = "LONG" if target > 0 else "SHORT" if target < 0 else None
        if direction is None:
            return finish("WAIT", None, "TARGET_FLAT")
        cap = evidence["account_capability"]
        capability_key = "can_open_long" if target > 0 else "can_open_short"
        if direction not in p["allowed_position_directions"] or cap.get("verified") is not True or cap.get(capability_key) is not True or spec.get(capability_key) is not True:
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "DIRECTION_OR_CAPABILITY_BLOCKED")
        if (ask - bid) / tick > p["execution_limits"]["max_spread_ticks"]:
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "SPREAD_LIMIT")
        if worst_qty > quantity_limit:
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "QUANTITY_LIMIT")
        if order_notional > money(limit["max_order_notional"], asset) or gross > max_gross:
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "NOTIONAL_LIMIT")
        if future:
            margin = a["margin"]
            quote = risk.get("margin_quote")
            if a["ledger_model"] != "FUTURES_MARGIN" or not margin or margin["status"] != "NORMAL" or margin["asset"] != asset or margin["rule_ref"] != spec["margin_rule_ref"] or not quote or limit["max_initial_margin"] is None:
                return finish("HOLD" if qty else "WAIT", qty if qty else None, "MARGIN_UNVERIFIED")
            if (quote["account_state_ref"] != account["message_id"] or quote["position_state_ref"] != position["message_id"] or quote["quote_ref"] != m["message_id"] or number(quote["target_position_qty"]) != target or quote["margin_rule_ref"] != margin["rule_ref"] or quote["available_basis"] != "NET_OF_ORDER_RESERVED"):
                return finish("HOLD" if qty else "WAIT", qty if qty else None, "MARGIN_QUOTE_MISMATCH")
            required, incremental = money(quote["total_initial_required"], asset), money(quote["incremental_required"], asset)
            if incremental < max(required - number(margin["initial_used"]), 0):
                return finish("HOLD" if qty else "WAIT", qty if qty else None, "MARGIN_QUOTE_INCONSISTENT")
            used = number(margin["initial_used"]) + number(margin["order_reserved"])
            maximum = money(limit["max_initial_margin"], asset)
            result["risk_checks"].update(initial_required=decimal_string(required), incremental_margin=decimal_string(incremental), available_margin=margin["available_margin"], reserved_margin=margin["order_reserved"])
            if required > maximum or used + incremental > maximum or incremental > number(margin["available_margin"]):
                return finish("HOLD" if qty else "WAIT", qty if qty else None, "MARGIN_LIMIT")
        threshold = p["entry"]["obi_long" if target > 0 else "obi_short"]
        slope = features["obi_slope_per_second"]
        slope_limit = p["entry"].get("slope_min_per_second")
        qualifies = threshold is not None and (obi >= threshold if target > 0 else obi <= threshold)
        qualifies &= slope_limit is None or (slope is not None and slope * (1 if target > 0 else -1) >= slope_limit)
        signature = [owner[0], owner[1], policy["message_id"], c["producer_id"], c["epoch"], market["trading_date"], p["instrument_spec_ref"], p["feature_config_ref"], direction]
        identity = [c["sequence"], quote_clock[2], m["message_id"], digest(encode(m))]
        prior_market = old.get("market_identity")
        same_scope = signature == old.get("filter_scope")
        if same_scope and prior_market:
            if identity[0] < prior_market[0] or identity[1] < prior_market[1] or (identity[0] == prior_market[0] and identity != prior_market):
                raise ValidationError("Observation counter/time regressed or conflicting duplicate")
            if identity == prior_market:
                # Re-evaluation/timers do not create an additional observation
                # or extend persistence. Availability/risk checks still ran.
                state.update(count=old.get("count", 0), start_ns=old.get("start_ns"), last_observation_ns=old.get("last_observation_ns"))
                return finish("HOLD" if qty else "WAIT", qty if qty else None, "DUPLICATE_OBSERVATION")
        reset_flags = {"FIRST_BOOK", "CONTEXT_RESET", "GAP_RESET", "ZERO_WEIGHTED_DEPTH"}
        continuous = same_scope and prior_market and quote_clock[2] - prior_market[1] < p["entry"]["max_observation_gap_ms"] * 1_000_000 and not (set(features["details"]["flags"]) & reset_flags)
        state.update(filter_scope=signature, market_identity=identity, last_observation_ns=quote_clock[2])
        if not qualifies:
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "ENTRY_THRESHOLD_NOT_MET")
        state["count"] = min(old.get("count", 0) + 1, p["entry"]["min_observations"]) if continuous else 1
        state["start_ns"] = old.get("start_ns") if continuous and old.get("count", 0) else quote_clock[2]
        # Duration uses source observation instants, not delayed decision time.
        duration = quote_clock[2] - state["start_ns"]
        if state["count"] < p["entry"]["min_observations"] or duration < p["entry"]["min_duration_ms"] * 1_000_000:
            return finish("HOLD" if qty else "WAIT", qty if qty else None, "PERSISTENCE_NOT_MET")
        result["entry_allowed"] = True
        # Consume a qualifying streak once. No orders/fills are fabricated.
        return finish("INCREASE" if qty else "ENTER", target, "FIXED_POLICY_FILTERS_PASSED")

    def commit(self, update):
        if self._states.get(update.key) is not update.expected:
            raise ValidationError("Stale signal transition; one ordered writer required")
        self._states[update.key] = update.state

    def process(self, bundle):
        update = self.prepare(bundle)
        self.commit(update)
        return update.decision
