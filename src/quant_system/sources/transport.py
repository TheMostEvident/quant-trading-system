"""Bounded GET transport. Authentication is runtime-only and redirects are refused."""

import os
import time
import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decode, now_utc, utc_us


@dataclass(frozen=True)
class Response:
    status: int | None
    body: bytes
    received_at: str
    monotonic_ns: int
    media_type: str = "application/octet-stream"
    retry_after: str | None = None
    error: str | None = None
    representation: str = "HTTP_ENTITY_BYTES"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class HTTPTransport:
    source: str
    synthetic: bool = field(default=False, init=False)
    headers: dict = field(default_factory=dict, repr=False)
    opener: object = field(default=None, repr=False)

    def __post_init__(self):
        if self.source == "NAVER_NEWS":
            client = os.environ.get("NAVER_CLIENT_ID")
            secret = os.environ.get("NAVER_CLIENT_SECRET")
            if not client or not secret or len(client) > 512 or len(secret) > 512 or any(not 33 <= ord(c) <= 126 for c in client + secret):
                raise ValidationError("Set NAVER_CLIENT_ID and NAVER_CLIENT_SECRET in the local runtime environment")
            self.headers = {"X-Naver-Client-Id": client, "X-Naver-Client-Secret": secret}
        if self.opener is None:
            self.opener = urllib.request.build_opener(NoRedirect())

    def fetch(self, request, c):
        from .adapters import config, request as expected_request
        c = config(c)
        if request.source != self.source:
            raise ValidationError("Transport/source mismatch")
        if expected_request(c, request.cursor) != request:
            raise ValidationError("Request does not match the allowlisted provider config")
        headers = {"User-Agent": "QuantTradingSystem-source-collector/1", "Accept-Encoding": "identity"} | self.headers
        req = urllib.request.Request(request.url, headers=headers, method="GET")
        try:
            try:
                response = self.opener.open(req, timeout=c["timeout_seconds"])
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(c["max_response_bytes"] + 1)
                status = response.code
                media = response.headers.get("Content-Type", "application/octet-stream")
                retry_after = response.headers.get("Retry-After")
                encoding = response.headers.get("Content-Encoding", "identity").lower()
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            # Never persist exception strings: proxies/server errors can echo auth.
            return Response(None, b"", now_utc(), time.monotonic_ns(), error="TRANSPORT_ERROR", representation="NO_RESPONSE")
        error, representation = None, "HTTP_ENTITY_BYTES"
        if any(s.encode() in raw for s in self.headers.values() if s):
            raw, error, representation = b"", "SENSITIVE_RESPONSE_OMITTED", "OMITTED_SENSITIVE_RESPONSE"
        elif len(raw) > c["max_response_bytes"]:
            raw, error, representation = raw[:c["max_response_bytes"]], "RESPONSE_TOO_LARGE", "HTTP_ENTITY_PREFIX"
        elif encoding not in ("", "identity"):
            error = "UNSUPPORTED_CONTENT_ENCODING"
        return Response(status, raw, now_utc(), time.monotonic_ns(), media, retry_after, error, representation)


class FixtureTransport:
    synthetic = True

    def __init__(self, path):
        path = Path(path)
        self.base = path.parent.resolve()
        self.rows = decode(path.read_bytes())
        if not isinstance(self.rows, list) or not self.rows:
            raise ValidationError("Fixture requires response rows")
        self.position = 0

    def fetch(self, request, c):
        if self.position >= len(self.rows):
            raise ValidationError("Fixture response list exhausted")
        row = self.rows[self.position]
        self.position += 1
        if row["request"] != request.public():
            raise ValidationError("Fixture request does not match provider URL/cursor")
        path = (self.base / row["body_file"]).resolve()
        if not path.is_relative_to(self.base):
            raise ValidationError("Fixture body must be inside its directory")
        with path.open("rb") as stream:
            raw = stream.read(c["max_response_bytes"] + 1)
        too_large = len(raw) > c["max_response_bytes"]
        at = row["received_at"]; utc_us(at)
        mono = row["monotonic_ns"]
        if type(mono) is not int or mono < 0 or type(row["status"]) is not int:
            raise ValidationError("Invalid fixture clock/status")
        return Response(row["status"], raw[:c["max_response_bytes"]], at, mono, row["media_type"], row.get("retry_after"),
                        "RESPONSE_TOO_LARGE" if too_large else None,
                        "HTTP_ENTITY_PREFIX" if too_large else "SYNTHETIC_HTTP_ENTITY")


def retry_delay(value, minimum):
    """Honor a bounded delta-seconds or HTTP-date Retry-After; no unbounded sleeps."""
    from datetime import UTC, datetime
    from email.utils import parsedate_to_datetime
    if value:
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            try:
                at = parsedate_to_datetime(value)
                seconds = (at - datetime.now(UTC)).total_seconds() if at.tzinfo else minimum
            except (ValueError, TypeError, OverflowError):
                seconds = minimum
        if seconds > 60:
            return None  # stop; do not retry earlier than the server requested
        return max(minimum, seconds)
    return minimum
