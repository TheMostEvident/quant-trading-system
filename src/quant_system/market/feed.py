from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from quant_system.contracts import validate_message
from quant_system.foundation.errors import DataConflict, ValidationError
from quant_system.foundation.models import Capture, Version
from quant_system.foundation.values import decode, digest, encode, now_utc, timestamp, utc_us

from .mapping import BOOK_TYPE, TRADE_TYPE, STATUS_TYPE, DEPTH, GUIDE
from .profile import validate_profile

_NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]\Z")
FRAME_FIELDS = {"frame_version", "producer_id", "epoch", "sequence", "received_at", "monotonic_ns", "data_origin", "event"}
BASE_FLAGS = {"SOURCE_TIME_UNKNOWN", "SOURCE_SEQUENCE_UNAVAILABLE", "SOURCE_EVENT_ID_UNAVAILABLE"}


def number(raw, *, magnitude=False, integral=False):
    if not isinstance(raw, str) or not _NUMBER.fullmatch(raw.strip()):
        raise ValidationError("Missing or non-decimal callback field")
    value = Decimal(raw.strip())
    if magnitude:
        value = value.copy_abs()
    if integral and value != value.to_integral_value():
        raise ValidationError("Future quantities require whole contracts")
    return value


def validate_frame(frame):
    if not isinstance(frame, dict) or set(frame) != FRAME_FIELDS or type(frame["frame_version"]) is not int or frame["frame_version"] != 1:
        raise ValidationError("Invalid callback frame envelope")
    for key in ("producer_id", "epoch"):
        if not isinstance(frame[key], str) or not frame[key]:
            raise ValidationError("Missing callback producer/epoch")
    for key in ("sequence", "monotonic_ns"):
        if type(frame[key]) is not int or frame[key] < (1 if key == "sequence" else 0):
            raise ValidationError("Invalid callback counter/clock")
    utc_us(frame["received_at"])
    if frame["data_origin"] not in ("SYNTHETIC", "OBSERVED") or not isinstance(frame["event"], dict):
        raise ValidationError("Invalid callback origin/event")
    event = frame["event"]
    kind = event.get("kind")
    fields = {"REAL": {"kind", "code", "real_type", "fields", "raw_real_data"},
              "CONNECTION": {"kind", "connected", "server", "error_code"},
              "HEARTBEAT": {"kind", "connected"}, "GAP": {"kind", "reason"}}
    if kind not in fields or set(event) != fields[kind]:
        raise ValidationError("Invalid callback event fields")
    if kind == "REAL":
        if not all(isinstance(event[k], str) for k in ("code", "real_type", "raw_real_data")) or not isinstance(event["fields"], dict):
            raise ValidationError("Invalid raw real-time callback")
        if any(not isinstance(k, str) or not k.isascii() or not k.isdigit() or not isinstance(v, str) for k, v in event["fields"].items()):
            raise ValidationError("FID keys and original string values required")
    elif kind in ("CONNECTION", "HEARTBEAT"):
        if type(event["connected"]) is not bool:
            raise ValidationError("Connection state must be explicit")
        if kind == "CONNECTION" and (event["server"] not in ("MOCK", "UNKNOWN") or type(event["error_code"]) is not int):
            raise ValidationError("Only the mock collection path is enabled")
    elif not isinstance(event["reason"], str) or not event["reason"]:
        raise ValidationError("Gap requires a reason")
    return frame


@dataclass
class InstrumentState:
    binding: dict
    spec_ref: str
    status: str = "UNKNOWN"
    status_ref: str | None = None
    valid_book: bool = False
    book_ref: str | None = None
    book_received_us: int | None = None
    revision: int = 0
    stale_reported: bool = False


