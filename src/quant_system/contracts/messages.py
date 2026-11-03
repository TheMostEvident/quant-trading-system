from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decimal_value, encode, utc_us

CONTRACT_VERSION = "0.2.0"
MESSAGE_TYPES = frozenset({"MarketSnapshot", "ExecutionReport", "AccountState", "PositionState", "EventPacket", "UniverseSnapshot", "TradingPolicy"})
ENVELOPE = frozenset({"schema_version", "message_type", "message_id", "run_id", "instrument_id", "source", "source_event_id", "event_at", "received_at", "ready_at", "data_origin", "quality_flags", "lineage", "payload"})
ZERO = Decimal(0)


def _require(condition, reason):
    if not condition:
        raise ValidationError(reason)


def _text(value):
    _require(isinstance(value, str) and bool(value), "Expected non-empty identifier")


def _fields(value, fields):
    _require(isinstance(value, dict) and set(fields) <= value.keys(), "Missing payload fields: " + ", ".join(fields))


def _integer(value, minimum=0):
    _require(type(value) is int and value >= minimum, "Expected integer above minimum")


def _number(value, lower=None, upper=None):
    _require(type(value) in (int, float) and math.isfinite(value), "Expected finite JSON number")
    _require(lower is None or value >= lower, "Number below minimum")
    _require(upper is None or value <= upper, "Number above maximum")


def _charges(value):
    _require(isinstance(value, list), "Charges must retain each asset")
    for item in value:
        _fields(item, ["kind", "asset", "amount"])
        _require(item["kind"] in ("FEE", "TAX"), "Unknown charge kind")
        _text(item["asset"])
        decimal_value(item["amount"])  # A rebate is negative, never silently clamped.


def _validity(p):
    _require(utc_us(p["valid_from"]) < utc_us(p["expires_at"]), "Empty validity interval")


