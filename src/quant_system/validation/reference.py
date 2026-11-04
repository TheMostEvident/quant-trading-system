"""Independent economic replay. No execution engine, ledger or matcher imports."""

from collections import defaultdict
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
import re

from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.replay import ReplaySession
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode, utc_us
from quant_system.market.latency import LatencyHistogram


def quantity(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValidationError("Reference replay requires bounded decimal strings")
    return Fraction(value)


def display(value):
    with localcontext() as ctx:
        ctx.prec = 100
        return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def economic_replay(config, records):
    """FIFO economic definition implemented separately from production code."""
    cash = {asset: quantity(value) for asset, value in config["balances"].items()}
    lots = {key: [] for key in config["instruments"]}
    marks = {key: None for key in lots}
    charges, turnover = defaultdict(Fraction), defaultdict(Fraction)

    def post(asset, amount):
        if amount:
            cash[asset] = cash.get(asset, Fraction(0)) + amount

    def net(key):
        return sum((lot[0] for lot in lots[key]), Fraction(0))

    for record in sorted(records, key=lambda row: (utc_us(row["at"]), {"FILL": 0, "MARK": 1, "SETTLE": 2}[row["kind"]], row["sequence"])):
        instrument = record["instrument_id"]
        spec = config["instruments"][instrument]
        future = spec["asset_class"] == "FUTURE"
        price = quantity(record["price"])
        if record["kind"] == "MARK":
            marks[instrument] = price
            continue
        if record["kind"] == "SETTLE":
            if not future:
                raise ValidationError("Reference cash inventory has no futures settlement")
            post(spec["pnl_asset"], sum((size * (price - basis) * quantity(spec["contract_multiplier"]) for size, basis in lots[instrument]), Fraction(0)))
            lots[instrument] = [[size, price] for size, _ in lots[instrument]]
            marks[instrument] = price
            continue
        size = quantity(record["quantity"])
        if size <= 0 or record["side"] not in ("BUY", "SELL"):
            raise ValidationError("Invalid independent fill")
        signed = size if record["side"] == "BUY" else -size
        if not future and net(instrument) + signed < 0:
            raise IntegrityFailure("Independent spot inventory would be negative")
        remaining, realized = signed, Fraction(0)
        while remaining and lots[instrument] and lots[instrument][0][0] * remaining < 0:
            old_size, basis = lots[instrument][0]
            matched = min(abs(old_size), abs(remaining))
            direction = 1 if old_size > 0 else -1
            realized += direction * matched * (price - basis) * quantity(spec["contract_multiplier"])
            old_size -= direction * matched
            remaining += direction * matched
            if old_size:
                lots[instrument][0][0] = old_size
            else:
                lots[instrument].pop(0)
        if remaining:
            lots[instrument].append([remaining, price])
        if future:
            post(spec["pnl_asset"], realized)
        else:
            post(spec["quote_asset"], -signed * price)
            post(spec["base_asset"], signed)
        for charge in record["charges"]:
            fee, asset = quantity(charge["amount"]), charge["asset"]
            post(asset, -fee)
            charges[asset] += fee
            if not future and asset == spec["base_asset"]:
                if fee > net(instrument):
                    raise IntegrityFailure("Independent base fee exceeds inventory")
                if fee < 0:
                    lots[instrument].append([-fee, Fraction(0)])
                while fee > 0:
                    taken = min(lots[instrument][0][0], fee)
                    lots[instrument][0][0] -= taken; fee -= taken
                    if not lots[instrument][0][0]:
                        lots[instrument].pop(0)
        turnover[spec["pnl_asset"]] += size * abs(price) * quantity(spec["contract_multiplier"])
        marks[instrument] = price
    pnl = Fraction(0)
    initial = maintenance = Fraction(0)
    for instrument, spec in config["instruments"].items():
        if spec["asset_class"] == "FUTURE":
            if marks[instrument] is not None:
                pnl += sum((size * (marks[instrument] - basis) * quantity(spec["contract_multiplier"]) for size, basis in lots[instrument]), Fraction(0))
            initial += abs(net(instrument)) * quantity(spec["initial_per_contract"])
            maintenance += abs(net(instrument)) * quantity(spec["maintenance_per_contract"])
        elif marks[instrument] is not None:
            pnl += net(instrument) * marks[instrument]
    equity = cash.get(config["valuation_asset"], Fraction(0)) + pnl
    return {"balances": {k: display(v) for k, v in cash.items()}, "positions": {k: display(net(k)) for k in lots},
            "equity": display(equity), "net_pnl_valuation_asset": display(equity - quantity(config["balances"][config["valuation_asset"]])),
            "initial_used": display(initial),
            "maintenance_required": display(maintenance), "charges_by_asset": {k: display(v) for k, v in charges.items()},
            "traded_notional_by_asset": {k: display(v) for k, v in turnover.items()},
            "turnover_ratio_by_asset": {k: display(v / quantity(config["balances"][k])) if k in config["balances"] and quantity(config["balances"][k]) > 0 else None for k, v in turnover.items()}}


def read_json(path):
    with Path(path).open("rb") as source:
        raw = source.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise ValidationError("Bounded audit artifact required")
    return decode(raw)


def equal_money(left, right):
    return set(left) == set(right) and all(quantity(left[key]) == quantity(right[key]) for key in left)


def audit_oms(directory):
    root = Path(directory)
    summary, manifest = read_json(root / "summary.json"), read_json(root / "manifest.json")
    checks = {}
    errors = []
    def check(name, passed):
        checks[name] = "PASS" if passed else "FAIL"
    with JournalStore(root / "history.db", read_only=True) as store:
        projections = store.connection.execute("SELECT * FROM oms_projection").fetchall()
        if len(projections) != 1:
            raise ValidationError("Audit one explicit account/run per archive")
        projection = projections[0]
        run_id = projection["run_id"]
        artifact = store.connection.execute("SELECT data FROM artifacts WHERE artifact_id=?", (projection["config_ref"],)).fetchone()
        config = decode(artifact[0]); state = decode(projection["state"])
        check("synthetic_scope", config["data_origin"] == "SYNTHETIC" and store.run(run_id)["mode"] == "SYNTHETIC_REPLAY")
        check("database_integrity", store.verify_integrity())
        check("projection_digest", digest(bytes(projection["state"])) == projection["state_sha"])
        check("summary_state_binding", summary["state_sha256"] == projection["state_sha"])
        latest = store.connection.execute("SELECT state_sha FROM oms_events ORDER BY seq DESC LIMIT 1").fetchone()
        check("projection_history_binding", latest is not None and latest[0] == projection["state_sha"])
        saved = store.connection.execute("SELECT data FROM manifests WHERE manifest_id=?", (manifest["manifest_id"],)).fetchone()
        check("manifest_binding", saved is not None and bytes(saved[0]) == encode(manifest) and summary["manifest_sha256"] == manifest["sha256"] and manifest["payload"]["store_id"] == store.store_id and manifest["payload"]["source_run_id"] == run_id)
        economic, active_fills, claims, targets = [], {}, {}, {}
        history_binding = True
        source_valid = True
        trace = read_json(root / "trace.json")
        check("trace_digest", summary["trace_sha256"] == digest(encode(trace)))
        trace_rows = {}
        for row in trace:
            if "event_id" in row:
                trace_rows.setdefault(row["event_id"], row)
        trace_ok = True

        def accept(message, event):
            nonlocal source_valid
            payload = message["payload"]
            fill = payload["last_fill"]
            key = digest(encode([config["execution_scope"], config["account_id"], payload["broker_order_id"], fill["fill_id"]]))
            record = {"kind": "FILL", "instrument_id": message["instrument_id"], "side": payload["side"], "quantity": fill["quantity"], "price": fill["price"],
                      "charges": fill["charges"], "at": fill["executed_at"], "sequence": fill["execution_sequence"], "order_id": payload["client_order_id"],
                      "fill": fill, "message": message, "key": key}
            if key in active_fills:
                source_valid &= encode({k: v for k, v in record.items() if k != "message"}) == encode({k: v for k, v in active_fills[key].items() if k != "message"})
                return
            order = claims.get(payload["client_order_id"])
            if order is None:
                source_valid = False
            else:
                spec = config["instruments"][message["instrument_id"]]
                price, size = quantity(fill["price"]), quantity(fill["quantity"])
                source_valid &= (utc_us(order["dispatched_at"]) <= utc_us(fill["executed_at"]) <= utc_us(message["ready_at"]) <= utc_us(event["at"]))
                source_valid &= size > 0 and size % quantity(spec["qty_step"]) == 0 and price % quantity(spec["tick_size"]) == 0
                source_valid &= price <= quantity(order["payload"]["limit_price"]) if payload["side"] == "BUY" else price >= quantity(order["payload"]["limit_price"])
                source_valid &= message["data_origin"] == "SYNTHETIC" and message["run_id"] == run_id and payload["account_id"] == config["account_id"]
                source_valid &= payload["side"] == order["payload"]["side"] and message["instrument_id"] == order["payload"]["instrument_id"]
                fc = fill["clock"]; ec = event["clock"]
                source_valid &= (fc["producer_id"], fc["epoch"]) == (ec["producer_id"], ec["epoch"]) and fc["monotonic_ns"] <= ec["monotonic_ns"]
            active_fills[key] = record

        for row in store.connection.execute("SELECT * FROM oms_events ORDER BY seq"):
            event, result = decode(row["input"]), decode(row["result"])
            history_binding &= digest(bytes(row["input"])) == row["input_sha"] and store.raw(row["capture_id"]) == bytes(row["input"])
            kind = event["kind"]
            if kind == "TARGET" and result["status"] == "QUEUED":
                targets[result["order_id"]] = result["quantity"]
            if kind == "DISPATCH" and result["status"] == "CLAIMED" and result["command"]["kind"] == "NEW":
                claims[result["command"]["order_id"]] = dict(result["command"], audit_dispatch_clock=event["clock"])
            if kind == "REPORT" and result["status"] in ("REPORT_APPLIED", "DUPLICATE_FILL") and event["data"]["report"]["payload"]["event_type"] == "FILL":
                accept(event["data"]["report"], event)
            elif kind == "REPORT" and result["status"] == "BUST_RECORDED":
                key = event["data"]["report"]["payload"]["correction_of"]
                if key not in active_fills:
                    source_valid = False
                else:
                    del active_fills[key]
            elif kind == "RECONCILE":
                if result["status"] == "MATCHED":
                    for message in event["data"]["executions"]:
                        accept(message, event)
                elif event["data"].get("executions"):
                    errors.append("Nonmatched reconciliation with economic inputs is outside this audit's accepted scope")
            elif kind in ("MARK", "SETTLE") and result["status"] in ("MARKED", "SETTLED"):
                economic.append({"kind": kind, "instrument_id": event["instrument_id"], "price": event["data"]["price"], "at": event["at"], "sequence": row["seq"]})
            current = economic_replay(config, economic + list(active_fills.values()))
            actual = trace_rows.get(event["event_id"])
            if actual:
                trace_ok &= equal_money(current["balances"], actual["balances"]) and equal_money(current["positions"], actual["positions"])
                trace_ok &= quantity(current["equity"]) == quantity(actual["valuation"]["equity"])
        reference = economic_replay(config, economic + list(active_fills.values()))
        check("input_history_binding", history_binding)
        check("accepted_fill_causality_and_limits", source_valid and not errors)
        check("intermediate_economic_trace", trace_ok)
        check("independent_cash", equal_money(reference["balances"], state["ledger"]["cash"]) and equal_money(reference["balances"], summary["balances"]))
        actual_positions = {key: display(sum((quantity(lot["qty"]) for lot in p["lots"]), Fraction(0))) for key, p in state["ledger"]["positions"].items()}
        check("independent_positions", equal_money(reference["positions"], actual_positions) and equal_money(reference["positions"], summary["positions"]))
        check("independent_equity_and_margin", all(quantity(reference[k]) == quantity(summary["valuation"][k]) for k in ("equity", "initial_used", "maintenance_required")))
        filled = defaultdict(Fraction)
        for fill in active_fills.values():
            filled[fill["order_id"]] += quantity(fill["quantity"])
        reservation_valid = True
        order_margin, fee_reserve = Fraction(0), defaultdict(Fraction)
        for identity, order in state["orders"].items():
            reservation_valid &= identity in targets and quantity(order["quantity"]) == quantity(targets[identity]) and filled[identity] == quantity(order["filled"])
            reservation_valid &= filled[identity] <= quantity(order["quantity"])
            if not order["finalized"]:
                remaining = quantity(order["quantity"]) - filled[identity]
                if remaining and config["ledger_model"] == "FUTURES_MARGIN" and order["position_effect"] == "OPEN":
                    order_margin += remaining * quantity(config["instruments"][order["instrument_id"]]["initial_per_contract"])
                if remaining:
                    fee_reserve[order["fee_reserve"]["asset"]] += quantity(order["fee_reserve"]["amount"])
        check("filled_and_reserved_quantity", reservation_valid)
        if config["ledger_model"] == "FUTURES_MARGIN":
            available = quantity(reference["equity"]) - quantity(reference["initial_used"]) - order_margin - fee_reserve[config["valuation_asset"]]
            check("independent_available_margin", order_margin == quantity(summary["valuation"]["order_reserved"]) and available == quantity(summary["valuation"]["available_margin"]))

        parameter_row = store.connection.execute("SELECT data FROM artifacts WHERE artifact_id=?", (run_id + ":paper-assumptions",)).fetchone()
        model_details = {"scope": "NOT_REQUESTED_NO_PAPER_ASSUMPTIONS"}
        if parameter_row:
            parameters = decode(parameter_row[0])
            books, book_sequences = {}, {}
            for query in ("SELECT data,seq FROM messages", "SELECT a.data,r.seq FROM artifacts a JOIN refs r ON r.ref_id=a.artifact_id"):
                for row in store.connection.execute(query):
                    message = decode(row[0])
                    if isinstance(message, dict) and message.get("message_type") == "MarketSnapshot":
                        books[message["message_id"]] = message
                        book_sequences[message["message_id"]] = row["seq"]
            used, per_book, per_book_level = defaultdict(Fraction), defaultdict(Fraction), defaultdict(Fraction)
            matching_ok = True
            latency = LatencyHistogram()
            allowed_flags = {"SOURCE_TIME_UNKNOWN", "SOURCE_SEQUENCE_UNAVAILABLE", "SOURCE_EVENT_ID_UNAVAILABLE"}
            for fill in sorted(active_fills.values(), key=lambda f: (utc_us(f["at"]), f["sequence"])):
                message, instrument = fill["message"], fill["instrument_id"]
                candidates = [books[ref] for ref in message["lineage"] if ref in books]
                if len(candidates) != 1:
                    matching_ok = False; continue
                book = candidates[0]; payload = book["payload"]; order = claims[fill["order_id"]]
                price, size = quantity(fill["price"]), quantity(fill["quantity"])
                tick = quantity(config["instruments"][instrument]["tick_size"])
                side = "asks" if fill["side"] == "BUY" else "bids"
                level_price = price - (1 if side == "asks" else -1) * parameters["slippage_ticks"] * tick
                key = (instrument, side, level_price)
                matches = [l for l in (payload["book"] or {}).get(side, []) if quantity(l["price"]) == level_price]
                claim = utc_us(order["dispatched_at"]); executed = utc_us(fill["at"])
                matching_ok &= payload["update_kind"] == "BOOK" and payload["market_status"] == "CONTINUOUS" and bool(payload["book"]) and payload["book"]["sync_status"] == "SYNCED"
                matching_ok &= not (set(book["quality_flags"]) - allowed_flags) and book["data_origin"] == "SYNTHETIC"
                matching_ok &= utc_us(book["ready_at"]) == executed and utc_us(book["received_at"]) > claim
                matching_ok &= utc_us(book["received_at"]) >= claim + parameters["order_latency_ms"] * 1000
                matching_ok &= executed - utc_us(book["received_at"]) < parameters["max_quote_age_ms"] * 1000
                matching_ok &= utc_us(message["ready_at"]) == executed + parameters["report_latency_ms"] * 1000
                matching_ok &= executed < utc_us(parameters["contract_expiry"][instrument])
                if payload.get("features"):
                    fc = payload["features"]["details"]["clock"]; cc = order["audit_dispatch_clock"]
                    matching_ok &= (fc["producer_id"], fc["epoch"]) == (cc["producer_id"], cc["epoch"])
                    matching_ok &= fc["monotonic_ns"] > cc["monotonic_ns"] and fc["monotonic_ns"] >= cc["monotonic_ns"] + parameters["order_latency_ms"] * 1000000
                cap = (quantity(matches[0]["quantity"]) * quantity(parameters["participation"]) // quantity(config["instruments"][instrument]["qty_step"])) * quantity(config["instruments"][instrument]["qty_step"]) if matches else Fraction(0)
                high = Fraction(0)
                for observation in books.values():
                    p = observation["payload"]
                    if (observation["instrument_id"] != instrument or utc_us(observation["ready_at"]) > executed
                            or book_sequences[observation["message_id"]] > book_sequences[book["message_id"]]
                            or p["update_kind"] != "BOOK" or p["market_status"] != "CONTINUOUS" or set(observation["quality_flags"]) - allowed_flags):
                        continue
                    for level in (p["book"] or {}).get(side, []):
                        if quantity(level["price"]) == level_price:
                            high = max(high, (quantity(level["quantity"]) * quantity(parameters["participation"]) // quantity(config["instruments"][instrument]["qty_step"])) * quantity(config["instruments"][instrument]["qty_step"]))
                used[key] += size; per_book[book["message_id"]] += size; per_book_level[(book["message_id"], key)] += size
                matching_ok &= used[key] <= high and per_book_level[(book["message_id"], key)] <= cap and per_book[book["message_id"]] <= parameters["max_contracts_per_book"]
                cost = parameters["costs"][instrument]
                notional = size * abs(price) * quantity(config["instruments"][instrument]["contract_multiplier"])
                expected = {"FEE": size * quantity(cost["per_contract"]) + notional * quantity(cost["notional_rate"]), "TAX": notional * quantity(cost["tax_rate"])}
                actual = defaultdict(Fraction)
                for charge in fill["charges"]:
                    matching_ok &= charge["asset"] == cost["asset"]
                    actual[charge["kind"]] += quantity(charge["amount"])
                matching_ok &= dict(actual) == expected
                if executed >= claim:
                    latency.record((executed - claim) / 1000)
                else:
                    matching_ok = False
            check("independent_matching_and_costs", matching_ok)
            model_details = {"scope": "synthetic matching bounds, not an independent exchange simulator", "dispatch_to_fill_latency": latency.summary()}
    replay = ReplaySession(manifest)
    check("manifest_delivery_replay", replay.trace_digest(target_run_id="independent-audit", speed=1) == replay.trace_digest(target_run_id="independent-audit", speed=1000))
    total = defaultdict(Fraction); requested = defaultdict(Fraction)
    for fill in active_fills.values(): total[fill["instrument_id"]] += quantity(fill["quantity"])
    for claim in claims.values(): requested[claim["payload"]["instrument_id"]] += quantity(claim["payload"]["quantity"])
    return {"audit_version": 1, "verified": all(v == "PASS" for v in checks.values()), "checks": checks, "errors": errors,
            "independent_economics": reference, "matching": model_details, "accepted_unique_fills": len(active_fills),
            "fill_ratio_by_instrument": {key: display(total[key] / size) if size else None for key, size in requested.items()},
            "producer_faults": state["faults"], "scope": "independent synthetic economics and declared model bounds; not field acceptance",
            "field_validation": "PENDING_EXTRA_ISSUES", "profitability_claim": False}
