"""The executable STATIC subset; legacy v0.2 policies remain readable."""

import math

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decimal_value

VERSION = "static-obi-v1"


def validate_static_policy(p):
    def require(ok, reason):
        if not ok:
            raise ValidationError(reason)

    require(p.get("strategy_version") == VERSION and p["provider_kind"] == "STATIC", "Executable policy must be explicitly STATIC")
    require(p["regime"] is None, "Static baseline cannot claim a predicted regime")
    require(isinstance(p.get("feature_config_ref"), str) and bool(p["feature_config_ref"]), "Feature configuration reference required")
    require(type(p.get("state_max_age_ms")) is int and p["state_max_age_ms"] > 0, "Finite account/position age limit required")
    e, h, lim = p["entry"], p["hold_exit"], p["execution_limits"]
    require(e["max_observation_gap_ms"] > 0, "Observation gap must be positive")
    require(e["min_observations"] <= 1_000_000, "Observation count exceeds representation bound")
    slope = e.get("slope_min_per_second")
    require(slope is None or (type(slope) in (int, float) and math.isfinite(slope) and slope >= 0), "Slope filter must be null or nonnegative")
    for direction, entry_key, hold_key in (("LONG", "obi_long", "long_hold_obi"), ("SHORT", "obi_short", "short_hold_obi")):
        if direction in p["allowed_position_directions"]:
            require(e[entry_key] is not None and h[hold_key] is not None, "Allowed direction requires entry and hold thresholds")
    require(decimal_value(h["exit_target_qty"]) == 0, "Baseline exits target flat")
    require(lim["max_order_notional"] is not None, "Explicit order notional limit required")
    require(lim["max_quote_age_ms"] > 0, "Quote age limit must be positive")
    require(lim["price_reference_mode"] == "AT_INTENT_CREATION", "Baseline only emits current-quote targets; fixed anchors belong to OMS")
    smoothing = p["smoothing"]
    if smoothing["method"] == "TIME_EMA":
        require(smoothing.get("alpha") is None, "TIME_EMA cannot also specify alpha")
    else:
        require(smoothing.get("tau_ms") is None, "EVENT_EMA cannot also specify tau_ms")