class MarketFeed:
    """One owning core writer, local callback spool input, no order capability.

    A restart uses a new run and requires fresh connection/status/full books.
    Source counters/clocks are in the raw frame; Capture's producer clock is the
    independent core ingest clock so malformed/reordered input can be preserved.
    """

    def __init__(self, store, run_id, profile, *, consumer="market", mode=None):
        self.store, self.run_id, self.consumer = store, run_id, consumer
        self.profile = validate_profile(profile)
        if store.connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
            raise ValidationError("A new feed process requires a new run; old state cannot silently resume")
        origin = self.profile["data_origin"]
        mode = mode or ("SYNTHETIC_REPLAY" if origin == "SYNTHETIC" else "SHADOW")
        store.register_run(run_id, mode=mode, data_origin=origin, config={"market_profile": self.profile}, code_version="market-v1")
        self.profile_ref = run_id + ":market-profile:" + digest(encode(self.profile))
        self.ingest_epoch = str(uuid.uuid4())
        self.ingest_count = 0
        self.progress = {}
        self.active_producer = None
        self.connected = False
        self.clock_fault = False
        self.transport_fault = False
        self.last_heartbeat_us = None
        self.last_source_times = {}
        self.states = {}
        self.initialized = False
        self.last_ready_us = None
        self.last_health = False
        self.live_timing = False
        self.metrics = {"raw_frames": 0, "messages": 0, "quarantined": 0, "duplicates": 0, "ignored": 0,
                        "gaps": 0, "max_normalization_ms": 0.0, "max_transport_age_ms": 0.0}

    def _initialize(self, at):
        if self.initialized:
            return
        self.store.put_artifact(self.profile_ref, self.run_id, {"profile": self.profile, "guide": GUIDE}, ready_at=at)
        for binding in self.profile["instruments"]:
            ref = self.run_id + ":spec:" + digest(encode(binding))
            self.store.put_artifact(ref, self.run_id, binding, ready_at=at)
            self.states[binding["source_code"]] = InstrumentState(binding, ref)
        self.store.set_health(self.run_id, False, "AWAITING_CONNECTION_STATUS_AND_FULL_BOOK", at=at)
        self.initialized = True

    def _audit(self, kind, at, data):
        with self.store.transaction():
            self.store._journal(self.run_id, kind, at, data)

    def _reset(self):
        self.connected = False
        self.last_source_times.clear()
        for state in self.states.values():
            state.status = "UNKNOWN"
            state.status_ref = None
            state.valid_book = False

    def _healthy(self, at):
        us = utc_us(at)
        return self.connected and not self.clock_fault and not self.transport_fault and all(
            s.valid_book and s.status == "CONTINUOUS" and s.binding["spec_verified"] and self.profile["session_mapping_verified"]
            and s.book_received_us is not None and 0 <= us - s.book_received_us < self.profile["stale_after_ms"] * 1000
            for s in self.states.values())

    def _health(self, at):
        healthy = self._healthy(at)
        if healthy != self.last_health:
            evidence = {"book_refs": [s.book_ref for s in self.states.values()], "status_refs": [s.status_ref for s in self.states.values()],
                        "recovery_rule": "current connection + verified session + next complete locally observed top-five snapshot for every instrument"}
            self.store.set_health(self.run_id, healthy, "FEED_RESYNCHRONIZED" if healthy else "FEED_NOT_USABLE", at=at,
                                  recovery_evidence=evidence if healthy else None)
            self.last_health = healthy

    def _emit(self, state, capture, ready_at, kind, *, flags=(), book=None, trade=None, replaces_book=False):
        if self.live_timing:
            ready_at = timestamp(max(utc_us(now_utc()), utc_us(ready_at), self.last_ready_us or utc_us(ready_at)))
        self.last_ready_us = max(self.last_ready_us or utc_us(ready_at), utc_us(ready_at))
        state.revision += 1
        message_id = capture.capture_id + ":m:" + digest(state.binding["instrument_id"].encode())[:16] + ":" + str(state.revision)
        quality = BASE_FLAGS | set(flags)
        status = state.status
        if not self.profile["session_mapping_verified"]:
            quality.add("SESSION_UNVERIFIED"); status = "UNKNOWN"
        if not state.binding["spec_verified"]:
            quality.add("INSTRUMENT_UNVERIFIED"); status = "UNKNOWN"
        if not self.connected:
            quality.add("GAP"); status = "UNKNOWN"
        if self.clock_fault:
            quality.add("CLOCK_UNVERIFIED")
        if self.transport_fault:
            quality.add("GAP")
        if kind in ("BOOK", "TRADE") and utc_us(ready_at) - utc_us(capture.received_at) >= min(self.profile["stale_after_ms"], self.profile["max_transport_delay_ms"]) * 1000:
            quality.add("STALE")
            if kind == "BOOK":
                state.valid_book = False
                book["sync_status"] = "UNSYNCED"
        lineage = [capture.capture_id, self.profile_ref]
        if state.status_ref and state.status_ref not in lineage:
            lineage.append(state.status_ref)
        message = {"schema_version": "0.2.0", "message_type": "MarketSnapshot", "message_id": message_id, "run_id": self.run_id,
                   "instrument_id": state.binding["instrument_id"], "source": "kiwoom-openapi-plus", "source_event_id": None,
                   "event_at": None, "received_at": capture.received_at, "ready_at": ready_at, "data_origin": self.profile["data_origin"],
                   "quality_flags": sorted(quality), "lineage": lineage,
                   "payload": {"instrument_spec_ref": state.spec_ref, "trading_date": state.binding["trading_date"], "update_kind": kind,
                               "market_status": status, "book": book, "trade": trade, "features": None}}
        validate_message(message)
        key = "book:" + state.binding["instrument_id"] if kind == "BOOK" or replaces_book else kind.lower() + ":" + state.binding["instrument_id"]
        expiry = None
        if kind == "BOOK" and state.valid_book:
            end = state.book_received_us + self.profile["stale_after_ms"] * 1000
            if end > utc_us(ready_at):
                expiry = timestamp(end)
        self.store.publish(capture.capture_id, message, [self.consumer], version=Version(key, state.revision, ready_at, expiry), normalization_version="kiwoom-futures-v1")
        self.metrics["messages"] += 1
        return message

    def _invalidate(self, capture, at, flags, *, reset_status=False):
        results = []
        for state in self.states.values():
            state.valid_book = False
            if reset_status:
                state.status = "UNKNOWN"; state.status_ref = None
            results.append(self._emit(state, capture, at, "STATUS", flags=flags, replaces_book=True))
        return results

    def process(self, raw, *, ready_at=None, framing_error=False):
        started = time.perf_counter_ns()
        self.live_timing = ready_at is None
        ready_at = ready_at or now_utc()
        observed_us = utc_us(ready_at)
        # Readiness is processing completion order, never sorted source time.
        if self.last_ready_us is not None and observed_us < self.last_ready_us:
            self.clock_fault = True
        if not isinstance(raw, bytes) or len(raw) > self.profile["max_frame_bytes"]:
            self.store.storage_healthy = False
            raise ValidationError("Callback frame exceeds configured byte limit")
        self._initialize(ready_at)
        frame, parse_error = None, None
        try:
            frame = validate_frame(decode(raw))
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            parse_error = str(exc)
        if framing_error:
            parse_error = "Incomplete JSONL frame at EOF"
        self.ingest_count += 1
        identity = [self.run_id, frame["producer_id"], frame["epoch"], frame["sequence"]] if frame else [self.run_id, self.ingest_epoch, self.ingest_count]
        capture_id = self.run_id + ":callback:" + digest(encode(identity))
        existing = self.store.connection.execute("SELECT 1 FROM captures WHERE capture_id=?", (capture_id,)).fetchone()
        collision = False
        if existing:
            if self.store.raw(capture_id) == raw:
                self.metrics["duplicates"] += 1
                return []
            collision = True
            capture_id += ":conflict:" + str(self.ingest_count)
        received = frame["received_at"] if frame else ready_at
        ready_at = timestamp(max(observed_us, utc_us(received), self.last_ready_us or observed_us))
        self.last_ready_us = utc_us(ready_at)
        capture = Capture(capture_id, self.run_id, "kiwoom-core-ingest", self.ingest_epoch, self.ingest_count, "kiwoom-openapi-plus", "callback-spool", raw,
                          received, ready_at, time.monotonic_ns(), representation="OCX_CALLBACK_FIELDS", media_type="application/x-kiwoom-callback+json")
        self.store.record_capture(capture)
        self.metrics["raw_frames"] += 1
        output = []
        try:
            if parse_error or collision:
                raise ValidationError(parse_error or "Callback identity has conflicting content")
            if frame["data_origin"] != self.profile["data_origin"]:
                raise ValidationError("Observed and synthetic callback origins cannot be mixed")
            producer = (frame["producer_id"], frame["epoch"])
            old = self.progress.get(producer)
            gap = False
            if producer != self.active_producer:
                if producer in self.progress:
                    raise ValidationError("Old producer epoch cannot become active again")
                self._reset(); self.clock_fault = False
                self.active_producer = producer
                output += self._invalidate(capture, ready_at, {"GAP"}, reset_status=True)
            if old:
                if frame["sequence"] <= old[0] or frame["monotonic_ns"] < old[1]:
                    self.clock_fault = True
                    raise ValidationError("Producer sequence/monotonic time reversed")
                gap = frame["sequence"] != old[0] + 1
                if utc_us(received) < old[2]:
                    self.clock_fault = True
            elif frame["sequence"] != 1:
                gap = True
            self.progress[producer] = (frame["sequence"], frame["monotonic_ns"], utc_us(received))
            age = observed_us - utc_us(received)
            self.metrics["max_transport_age_ms"] = max(self.metrics["max_transport_age_ms"], max(0, age) / 1000)
            if age < 0:
                self.clock_fault = True
            flags = set()
            if gap:
                self.metrics["gaps"] += 1
                output += self._invalidate(capture, ready_at, {"GAP"})
                flags.add("GAP")
            if age >= self.profile["max_transport_delay_ms"] * 1000:
                flags.add("STALE")
            event = frame["event"]
            kind = event["kind"]
            if kind == "CONNECTION":
                self._reset()
                self.connected = event["connected"] and event["server"] == "MOCK" and event["error_code"] == 0
                self.last_heartbeat_us = utc_us(received)
                output += self._invalidate(capture, ready_at, flags | {"GAP"}, reset_status=True)
            elif kind == "HEARTBEAT":
                self.last_heartbeat_us = utc_us(received)
                if not event["connected"]:
                    self._reset()
                    output += self._invalidate(capture, ready_at, flags | {"GAP"}, reset_status=True)
            elif kind == "GAP":
                output += self._invalidate(capture, ready_at, {"GAP"})
                self._audit("FEED_GAP", ready_at, {"capture_id": capture_id, "reason": event["reason"]})
            elif event["real_type"] == STATUS_TYPE:
                if event["code"] != "":
                    raise ValidationError("Session source scope differs from the configured global callback")
                status = self.profile["status_codes"].get(event["fields"].get("215", "").strip(), "UNKNOWN")
                if flags or self.clock_fault or not self.connected:
                    status = "UNKNOWN"
                for state in self.states.values():
                    if status != state.status:
                        state.valid_book = False
                    state.status = status
                    m = self._emit(state, capture, ready_at, "STATUS", flags=flags)
                    state.status_ref = m["message_id"]; output.append(m)
            elif event["code"] not in self.states or event["real_type"] not in (BOOK_TYPE, TRADE_TYPE):
                self.metrics["ignored"] += 1
                self._audit("FEED_IGNORED", ready_at, {"capture_id": capture_id, "reason": "UNSUBSCRIBED_CODE_OR_TYPE"})
            else:
                state = self.states[event["code"]]
                fields = event["fields"]
                clock_field = "21" if event["real_type"] == BOOK_TYPE else "20"
                source_time = fields.get(clock_field, "").strip()
                if not _TIME.fullmatch(source_time):
                    flags.add("SOURCE_TIME_UNAVAILABLE")
                else:
                    time_key = (event["code"], event["real_type"])
                    if source_time < self.last_source_times.get(time_key, source_time):
                        flags |= {"OUT_OF_ORDER", "CLOCK_UNVERIFIED"}
                        self.clock_fault = True
                    self.last_source_times[time_key] = source_time
                if event["real_type"] == BOOK_TYPE:
                    book = self._book(fields)
                    state.valid_book = self.connected and not flags & {"GAP", "STALE", "OUT_OF_ORDER"} and not self.clock_fault and not self.transport_fault
                    state.book_received_us = utc_us(received)
                    state.stale_reported = False
                    book["sync_status"] = "SYNCED" if state.valid_book else "UNSYNCED"
                    m = self._emit(state, capture, ready_at, "BOOK", flags=flags, book=book)
                    state.book_ref = m["message_id"]; output.append(m)
                else:
                    price = number(fields.get("10"), magnitude=True)
                    quantity = number(fields.get("15"), magnitude=True, integral=True)
                    if price <= 0 or quantity <= 0:
                        raise ValidationError("Nonpositive index-future trade")
                    output.append(self._emit(state, capture, ready_at, "TRADE", flags=flags,
                                             trade={"trade_id": None, "price": str(price), "quantity": str(quantity), "aggressor": "UNKNOWN"}))
        except (ValidationError, DataConflict) as exc:
            self.metrics["quarantined"] += 1
            self._audit("FEED_QUARANTINE", ready_at, {"capture_id": capture_id, "reason": str(exc)})
            output += self._invalidate(capture, ready_at, {"GAP", "INVALID_BOOK"})
        ready_at = timestamp(max(utc_us(ready_at), self.last_ready_us or utc_us(ready_at)))
        output += self._expire(capture, ready_at)
        self._health(timestamp(self.last_ready_us))
        self.metrics["max_normalization_ms"] = max(self.metrics["max_normalization_ms"], (time.perf_counter_ns() - started) / 1_000_000)
        return output

    @staticmethod
    def _book(fields):
        book = {"coverage": "TOP_5_CALLBACK_SNAPSHOT", "sync_status": "UNSYNCED", "source_sequence": None, "bids": [], "asks": []}
        for side, price_start, quantity_start in (("bids", 51, 71), ("asks", 41, 61)):
            empty_seen = False
            for level in range(DEPTH):
                quantity = number(fields.get(str(quantity_start + level)), integral=True)
                if quantity < 0:
                    raise ValidationError("Negative active depth")
                if quantity == 0:
                    empty_seen = True
                    continue
                if empty_seen:
                    raise ValidationError("Depth has a nonempty level after a missing level")
                price = number(fields.get(str(price_start + level)), magnitude=True)
                if price <= 0:
                    raise ValidationError("Nonpositive index-future depth price")
                book[side].append({"price": str(price), "quantity": str(quantity)})
            if not book[side]:
                raise ValidationError("Two nonempty sides required for a usable book")
        return book

    def _expire(self, capture, at):
        output = []
        heartbeat_lost = self.last_heartbeat_us is None or utc_us(at) - self.last_heartbeat_us >= self.profile["heartbeat_timeout_ms"] * 1000
        for state in self.states.values():
            expired = state.book_received_us is not None and utc_us(at) - state.book_received_us >= self.profile["stale_after_ms"] * 1000
            if (heartbeat_lost or expired) and state.book_ref and not state.stale_reported:
                state.valid_book = False; state.stale_reported = True
                output.append(self._emit(state, capture, at, "STATUS", flags={"STALE", "GAP"} if heartbeat_lost else {"STALE"}, replaces_book=True))
        return output

    def tick(self, *, at=None, transport_failed=False):
        at = at or now_utc()
        if self.last_ready_us is not None:
            at = timestamp(max(utc_us(at), self.last_ready_us))
        self.last_ready_us = utc_us(at)
        if not self.initialized:
            self._initialize(at)
        if transport_failed:
            self.transport_fault = True
        # Watchdog is a separate local producer, not a forged source heartbeat.
        self.ingest_count += 1
        raw = encode({"kind": "CORE_WATCHDOG", "at": at, "transport_failed": transport_failed})
        capture = Capture(self.run_id + ":watchdog:" + str(self.ingest_count), self.run_id, "kiwoom-core-ingest", self.ingest_epoch,
                          self.ingest_count, "core-watchdog", "health", raw, at, at, time.monotonic_ns())
        self.store.record_capture(capture)
        results = self._invalidate(capture, at, {"GAP"}) if transport_failed else []
        results += self._expire(capture, at)
        self._health(at)
        return results
