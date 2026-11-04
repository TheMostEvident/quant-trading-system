"""Causal, staged microstructure calculations for contract-denominated books.

OFI is observed between snapshots, not a reconstruction of every exchange event.
No clock, network, broker, random state or future observations are consulted.
"""

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, localcontext
from fractions import Fraction
import math

from quant_system.contracts import validate_message
from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decimal_value, digest, encode, decode

VERSION = "microstructure-v1"
MAX_QUANTITY = 10**12  # Representation bound, not a broker/order risk limit.
DEFAULT_CONFIG = {"feature_version": VERSION, "depth": 5, "weights": ["1"] * 5,
                  "smoothing": {"method": "TIME_EMA", "tau_ms": 500, "alpha": None},
                  "window_ms": 1000, "max_observation_gap_ms": 2000, "max_window_events": 4096}
BLOCKED = {"STALE", "GAP", "OUT_OF_ORDER", "INVALID_BOOK", "CLOCK_UNVERIFIED", "SESSION_UNVERIFIED", "INSTRUMENT_UNVERIFIED"}


def validate_config(value=None):
    config = decode(encode(DEFAULT_CONFIG if value is None else value))
    if not isinstance(config, dict) or set(config) != set(DEFAULT_CONFIG) or config["feature_version"] != VERSION:
        raise ValidationError("Unsupported feature configuration")
    if type(config["depth"]) is not int or not 1 <= config["depth"] <= 5:
        raise ValidationError("Feature depth must be 1..5")
    weights = config["weights"]
    if not isinstance(weights, list) or len(weights) != config["depth"]:
        raise ValidationError("One decimal weight per requested rank required")
    if any(not isinstance(w, str) or len(w) > 64 or decimal_value(w) < 0 for w in weights) or not any(decimal_value(w) > 0 for w in weights):
        raise ValidationError("Weights must be nonnegative with a positive total")
    for key, maximum in (("window_ms", 86_400_000), ("max_observation_gap_ms", 86_400_000), ("max_window_events", 4096)):
        if type(config[key]) is not int or not 1 <= config[key] <= maximum:
            raise ValidationError("Invalid bounded feature parameter: " + key)
    smoothing = config["smoothing"]
    if not isinstance(smoothing, dict) or set(smoothing) != {"method", "tau_ms", "alpha"}:
        raise ValidationError("Explicit smoothing method/parameters required")
    if smoothing["method"] == "TIME_EMA":
        if type(smoothing["tau_ms"]) is not int or not 1 <= smoothing["tau_ms"] <= 86_400_000 or smoothing["alpha"] is not None:
            raise ValidationError("TIME_EMA requires positive tau_ms and null alpha")
    elif smoothing["method"] == "EVENT_EMA":
        if type(smoothing["alpha"]) not in (float, int) or not math.isfinite(smoothing["alpha"]) or not 0 < smoothing["alpha"] <= 1 or smoothing["tau_ms"] is not None:
            raise ValidationError("EVENT_EMA requires 0 < alpha <= 1 and null tau_ms")
    else:
        raise ValidationError("Unknown smoothing method")
    return config


def quantity(raw, *, allow_zero=False):
    value = decimal_value(raw)
    if value != value.to_integral_value() or value < (0 if allow_zero else 1) or value > MAX_QUANTITY:
        raise ValidationError("Expected whole contracts within the feature representation bound")
    return int(value)


def price(raw):
    if not isinstance(raw, str) or len(raw) > 64:
        raise ValidationError("Bounded exact decimal price required")
    return Fraction(decimal_value(raw))


def decimal_string(value):
    value = Fraction(value)
    # Prices, depth weights and their sums have terminating decimal expansions.
    with localcontext() as context:
        context.prec = len(str(abs(value.numerator))) + len(str(value.denominator)) + 4
        return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def weighted_obi(bids, asks, weights):
    """Known absent ranks contribute zero; caller verifies source coverage."""
    weighted = []
    for side in (bids, asks):
        weighted.append(sum((Fraction(decimal_value(w)) * quantity(side[i]["quantity"], allow_zero=True)
                             for i, w in enumerate(weights) if i < len(side)), Fraction(0)))
    denominator = weighted[0] + weighted[1]
    return (None if denominator == 0 else float((weighted[0] - weighted[1]) / denominator), *weighted)


