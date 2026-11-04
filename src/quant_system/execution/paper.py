"""Deterministic synthetic futures matching; no broker transport or queue claims."""

from copy import deepcopy
from fractions import Fraction

from quant_system.contracts import validate_message
from quant_system.contracts.quality import execution_book_issues
from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import digest, encode, timestamp, utc_us
from quant_system.strategy.engine import clock as checked_clock

from .ledger import amount, text, validate_config


MODEL = "synthetic-depth-highwater-v1"


def assumptions(value, ledger):
    value = deepcopy(value)
    fields = {"model", "data_origin", "order_latency_ms", "cancel_latency_ms", "report_latency_ms",
              "max_quote_age_ms", "participation", "slippage_ticks", "max_contracts_per_book", "costs", "contract_expiry", "evidence"}
    if not isinstance(value, dict) or set(value) != fields or value["model"] != MODEL or value["data_origin"] != "SYNTHETIC":
        raise ValidationError("Explicit synthetic paper assumptions required")
    for name in ("order_latency_ms", "cancel_latency_ms", "report_latency_ms", "max_quote_age_ms", "slippage_ticks", "max_contracts_per_book"):
        minimum = 0 if name in ("report_latency_ms", "slippage_ticks") else 1
        if type(value[name]) is not int or not minimum <= value[name] <= 1000000:
            raise ValidationError("Bounded integer paper parameter required: " + name)
    if not 0 < amount(value["participation"]) <= 1 or not isinstance(value["evidence"], str) or not value["evidence"].strip():
        raise ValidationError("Participation and explicit assumption evidence required")
    if ledger["ledger_model"] != "FUTURES_MARGIN" or not isinstance(value["costs"], dict) or set(value["costs"]) != set(ledger["instruments"]):
        raise ValidationError("First paper model requires explicitly configured linear futures")
    if not isinstance(value["contract_expiry"], dict) or set(value["contract_expiry"]) != set(ledger["instruments"]):
        raise ValidationError("Explicit per-contract synthetic expiry required")
    for expiry in value["contract_expiry"].values():
        if utc_us(expiry) <= utc_us(ledger["ready_at"]):
            raise ValidationError("Synthetic contract must initially be unexpired")
    for instrument, cost in value["costs"].items():
        if set(cost) != {"asset", "per_contract", "notional_rate", "tax_rate"} or cost["asset"] != ledger["instruments"][instrument]["pnl_asset"]:
            raise ValidationError("Costs must use the instrument collateral asset")
        if any(amount(cost[name]) < 0 for name in ("per_contract", "notional_rate", "tax_rate")):
            raise ValidationError("Nonnegative synthetic fee/tax assumptions required")
    return value


