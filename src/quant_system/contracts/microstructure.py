"""Validation for the optional microstructure-v1 feature extension of v0.2."""

import math

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decimal_value


def validate_microstructure(features, observation_kind):
    def require(test, reason):
        if not test:
            raise ValidationError(reason)

    def integer(value, minimum=0, nullable=False):
        require((nullable and value is None) or (type(value) is int and value >= minimum), "Invalid feature integer")

    def numeric(value):
        require(value is None or (type(value) in (int, float) and math.isfinite(value)), "Invalid feature numeric value")

    require(features["time_basis"] == "RECEIVE_MONOTONIC", "Feature version requires producer receive-monotonic time")
    require(isinstance(features.get("config_ref"), str) and bool(features["config_ref"]), "Feature configuration provenance required")
    basis = features["basis"]
    require(isinstance(basis, list) and all(isinstance(r, str) and r for r in basis) and len(basis) == len(set(basis)) and bool(basis), "Invalid feature basis")
    for key in ("ofi_top1_delta", "ofi_window_sum"):
        require(features[key] is None or type(features[key]) is int, "OFI is represented as exact whole contracts")
    d = features.get("details")
    keys = {"kind", "quantity_unit", "price_unit", "coverage", "depth", "weights", "clock", "book_observations", "elapsed_ns",
            "obi_delta_per_observation", "smooth_obi_delta_per_observation", "raw_obi_slope_per_second", "spread", "mid_price", "spread_ticks",
            "bid_depth", "ask_depth", "weighted_bid_depth", "weighted_ask_depth", "ofi_window_count", "ofi_window_complete",
            "trade_window_count", "trade_window_complete", "trade_volume", "signed_trade_volume", "flags"}
    require(isinstance(d, dict) and set(d) == keys, "Invalid microstructure details")
    require(d["kind"] in ("BOOK", "TRADE") and d["quantity_unit"] == "CONTRACT", "Unsupported feature observation/unit")
    require(d["kind"] == observation_kind, "Feature kind differs from its market observation")
    for key in ("price_unit", "coverage"):
        require(isinstance(d[key], str) and bool(d[key]), "Missing feature unit/coverage")
    integer(d["depth"], 1)
    require(d["depth"] <= 5 and isinstance(d["weights"], list) and len(d["weights"]) == d["depth"], "Invalid feature depth/weights")
    require(all(decimal_value(w) >= 0 for w in d["weights"]) and any(decimal_value(w) > 0 for w in d["weights"]), "Invalid feature weights")
    c = d["clock"]
    require(isinstance(c, dict) and set(c) == {"producer_id", "epoch", "sequence", "monotonic_ns", "raw_ref"}, "Invalid feature clock")
    for key in ("producer_id", "epoch", "raw_ref"):
        require(isinstance(c[key], str) and bool(c[key]), "Incomplete feature clock")
    require(c["raw_ref"] in basis, "Feature clock raw must be in its basis")
    integer(c["sequence"], 1); integer(c["monotonic_ns"])
    for key in ("book_observations", "trade_window_count"):
        integer(d[key])
    for key in ("elapsed_ns", "ofi_window_count", "spread_ticks"):
        integer(d[key], nullable=True)
    for key in ("obi_delta_per_observation", "smooth_obi_delta_per_observation", "raw_obi_slope_per_second"):
        numeric(d[key])
    for key in ("spread", "mid_price", "bid_depth", "ask_depth", "weighted_bid_depth", "weighted_ask_depth", "signed_trade_volume"):
        if d[key] is not None:
            decimal_value(d[key])
    for key in ("bid_depth", "ask_depth", "weighted_bid_depth", "weighted_ask_depth"):
        if d[key] is not None:
            require(decimal_value(d[key]) >= 0, "Negative gross depth")
    if d["spread"] is not None:
        require(decimal_value(d["spread"]) > 0, "Nonpositive valid spread")
    require(d["ofi_window_complete"] is None or type(d["ofi_window_complete"]) is bool, "Invalid OFI window coverage")
    require(type(d["trade_window_complete"]) is bool, "Invalid trade window coverage")
    volume = d["trade_volume"]
    require(isinstance(volume, dict) and set(volume) == {"BUY", "SELL", "UNKNOWN"}, "Retain unknown trade direction")
    for value in volume.values():
        if value is not None:
            require(decimal_value(value) >= 0, "Negative gross trade volume")
    require(isinstance(d["flags"], list) and all(isinstance(f, str) and f for f in d["flags"]) and len(d["flags"]) == len(set(d["flags"])), "Invalid feature flags")
    if d["elapsed_ns"] in (None, 0):
        require(features["obi_slope_per_second"] is None and d["raw_obi_slope_per_second"] is None, "Rate cannot use missing/zero elapsed time")
    if d["kind"] == "TRADE":
        require(all(features[k] is None for k in ("obi", "smooth_obi", "obi_slope_per_second", "ofi_top1_delta", "ofi_window_sum")), "Trade cannot refresh book features")
        require(all(d[k] is None for k in ("spread", "mid_price", "spread_ticks", "bid_depth", "ask_depth", "weighted_bid_depth", "weighted_ask_depth")), "Trade cannot refresh book depth or spread")