def top1_ofi(previous, current):
    """(bid price, bid qty, ask price, ask qty) -> signed observed contracts."""
    bp0, bq0, ap0, aq0 = previous
    bp, bq, ap, aq = current
    return (bq if bp >= bp0 else 0) - (bq0 if bp <= bp0 else 0) - (aq if ap <= ap0 else 0) + (aq0 if ap >= ap0 else 0)


@dataclass(frozen=True)
class FeatureClock:
    producer_id: str
    epoch: str
    sequence: int
    monotonic_ns: int
    raw_ref: str

    def validate(self):
        if any(not isinstance(v, str) or not v for v in (self.producer_id, self.epoch, self.raw_ref)):
            raise ValidationError("Producer/epoch/raw reference required for feature time")
        if type(self.sequence) is not int or self.sequence < 1 or type(self.monotonic_ns) is not int or self.monotonic_ns < 0:
            raise ValidationError("Invalid producer sequence/monotonic clock")


@dataclass(frozen=True)
class BookState:
    top: tuple
    obi: float
    smooth: float
    monotonic_ns: int


@dataclass(frozen=True)
class FeatureState:
    scope: tuple
    started_ns: int
    clock: FeatureClock | None = None
    book: BookState | None = None
    first_book_ns: int | None = None
    books: int = 0
    ofi: tuple = ()
    trades: tuple = ()
    ofi_missing_until: int = 0
    trade_missing_until: int = 0
    previous_ref: str | None = None
    fingerprint: str | None = None
    cached: bytes | None = None


@dataclass(frozen=True)
class FeatureUpdate:
    key: tuple
    expected: FeatureState | None
    state: FeatureState | None
    features: dict | None
    duplicate: bool = False


