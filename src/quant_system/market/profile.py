from copy import deepcopy
from datetime import date

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import encode


def validate_profile(value):
    profile = deepcopy(value)
    expected = {"profile_version", "data_origin", "instruments", "status_codes", "session_mapping_verified",
                "mapping_evidence", "stale_after_ms", "max_transport_delay_ms", "heartbeat_timeout_ms", "max_frame_bytes"}
    if not isinstance(profile, dict) or set(profile) != expected or type(profile["profile_version"]) is not int or profile["profile_version"] != 1:
        raise ValidationError("Unsupported market profile")
    if profile["data_origin"] not in ("SYNTHETIC", "OBSERVED"):
        raise ValidationError("Declare synthetic or observed source explicitly")
    for key in ("stale_after_ms", "max_transport_delay_ms", "heartbeat_timeout_ms", "max_frame_bytes"):
        if type(profile[key]) is not int or profile[key] < 1:
            raise ValidationError("Positive market limits required: " + key)
    if profile["max_frame_bytes"] > 16 * 1024 * 1024:
        raise ValidationError("Frame limit exceeds raw storage limit")
    if type(profile["session_mapping_verified"]) is not bool or not isinstance(profile["mapping_evidence"], str) or not profile["mapping_evidence"]:
        raise ValidationError("Explicit session verification and evidence required")
    if not isinstance(profile["status_codes"], dict) or any(not isinstance(k, str) or not k or v not in ("CONTINUOUS", "AUCTION", "HALTED", "CLOSED", "UNKNOWN") for k, v in profile["status_codes"].items()):
        raise ValidationError("Invalid session status mapping")
    if not isinstance(profile["instruments"], list) or not 1 <= len(profile["instruments"]) <= 10:
        raise ValidationError("Start with one to ten explicitly selected futures")
    codes, ids = set(), set()
    for item in profile["instruments"]:
        if not isinstance(item, dict) or set(item) != {"source_code", "instrument_id", "trading_date", "specification", "spec_verified", "price_encoding"}:
            raise ValidationError("Invalid instrument binding")
        for key in ("source_code", "instrument_id"):
            if not isinstance(item[key], str) or not item[key] or item[key] != item[key].strip() or any(x in item[key] for x in (";", "\n", "\r")):
                raise ValidationError("Invalid instrument identity")
        if item["source_code"] in codes or item["instrument_id"] in ids:
            raise ValidationError("Duplicate instrument binding")
        codes.add(item["source_code"]); ids.add(item["instrument_id"])
        try:
            date.fromisoformat(item["trading_date"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("Explicit exchange trading date required") from exc
        if type(item["spec_verified"]) is not bool or not isinstance(item["specification"], dict):
            raise ValidationError("Instrument specification and verification required")
        if item["specification"].get("quantity_unit") != "CONTRACT":
            raise ValidationError("This adapter only supports contract-denominated domestic index futures")
        if item["price_encoding"] != "SIGNED_MAGNITUDE":
            raise ValidationError("Only the explicit index-future signed-magnitude price profile is supported")
        if profile["data_origin"] == "OBSERVED" and (item["instrument_id"].startswith("SIM:") or item["source_code"].startswith("SIM")):
            raise ValidationError("Synthetic instruments cannot become observed futures")
    encode(profile)
    return profile
