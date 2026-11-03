from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ValidationError

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")


def utc_us(value: str) -> int:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise ValidationError("Expected RFC3339 UTC with six fractional digits")
    try:
        delta = datetime.fromisoformat(value) - _EPOCH
    except ValueError as exc:
        raise ValidationError("Invalid UTC timestamp") from exc
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def timestamp(us: int) -> str:
    return (_EPOCH + timedelta(microseconds=us)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def decimal_value(value: str, *, minimum: Decimal | None = None) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValidationError("Money and quantities require finite, plain decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError("Invalid decimal") from exc
    if minimum is not None and result < minimum:
        raise ValidationError("Decimal below its allowed minimum")
    return result


def encode(value: Any) -> bytes:
    """One versioned Python encoding; not a cross-language canonical JSON claim."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValidationError("Value is not finite UTF-8 JSON") from exc


def decode(raw: bytes | str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValidationError("Non-finite JSON number")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValidationError("Invalid JSON") from exc


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