class FeatureEngine:
    def __init__(self, config=None, *, config_ref="feature-config", max_gap_ms=None, coverage="TOP_5_CALLBACK_SNAPSHOT", source_depth=5):
        self.config = validate_config(config)
        if not isinstance(config_ref, str) or not config_ref:
            raise ValidationError("Feature config reference required")
        if type(source_depth) is not int or not self.config["depth"] <= source_depth <= 5 or not isinstance(coverage, str) or not coverage:
            raise ValidationError("Verified source depth/coverage required")
        if max_gap_ms is not None and (type(max_gap_ms) is not int or max_gap_ms < 1):
            raise ValidationError("Positive source freshness limit required")
        self.max_gap_ms = min(self.config["max_observation_gap_ms"], max_gap_ms or self.config["max_observation_gap_ms"])
        self.coverage, self.source_depth, self.config_ref = coverage, source_depth, config_ref
        self._states = {}

    def definition(self):
        return {"feature_config": decode(encode(self.config)), "effective_max_gap_ms": self.max_gap_ms, "coverage": self.coverage, "source_depth": self.source_depth}

    def _window(self, events, now, missing_until):
        cutoff = now - self.config["window_ms"] * 1_000_000
        events = tuple(event for event in events if event[0] > cutoff)
        limit = self.config["max_window_events"]
        if len(events) > limit:
            removed = events[:-limit]
            missing_until = max(missing_until, removed[-1][0] + self.config["window_ms"] * 1_000_000)
            events = events[-limit:]
        return events, missing_until if now < missing_until else 0

    def prepare(self, message, clock, specification):
        validate_message(message)
        if message["message_type"] != "MarketSnapshot" or not isinstance(message["instrument_id"], str):
            raise ValidationError("Instrument-specific MarketSnapshot required")
        key = (message["run_id"], message["instrument_id"])
        old = self._states.get(key)
        p = message["payload"]
        blocked = bool(set(message["quality_flags"]) & BLOCKED) or p["market_status"] != "CONTINUOUS"
        if blocked:
            return FeatureUpdate(key, old, None, None)
        if p["update_kind"] == "STATUS":
            return FeatureUpdate(key, old, old, None)
        if clock is None:
            return FeatureUpdate(key, old, None, None)
        if not isinstance(clock, FeatureClock):
            raise ValidationError("Explicit FeatureClock required")
        clock.validate()
        if clock.raw_ref not in message["lineage"]:
            raise ValidationError("Feature clock must refer to the current raw lineage")
        if not isinstance(specification, dict) or specification.get("quantity_unit") != "CONTRACT" or not isinstance(specification.get("price_unit"), str) or not specification["price_unit"]:
            raise ValidationError("CONTRACT and explicit price_unit required; units cannot be guessed")
        fingerprint = digest(encode({"message": dict(message, ready_at=None, payload=dict(p, features=None)), "clock": asdict(clock), "spec": specification}))
        if old and old.previous_ref == message["message_id"]:
            if old.fingerprint != fingerprint:
                raise ValidationError("Feature input ID reused with different data")
            return FeatureUpdate(key, old, old, decode(old.cached), True)
        scope = (clock.producer_id, clock.epoch, p["trading_date"], p["instrument_spec_ref"], specification["quantity_unit"], specification["price_unit"], self.coverage)
        flags = {"OBSERVED_SNAPSHOT_OFI"}
        state = old
        if state is None or state.scope != scope:
            flags.add("FIRST_OBSERVATION" if state is None else "CONTEXT_RESET")
            state = FeatureState(scope, clock.monotonic_ns)
        if state.clock:
            if clock.sequence <= state.clock.sequence or clock.monotonic_ns < state.clock.monotonic_ns:
                raise ValidationError("Feature observations must follow their producer clock/counter")
            max_elapsed = clock.monotonic_ns - state.clock.monotonic_ns
            if p["update_kind"] == "BOOK" and state.book:
                max_elapsed = max(max_elapsed, clock.monotonic_ns - state.book.monotonic_ns)
            if max_elapsed >= self.max_gap_ms * 1_000_000:
                flags.add("GAP_RESET")
                state = FeatureState(scope, clock.monotonic_ns)
        now = clock.monotonic_ns
        basis = [clock.raw_ref] + ([state.previous_ref] if state.previous_ref else [])
        features = {"feature_version": VERSION, "time_basis": "RECEIVE_MONOTONIC", "basis": basis, "config_ref": self.config_ref,
                    "obi": None, "smooth_obi": None, "obi_slope_per_second": None, "ofi_top1_delta": None,
                    "ofi_window_sum": None, "ofi_window_ms": self.config["window_ms"]}
        details = {"kind": p["update_kind"], "quantity_unit": "CONTRACT", "price_unit": specification["price_unit"], "coverage": self.coverage,
                   "depth": self.config["depth"], "weights": list(self.config["weights"]), "clock": asdict(clock), "book_observations": state.books,
                   "elapsed_ns": None, "obi_delta_per_observation": None, "smooth_obi_delta_per_observation": None, "raw_obi_slope_per_second": None,
                   "spread": None, "mid_price": None, "spread_ticks": None, "bid_depth": None, "ask_depth": None,
                   "weighted_bid_depth": None, "weighted_ask_depth": None, "ofi_window_count": None, "ofi_window_complete": None,
                   "trade_window_count": 0, "trade_window_complete": False, "trade_volume": {}, "signed_trade_volume": None, "flags": []}
        ofi, trades = state.ofi, state.trades
        new_book, first_book, books = state.book, state.first_book_ns, state.books
        if p["update_kind"] == "BOOK":
            book = p["book"]
            if book["sync_status"] != "SYNCED":
                return FeatureUpdate(key, old, None, None)
            if book["coverage"] != self.coverage or any(len(book[s]) > self.source_depth or not book[s] for s in ("bids", "asks")):
                raise ValidationError("Book coverage differs from the configured source")
            for side in ("bids", "asks"):
                for level in book[side]:
                    quantity(level["quantity"]); price(level["price"])
            obi, bid_weighted, ask_weighted = weighted_obi(book["bids"], book["asks"], self.config["weights"])
            top = (price(book["bids"][0]["price"]), quantity(book["bids"][0]["quantity"]), price(book["asks"][0]["price"]), quantity(book["asks"][0]["quantity"]))
            spread = top[2] - top[0]
            details.update(spread=decimal_string(spread), mid_price=decimal_string((top[0] + top[2]) / 2),
                           bid_depth=str(sum(quantity(l["quantity"]) for l in book["bids"][:self.config["depth"]])),
                           ask_depth=str(sum(quantity(l["quantity"]) for l in book["asks"][:self.config["depth"]])),
                           weighted_bid_depth=decimal_string(bid_weighted), weighted_ask_depth=decimal_string(ask_weighted))
            if specification.get("tick_size") is not None:
                tick = price(specification["tick_size"])
                if tick <= 0:
                    raise ValidationError("Positive tick size required")
                ticks = spread / tick
                if ticks.denominator == 1:
                    details["spread_ticks"] = ticks.numerator
                else:
                    flags.add("TICK_SIZE_MISMATCH")
            else:
                flags.add("TICK_SIZE_UNAVAILABLE")
            if obi is None:
                flags.add("ZERO_WEIGHTED_DEPTH")
                state = FeatureState(scope, now)
                ofi, trades, new_book, first_book, books = (), (), None, None, 0
            else:
                smooth = obi
                if state.book is None:
                    flags.add("FIRST_BOOK")
                    first_book = now
                else:
                    delta_ns = now - state.book.monotonic_ns
                    details["elapsed_ns"] = delta_ns
                    smoothing = self.config["smoothing"]
                    alpha = (-math.expm1(-delta_ns / (smoothing["tau_ms"] * 1_000_000))
                             if smoothing["method"] == "TIME_EMA" else smoothing["alpha"])
                    smooth = state.book.smooth + alpha * (obi - state.book.smooth)
                    raw_delta, smooth_delta = obi - state.book.obi, smooth - state.book.smooth
                    details.update(obi_delta_per_observation=raw_delta, smooth_obi_delta_per_observation=smooth_delta)
                    if delta_ns:
                        features["obi_slope_per_second"] = smooth_delta * 1_000_000_000 / delta_ns
                        details["raw_obi_slope_per_second"] = raw_delta * 1_000_000_000 / delta_ns
                    else:
                        flags.add("ZERO_ELAPSED_TIME")
                    features["ofi_top1_delta"] = top1_ofi(state.book.top, top)
                    ofi += ((now, features["ofi_top1_delta"]),)
                books += 1
                new_book = BookState(top, obi, smooth, now)
                features.update(obi=obi, smooth_obi=smooth)
        elif p["update_kind"] == "TRADE":
            if p["trade"] is None:
                raise ValidationError("TRADE requires a current trade")
            trades += ((now, quantity(p["trade"]["quantity"]), p["trade"]["aggressor"]),)
        ofi, ofi_missing = self._window(ofi, now, state.ofi_missing_until)
        trades, trade_missing = self._window(trades, now, state.trade_missing_until)
        window_ns = self.config["window_ms"] * 1_000_000
        if p["update_kind"] == "BOOK":
            features["ofi_window_sum"] = sum(item[1] for item in ofi) if ofi and not ofi_missing else None
            details["ofi_window_count"] = len(ofi)
            details["ofi_window_complete"] = first_book is not None and now - first_book >= window_ns and not ofi_missing
        if ofi_missing:
            flags.add("OFI_WINDOW_OVERFLOW")
        if trade_missing:
            flags.add("TRADE_WINDOW_OVERFLOW")
        volumes = {side: sum(item[1] for item in trades if item[2] == side) for side in ("BUY", "SELL", "UNKNOWN")}
        details.update(book_observations=books, trade_window_count=len(trades),
                       trade_window_complete=now - state.started_ns >= window_ns and not trade_missing,
                       trade_volume={side: None if trade_missing else str(value) for side, value in volumes.items()},
                       signed_trade_volume=None if trade_missing or volumes["UNKNOWN"] else str(volumes["BUY"] - volumes["SELL"]), flags=sorted(flags))
        features["details"] = details
        # Freeze the cached result so callers cannot mutate committed state.
        next_state = replace(state, clock=clock, book=new_book, first_book_ns=first_book, books=books, ofi=ofi, trades=trades,
                             ofi_missing_until=ofi_missing, trade_missing_until=trade_missing, previous_ref=message["message_id"],
                             fingerprint=fingerprint, cached=encode(features))
        return FeatureUpdate(key, old, next_state, features)

    def commit(self, update):
        if self._states.get(update.key) is not update.expected:
            raise ValidationError("Stale feature transition; one ordered writer is required")
        if update.state is None:
            self._states.pop(update.key, None)
        else:
            self._states[update.key] = update.state

    def process(self, message, clock, specification):
        update = self.prepare(message, clock, specification)
        self.commit(update)
        return update.features