def _validate_message(m: dict) -> None:
    _require(isinstance(m, dict) and set(m) == ENVELOPE, "Invalid message envelope fields")
    _require(m["schema_version"] == CONTRACT_VERSION, "Unsupported contract version")
    _require(m["message_type"] in MESSAGE_TYPES, "Unsupported message type")
    for field in ("message_id", "run_id", "source"):
        _text(m[field])
    for field in ("instrument_id", "source_event_id"):
        if m[field] is not None:
            _text(m[field])
    _require(utc_us(m["received_at"]) <= utc_us(m["ready_at"]), "ready_at precedes collection")
    if m["event_at"] is not None:
        utc_us(m["event_at"])
    _require(m["data_origin"] in ("SYNTHETIC", "OBSERVED", "DERIVED"), "Unknown data origin")
    for field in ("quality_flags", "lineage"):
        _require(isinstance(m[field], list) and all(isinstance(x, str) and x for x in m[field]), "Invalid identifier list")
        _require(len(m[field]) == len(set(m[field])), "Duplicate identifiers in message")
    _require(m["message_id"] not in m["lineage"], "Self-referential lineage")
    encode(m)
    p, kind = m["payload"], m["message_type"]
    _require(isinstance(p, dict), "Payload must be an object")
    if kind == "MarketSnapshot":
        _fields(p, ["instrument_spec_ref", "trading_date", "update_kind", "market_status", "book", "trade", "features"])
        date.fromisoformat(p["trading_date"])
        _require(p["update_kind"] in ("BOOK", "TRADE", "STATUS"), "Unknown market update")
        _require(p["market_status"] in ("CONTINUOUS", "AUCTION", "HALTED", "CLOSED", "UNKNOWN"), "Unknown market status")
        if p["update_kind"] == "BOOK":
            _fields(p["book"], ["coverage", "sync_status", "source_sequence", "bids", "asks"])
            book = p["book"]
            for side in ("bids", "asks"):
                _require(isinstance(book[side], list), "Invalid book side")
                prices = []
                for level in book[side]:
                    _fields(level, ["price", "quantity"])
                    prices.append(decimal_value(level["price"]))
                    _require(decimal_value(level["quantity"]) > 0, "Active depth must have positive size")
                _require(prices == sorted(set(prices), reverse=side == "bids"), "Book must be sorted with unique prices")
            if book["bids"] and book["asks"]:
                _require(decimal_value(book["bids"][0]["price"]) < decimal_value(book["asks"][0]["price"]), "Crossed or locked book")
        elif p["book"] is not None:
            raise ValidationError("Non-book update cannot make cached depth fresh")
        if p["trade"] is not None:
            _fields(p["trade"], ["trade_id", "price", "quantity", "aggressor"])
            decimal_value(p["trade"]["price"])
            _require(decimal_value(p["trade"]["quantity"]) > 0, "Trade quantity must be positive")
            _require(p["trade"]["aggressor"] in ("BUY", "SELL", "UNKNOWN"), "Invalid aggressor")
        if p["features"] is not None:
            f = p["features"]
            _fields(f, ["feature_version", "time_basis", "basis", "obi", "smooth_obi", "obi_slope_per_second", "ofi_top1_delta", "ofi_window_sum", "ofi_window_ms"])
            _require(f["time_basis"] in ("RECEIVE_MONOTONIC", "EVENT_TIME"), "Unknown feature time basis")
            for field in ("obi", "smooth_obi"):
                if f[field] is not None:
                    _number(f[field], -1, 1)
            for field in ("obi_slope_per_second", "ofi_top1_delta", "ofi_window_sum"):
                if f[field] is not None:
                    _number(f[field])
            _integer(f["ofi_window_ms"])
    elif kind == "ExecutionReport":
        _fields(p, ["account_id", "client_order_id", "broker_order_id", "event_type", "order_status", "side", "position_effect", "order_qty", "cum_filled_qty", "leaves_qty", "last_fill", "correction_of", "reason"])
        _require(p["side"] in ("BUY", "SELL"), "Unknown side")
        _require(p["position_effect"] in ("OPEN", "CLOSE", "UNKNOWN"), "Unknown position effect")
        _require(p["order_status"] in ("OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED", "UNKNOWN"), "Unknown order status")
        _require(p["event_type"] in ("ACK", "REJECT", "FILL", "CANCEL_ACK", "CANCEL_REJECT", "REPLACE_ACK", "EXPIRE", "RECONCILE", "CORRECTION"), "Unknown execution type")
        qty, filled, leaves = (decimal_value(p[x], minimum=ZERO) for x in ("order_qty", "cum_filled_qty", "leaves_qty"))
        _require(qty >= filled and leaves <= qty - filled, "Inconsistent order quantities")
        if p["event_type"] == "FILL":
            f = p["last_fill"]
            _fields(f, ["fill_id", "quantity", "price", "charges"])
            _text(f["fill_id"])
            _require(0 < decimal_value(f["quantity"]) <= filled, "Invalid fill quantity")
            decimal_value(f["price"])
            _charges(f["charges"])
        else:
            _require(p["last_fill"] is None, "Only FILL carries a new fill")
    elif kind == "AccountState":
        _fields(p, ["account_id", "account_version", "ledger_model", "balances", "margin", "valuation", "unsettled_pnl", "adjustments", "reconciliation"])
        _integer(p["account_version"], 1)
        _require(p["ledger_model"] in ("CASH", "FUTURES_MARGIN"), "Unsupported ledger model")
        _require(p["ledger_model"] != "CASH" or p["margin"] is None, "Cash ledger cannot silently use futures margin")
        _require(isinstance(p["balances"], list), "Invalid balances")
        assets = set()
        for b in p["balances"]:
            _fields(b, ["asset", "wallet", "free", "locked"])
            _text(b["asset"])
            _require(b["asset"] not in assets, "Duplicate balance asset")
            assets.add(b["asset"])
            decimal_value(b["wallet"]); decimal_value(b["free"])
            decimal_value(b["locked"], minimum=ZERO)
        if p["margin"] is not None:
            g = p["margin"]
            _fields(g, ["asset", "initial_used", "maintenance_required", "order_reserved", "available_margin", "rule_ref", "status"])
            for key in ("initial_used", "maintenance_required", "order_reserved"):
                decimal_value(g[key], minimum=ZERO)
            decimal_value(g["available_margin"])
        _fields(p["valuation"], ["equity", "asset", "as_of", "model_ref", "fx_refs"])
        decimal_value(p["valuation"]["equity"])
        utc_us(p["valuation"]["as_of"])
        for b in p["unsettled_pnl"]:
            _fields(b, ["asset", "amount"]); decimal_value(b["amount"])
    elif kind == "PositionState":
        _fields(p, ["account_id", "account_state_ref", "position_version", "instrument_spec_ref", "position_status", "position_mode", "net_qty", "quantity_unit", "avg_entry_price", "pnl_reference_price", "mark_price", "contract_multiplier", "pnl_asset", "reserved_buy_qty", "reserved_sell_qty", "active_orders", "unrealized_pnl_gross", "charges_paid", "settlement_ref", "reconciliation"])
        _integer(p["position_version"], 1)
        qty = decimal_value(p["net_qty"])
        _require(p["quantity_unit"] in ("SHARE", "CONTRACT", "BASE_ASSET"), "Unknown quantity unit")
        if p["quantity_unit"] in ("CONTRACT", "SHARE"):
            _require(qty == qty.to_integral_value(), "Contract/share quantity must be integral")
        if p["quantity_unit"] in ("SHARE", "BASE_ASSET"):
            _require(qty >= 0, "Cash inventory cannot be negative")
        for key in ("avg_entry_price", "pnl_reference_price", "mark_price"):
            if p[key] is not None:
                decimal_value(p[key])
        _require(decimal_value(p["contract_multiplier"]) > 0, "Invalid multiplier")
        for key in ("reserved_buy_qty", "reserved_sell_qty"):
            decimal_value(p[key], minimum=ZERO)
        decimal_value(p["unrealized_pnl_gross"])
        _charges(p["charges_paid"])
        _require(p["reconciliation"] in ("INTERNAL_ONLY", "MATCHED", "UNRECONCILED", "MISMATCH"), "Unknown reconciliation status")
    elif kind == "EventPacket":
        _fields(p, ["event_id", "event_revision", "event_type", "status", "document_ids", "evidence_refs", "published_at", "published_date", "publication_precision", "affected_targets", "model_id", "prompt_version", "raw_response_ref", "supersedes", "expires_at"])
        _integer(p["event_revision"], 1)
        utc_us(p["expires_at"])
        _require(p["publication_precision"] in ("SECOND", "DATE", "UNKNOWN"), "Unknown publication precision")
        if p["published_at"] is not None:
            utc_us(p["published_at"])
        if p["published_date"] is not None:
            date.fromisoformat(p["published_date"])
        for target in p["affected_targets"]:
            if "relevance_noul" in target:
                _text(target["relevance_noul"]["statement"])
                _number(target["relevance_noul"]["value"], 0, 1)
            probs = target["direction_probabilities"]
            _require(set(probs) == {"POSITIVE", "NEGATIVE", "MIXED", "UNCLEAR"}, "Incomplete direction distribution")
            for v in probs.values():
                _number(v, 0, 1)
            _require(abs(sum(probs.values()) - 1) <= 1e-6, "Probabilities do not sum to one")
    elif kind == "UniverseSnapshot":
        _fields(p, ["universe_id", "revision", "source_kind", "as_of", "valid_from", "expires_at", "members", "excluded", "coverage", "rule_version"])
        _integer(p["revision"], 1); _validity(p); utc_us(p["as_of"])
        _require(p["source_kind"] in ("SEED", "LAYER0"), "Unknown universe source")
        _require(isinstance(p["members"], list), "Invalid universe members")
    elif kind == "TradingPolicy":
        _fields(p, ["policy_id", "provider_kind", "provider_epoch", "revision", "account_id", "instrument_spec_ref", "universe_ref", "valid_from", "expires_at", "allowed_position_directions", "target_position_qty", "entry", "hold_exit", "smoothing", "execution_limits", "risk_budget_ref", "regime", "rationale", "input_refs"])
        _validity(p); _integer(p["revision"], 1)
        _require(p["provider_kind"] in ("STATIC", "LAYER2"), "Unknown policy provider")
        dirs = p["allowed_position_directions"]
        _require(isinstance(dirs, list) and len(set(dirs)) == len(dirs) and set(dirs) <= {"LONG", "SHORT"}, "Unknown allowed direction")
        qty = decimal_value(p["target_position_qty"])
        _require(qty <= 0 or "LONG" in dirs, "Long target disallowed")
        _require(qty >= 0 or "SHORT" in dirs, "Short target disallowed")
        e, h, lim = p["entry"], p["hold_exit"], p["execution_limits"]
        for key in ("long_hold_obi", "short_hold_obi"):
            if h[key] is not None:
                _number(h[key], -1, 1)
        _integer(h["max_hold_ms"], 1)
        decimal_value(h["exit_target_qty"])
        smoothing = p["smoothing"]
        _require(smoothing["method"] in ("TIME_EMA", "EVENT_EMA"), "Unknown smoothing basis")
        if smoothing["method"] == "TIME_EMA":
            _integer(smoothing["tau_ms"], 1)
        else:
            _number(smoothing["alpha"], 0, 1)
            _require(smoothing["alpha"] > 0, "EMA alpha must be positive")
        for key in ("obi_long", "obi_short"):
            if e[key] is not None:
                _number(e[key], -1, 1)
        if e["obi_long"] is not None and h["long_hold_obi"] is not None:
            _require(e["obi_long"] > h["long_hold_obi"], "Invalid long hysteresis")
        if e["obi_short"] is not None and h["short_hold_obi"] is not None:
            _require(e["obi_short"] < h["short_hold_obi"], "Invalid short hysteresis")
        _integer(e["min_observations"], 1)
        for key in ("min_duration_ms", "max_observation_gap_ms"):
            _integer(e[key])
        _require(qty.copy_abs() <= decimal_value(lim["max_position_qty"], minimum=ZERO), "Target exceeds quantity limit")
        for key in ("max_order_notional", "max_initial_margin"):
            if lim[key] is not None:
                _text(lim[key]["asset"]); decimal_value(lim[key]["amount"], minimum=ZERO)
        for key in ("max_spread_ticks", "max_quote_age_ms", "order_ttl_ms", "max_retries", "max_slippage_ticks"):
            _integer(lim[key])
        _require(lim["price_reference_mode"] in ("AT_INTENT_CREATION", "FIXED_REFERENCE"), "Unknown price anchor")
        if lim["price_reference_mode"] == "FIXED_REFERENCE":
            _text(lim["price_reference_ref"])


def validate_message(message: dict) -> None:
    try:
        _validate_message(message)
    except ValidationError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValidationError("Malformed contract payload") from exc