class PaperBroker:
    def __init__(self, ledger_config, parameters):
        self.config = validate_config(ledger_config)
        self.parameters = assumptions(parameters, self.config)
        self.orders = {}
        self.commands = {}
        self.arrivals = []
        self.pending_reports = []
        self.report_log = []
        self.liquidity = {}
        self.book_ids = {}
        self.latest_received = {}
        self.last = None
        self.serial = self.fill_sequence = self.report_sequence = 0
        self.last_book_result = None

    def _time(self, at, clock):
        now = (utc_us(at), checked_clock(clock))
        if self.last is not None:
            old_at, old_clock = self.last
            if now[1][:2] != old_clock[:2] or now[0] < old_at or now[1][2] < old_clock[2]:
                raise ValidationError("Paper clock regressed or changed domain")
            if (now[0] - old_at) * 1000 != now[1][2] - old_clock[2]:
                raise ValidationError("Synthetic UTC and monotonic clock deltas must agree")
        self.last = now

    @staticmethod
    def _later(at, clock, milliseconds):
        return timestamp(utc_us(at) + milliseconds * 1000), dict(clock, monotonic_ns=clock["monotonic_ns"] + milliseconds * 1000000)

    def submit(self, command, *, at, clock):
        """Accept only an already committed OMS transport claim."""
        self._time(at, clock)
        if command.get("status") != "SENDING" or command.get("dispatched_at") != at or command.get("kind") not in ("NEW", "CANCEL"):
            raise ValidationError("Paper transport requires a committed command claim")
        identity = command["command_id"]
        semantic = digest(encode(command))
        if identity in self.commands:
            if self.commands[identity] != semantic:
                raise ValidationError("Paper command identity conflict")
            return False
        order = command["payload"]
        if order["order_id"] != command["order_id"] or order["instrument_id"] not in self.config["instruments"]:
            raise ValidationError("Paper command/order binding mismatch")
        if command["kind"] == "NEW":
            if order["order_id"] in self.orders:
                raise ValidationError("Duplicate new order identity")
            spec = self.config["instruments"][order["instrument_id"]]
            qty, price = amount(order["quantity"]), amount(order["limit_price"])
            if qty <= 0 or qty.denominator != 1 or qty % amount(spec["qty_step"]) or price % amount(spec["tick_size"]) or order["side"] not in ("BUY", "SELL"):
                raise ValidationError("Invalid synthetic futures order")
            self.orders[order["order_id"]] = dict(deepcopy(order), filled="0", active=False, canceled=False, expired=False,
                                                  claim_at=at, claim_clock=deepcopy(clock), serial=self.serial)
            expiry = self.parameters["contract_expiry"][order["instrument_id"]]
            if utc_us(expiry) > utc_us(at):
                expiry_clock = dict(clock, monotonic_ns=clock["monotonic_ns"] + (utc_us(expiry) - utc_us(at)) * 1000)
                self.arrivals.append({"at": expiry, "clock": expiry_clock, "kind": "EXPIRE", "order_id": order["order_id"],
                                      "command_id": identity, "serial": -1})
        elif order["order_id"] not in self.orders:
            raise ValidationError("Cancellation has no known transmitted order")
        self.commands[identity] = semantic
        delay = self.parameters["order_latency_ms" if command["kind"] == "NEW" else "cancel_latency_ms"]
        arrival_at, arrival_clock = self._later(at, clock, delay)
        self.arrivals.append({"at": arrival_at, "clock": arrival_clock, "kind": command["kind"],
                              "order_id": order["order_id"], "command_id": identity, "serial": self.serial})
        self.serial += 1
        return True

    def _report(self, order, kind, at, clock, *, fill=None, refs=()):
        self.report_sequence += 1
        ready_at, ready_clock = self._later(at, clock, self.parameters["report_latency_ms"])
        remaining = amount(order["quantity"]) - amount(order["filled"])
        status = ("FILLED" if remaining == 0 else "REJECTED" if kind == "REJECT" else "EXPIRED" if order["expired"]
                  else "CANCELED" if order["canceled"] else "PARTIALLY_FILLED" if amount(order["filled"]) else "OPEN")
        payload = {"account_id": self.config["account_id"], "client_order_id": order["order_id"],
                   "broker_order_id": "paper:" + order["order_id"], "event_type": kind, "order_status": status,
                   "side": order["side"], "position_effect": order["position_effect"], "order_qty": order["quantity"],
                   "cum_filled_qty": order["filled"], "leaves_qty": "0" if kind in ("CANCEL_ACK", "REJECT", "EXPIRE") or order["expired"] else text(remaining),
                   "last_fill": fill, "correction_of": None, "reason": None}
        message = {"schema_version": "0.2.0", "message_type": "ExecutionReport", "message_id": self.config["run_id"] + ":paper-report:" + str(self.report_sequence),
                   "run_id": self.config["run_id"], "instrument_id": order["instrument_id"], "source": MODEL, "source_event_id": None,
                   "event_at": at, "received_at": ready_at, "ready_at": ready_at, "data_origin": "SYNTHETIC",
                   "quality_flags": [], "lineage": list(refs), "payload": payload}
        validate_message(message)
        record = {"at": ready_at, "clock": ready_clock, "report": message, "sequence": self.report_sequence}
        self.pending_reports.append(record)
        self.report_log.append(deepcopy(record))

    def _deliver(self, at):
        ready = sorted((x for x in self.pending_reports if utc_us(x["at"]) <= utc_us(at)), key=lambda x: (utc_us(x["at"]), x["sequence"]))
        self.pending_reports = [x for x in self.pending_reports if utc_us(x["at"]) > utc_us(at)]
        return ready

    def advance(self, *, at, clock):
        self._time(at, clock)
        due = sorted((x for x in self.arrivals if utc_us(x["at"]) <= utc_us(at)), key=lambda x: (utc_us(x["at"]), x["serial"]))
        self.arrivals = [x for x in self.arrivals if utc_us(x["at"]) > utc_us(at)]
        for item in due:
            order = self.orders[item["order_id"]]
            if item["kind"] == "NEW":
                expired = utc_us(item["at"]) >= utc_us(self.parameters["contract_expiry"][order["instrument_id"]])
                order.update(active=not expired, expired=expired, arrival_clock=item["clock"], arrival_at=item["at"])
                self._report(order, "REJECT" if expired else "ACK", item["at"], item["clock"], refs=[item["command_id"]])
            elif item["kind"] == "EXPIRE":
                if order["active"] and amount(order["filled"]) < amount(order["quantity"]):
                    order.update(active=False, expired=True)
                    self._report(order, "EXPIRE", item["at"], item["clock"], refs=[item["command_id"]])
            else:
                fully_filled = amount(order["filled"]) == amount(order["quantity"])
                terminal = fully_filled or order["expired"] or order["canceled"]
                order.update(active=False, canceled=order["canceled"] or not terminal)
                self._report(order, "CANCEL_REJECT" if terminal else "CANCEL_ACK", item["at"], item["clock"], refs=[item["command_id"]])
        return self._deliver(at)

    def on_book(self, message, *, clock):
        validate_message(message)
        if message["message_type"] != "MarketSnapshot" or message["run_id"] != self.config["run_id"] or message["data_origin"] != "SYNTHETIC" or message["instrument_id"] not in self.config["instruments"]:
            raise ValidationError("Paper accepts only scoped synthetic market inputs")
        at = message["ready_at"]
        output = self.advance(at=at, clock=clock)
        key, semantic = message["message_id"], digest(encode(message))
        if key in self.book_ids:
            if self.book_ids[key] != semantic:
                raise ValidationError("Market message identity conflict")
            self.last_book_result = "DUPLICATE_OBSERVATION"
            return output
        self.book_ids[key] = semantic
        instrument, payload = message["instrument_id"], message["payload"]
        spec = self.config["instruments"][instrument]
        received = utc_us(message["received_at"])
        if received > utc_us(at) or received < self.latest_received.get(instrument, received):
            self.last_book_result = "OBSERVATION_TIME_INVALID"
            return output
        self.latest_received[instrument] = received
        book = payload["book"]
        features = payload.get("features")
        observation_ns = clock["monotonic_ns"]
        if features:
            source_clock = features["details"]["clock"]
            if (source_clock["producer_id"], source_clock["epoch"]) != (clock["producer_id"], clock["epoch"]) or source_clock["monotonic_ns"] > clock["monotonic_ns"]:
                self.last_book_result = "SOURCE_CLOCK_UNMAPPED"
                return output
            observation_ns = source_clock["monotonic_ns"]
        if execution_book_issues(message) or payload["update_kind"] != "BOOK" or payload["market_status"] != "CONTINUOUS" or not book or book["sync_status"] != "SYNCED":
            self.last_book_result = "UNUSABLE_BOOK"
            return output
        if utc_us(at) - received >= self.parameters["max_quote_age_ms"] * 1000:
            self.last_book_result = "STALE_BOOK"
            return output
        if payload["instrument_spec_ref"] != spec["instrument_spec_ref"]:
            raise ValidationError("Market specification differs from the order instrument")
        tick, step = amount(spec["tick_size"]), amount(spec["qty_step"])
        levels = {}
        for side in ("bids", "asks"):
            rows = [(amount(row["price"]), amount(row["quantity"])) for row in book[side]]
            if not 1 <= len(rows) <= 5 or any(qty < 0 or qty.denominator != 1 or qty % step or price % tick for price, qty in rows):
                raise ValidationError("Book violates futures tick/quantity/depth rules")
            prices = [price for price, _ in rows]
            if prices != sorted(set(prices), reverse=side == "bids"):
                raise ValidationError("Book levels must be unique and price ordered")
            levels[side] = rows
        if levels["bids"][0][0] >= levels["asks"][0][0]:
            raise ValidationError("Crossed or locked book")
        available = {}
        for side, rows in levels.items():
            for price, qty in rows:
                liquidity_key = encode([instrument, side, text(price)]).decode()
                cap = (qty * amount(self.parameters["participation"]) // step) * step
                limit = self.liquidity.setdefault(liquidity_key, {"high": "0", "used": "0"})
                limit["high"] = text(max(amount(limit["high"]), cap))
                available[liquidity_key] = min(cap, amount(limit["high"]) - amount(limit["used"]))
        budget = Fraction(self.parameters["max_contracts_per_book"])
        for order in sorted(self.orders.values(), key=lambda x: x["serial"]):
            if not order["active"] or order["instrument_id"] != instrument or amount(order["filled"]) == amount(order["quantity"]):
                continue
            # A delayed/cached pre-command observation can never become a fill.
            if received <= utc_us(order["claim_at"]) or received < utc_us(order["arrival_at"]) or observation_ns <= order["claim_clock"]["monotonic_ns"] or observation_ns < order["arrival_clock"]["monotonic_ns"]:
                continue
            side = "asks" if order["side"] == "BUY" else "bids"
            for price, _ in levels[side]:
                execution = price + (1 if side == "asks" else -1) * self.parameters["slippage_ticks"] * tick
                outside_limit = execution > amount(order["limit_price"]) if side == "asks" else execution < amount(order["limit_price"])
                if outside_limit:
                    continue
                liquidity_key = encode([instrument, side, text(price)]).decode()
                qty = min(amount(order["quantity"]) - amount(order["filled"]), available[liquidity_key], budget)
                qty = (qty // step) * step
                if qty <= 0:
                    continue
                available[liquidity_key] -= qty
                limit = self.liquidity[liquidity_key]
                limit["used"] = text(amount(limit["used"]) + qty)
                budget -= qty
                order["filled"] = text(amount(order["filled"]) + qty)
                cost = self.parameters["costs"][instrument]
                notional = qty * abs(execution) * amount(spec["contract_multiplier"])
                charges = [{"kind": "FEE", "asset": cost["asset"], "amount": text(qty * amount(cost["per_contract"]) + notional * amount(cost["notional_rate"]))},
                           {"kind": "TAX", "asset": cost["asset"], "amount": text(notional * amount(cost["tax_rate"]))}]
                self.fill_sequence += 1
                fill = {"fill_id": "paper-fill-" + str(self.fill_sequence), "quantity": text(qty), "price": text(execution), "charges": charges,
                        "clock": deepcopy(clock), "executed_at": at, "execution_sequence": self.fill_sequence}
                self._report(order, "FILL", at, clock, fill=fill, refs=[key])
        self.last_book_result = "PROCESSED_WITH_CONSERVATIVE_LIQUIDITY"
        return output + self._deliver(at)

    def snapshot(self):
        return deepcopy({"model": MODEL, "orders": self.orders, "commands": self.commands, "arrivals": self.arrivals,
                         "pending_reports": self.pending_reports, "reports": self.report_log, "liquidity": self.liquidity,
                         "book_ids": self.book_ids, "last": self.last, "last_book_result": self.last_book_result})
