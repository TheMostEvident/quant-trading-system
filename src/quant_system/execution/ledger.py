"""Exact FIFO lots and per-asset postings for a named synthetic ledger model."""

from decimal import Decimal, localcontext
from fractions import Fraction

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.values import decimal_value, decode, encode, utc_us
from quant_system.market.features import decimal_string

MODEL = "synthetic-fifo-v1"


def amount(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValidationError("Bounded plain decimal required")
    return Fraction(decimal_value(value))


def text(value):
    return decimal_string(value)


def average(value):
    # Display only. All cash/PnL postings use exact lot prices, never this mean.
    with localcontext() as context:
        context.prec = 40
        return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def validate_config(value):
    c = decode(encode(value))
    if c.get("model") != MODEL or c.get("data_origin") != "SYNTHETIC" or c.get("ledger_model") not in ("FUTURES_MARGIN", "CASH"):
        raise ValidationError("Only the explicitly synthetic FIFO accounting model is executable")
    for key in ("run_id", "account_id", "execution_scope", "valuation_asset"):
        if not isinstance(c.get(key), str) or not c[key]:
            raise ValidationError("Ledger scope required")
    if not isinstance(c.get("balances"), dict) or not c["balances"] or c["valuation_asset"] not in c["balances"]:
        raise ValidationError("Initial per-asset balances required")
    for asset, value in c["balances"].items():
        if not asset or amount(value) < 0:
            raise ValidationError("Invalid initial balance")
    if not isinstance(c.get("instruments"), dict) or not c["instruments"]:
        raise ValidationError("Explicit instrument specifications required")
    if amount(c["max_gross_notional"]) <= 0:
        raise ValidationError("Explicit synthetic account gross limit required")
    for instrument, s in c["instruments"].items():
        for key in ("instrument_spec_ref", "quantity_unit", "price_unit", "quote_asset", "pnl_asset"):
            if not isinstance(s.get(key), str) or not s[key]:
                raise ValidationError("Explicit instrument identity/units required")
        if not instrument or s.get("position_mode") != "NET" or amount(s["qty_step"]) <= 0 or amount(s["min_qty"]) <= 0 or amount(s["tick_size"]) <= 0 or amount(s["contract_multiplier"]) <= 0:
            raise ValidationError("Unsupported instrument specification")
        if s["quantity_unit"] in ("CONTRACT", "SHARE") and (amount(s["qty_step"]).denominator != 1 or amount(s["min_qty"]).denominator != 1):
            raise ValidationError("Contract/share steps and minimums must be integral")
        for key in ("can_open_long", "can_open_short"):
            if type(s.get(key)) is not bool:
                raise ValidationError("Verified synthetic capabilities required")
        if c["ledger_model"] == "FUTURES_MARGIN":
            if s["asset_class"] != "FUTURE" or s["quantity_unit"] != "CONTRACT" or s.get("pnl_model") != "LINEAR" or s["pnl_asset"] != c["valuation_asset"]:
                raise ValidationError("Synthetic futures require linear CONTRACT/NET in one collateral asset")
            if amount(s["qty_step"]).denominator != 1 or not s.get("margin_rule_ref") or not 0 <= amount(s["maintenance_per_contract"]) <= amount(s["initial_per_contract"]):
                raise ValidationError("Explicit synthetic margin rules required")
        elif (s["asset_class"], s["quantity_unit"]) not in (("EQUITY", "SHARE"), ("CRYPTO_SPOT", "BASE_ASSET")) or amount(s["contract_multiplier"]) != 1 or s["can_open_short"] or not s.get("base_asset") or s["quote_asset"] != c["valuation_asset"]:
            raise ValidationError("Cash ledger requires inventory-only spot specification")
        if c["ledger_model"] == "FUTURES_MARGIN" and s["margin_rule_ref"] != c.get("margin_rule_ref"):
            raise ValidationError("Synthetic account margin rule must cover every configured instrument")
        if c["ledger_model"] == "CASH" and amount(c["balances"].get(s["base_asset"], "0")):
            raise ValidationError("Initial cash inventory needs an explicit lot seed; this version starts flat")
        if c["ledger_model"] == "CASH" and (c.get("margin_rule_ref") is not None or s.get("margin_rule_ref") is not None or s["pnl_asset"] != s["quote_asset"]):
            raise ValidationError("Cash inventory cannot inherit futures margin or implicit currency conversion")
    if c["ledger_model"] == "CASH" and len({s["base_asset"] for s in c["instruments"].values()}) != len(c["instruments"]):
        raise ValidationError("One cash inventory per base asset is supported")
    if "source_bindings" in c:
        bindings = c["source_bindings"]
        if not isinstance(bindings, dict) or set(bindings) != set(c["instruments"]):
            raise ValidationError("Complete synthetic source specification bindings required")
        for instrument, spec in c["instruments"].items():
            binding = bindings[instrument]
            if (not isinstance(binding, dict) or binding.get("instrument_id") != instrument or not binding.get("source_code")
                    or binding.get("spec_verified") is not True
                    or binding.get("specification") != {k: v for k, v in spec.items() if k != "instrument_spec_ref"}):
                raise ValidationError("Source and ledger specifications must match exactly")
    return c


def empty_ledger(config):
    return {"cash": dict(config["balances"]), "positions": {key: {"lots": [], "mark": None, "charges": [], "first_fill_ref": None, "first_fill_clock": None, "settlement_ref": None} for key in config["instruments"]}, "postings": []}


def net(position):
    return sum((amount(lot["qty"]) for lot in position["lots"]), Fraction(0))


def post(ledger, asset, value, kind, reference):
    if not value:
        return
    ledger["cash"][asset] = text(amount(ledger["cash"].get(asset, "0")) + value)
    ledger["postings"].append({"asset": asset, "amount": text(value), "kind": kind, "reference": reference})


def apply_fill(ledger, config, record):
    instrument, fill = record["instrument_id"], record["fill"]
    spec, position = config["instruments"][instrument], ledger["positions"][instrument]
    qty, price = amount(fill["quantity"]), amount(fill["price"])
    signed = qty if record["side"] == "BUY" else -qty
    old = net(position)
    future = spec["asset_class"] == "FUTURE"
    if not future and old + signed < 0:
        raise ValidationError("Spot execution would create negative inventory; reconcile source")
    if not old and signed:
        position.update(first_fill_ref=record["reference"], first_fill_clock=record["clock"])
    remaining = signed
    realized = Fraction(0)
    while remaining and position["lots"] and amount(position["lots"][0]["qty"]) * remaining < 0:
        lot = position["lots"][0]
        lot_qty = amount(lot["qty"])
        matched = min(abs(remaining), abs(lot_qty))
        direction = 1 if lot_qty > 0 else -1
        realized += direction * matched * (price - amount(lot["basis_price"])) * amount(spec["contract_multiplier"])
        new_qty = lot_qty - direction * matched
        remaining += direction * matched
        if new_qty:
            lot["qty"] = text(new_qty)
        else:
            position["lots"].pop(0)
    if remaining:
        position["lots"].append({"qty": text(remaining), "entry_price": fill["price"], "basis_price": fill["price"]})
    if future:
        post(ledger, spec["pnl_asset"], realized, "REALIZED_PNL", record["reference"])
    else:
        post(ledger, spec["quote_asset"], -signed * price, "SPOT_PRINCIPAL", record["reference"])
        post(ledger, spec["base_asset"], signed, "SPOT_INVENTORY", record["reference"])
    for charge in fill["charges"]:
        if not future and charge["asset"] == spec["base_asset"]:
            fee = amount(charge["amount"])
            if fee > net(position):
                raise ValidationError("Base-asset fee exceeds remaining inventory")
            if fee < 0:
                position["lots"].append({"qty": text(-fee), "entry_price": "0", "basis_price": "0"})
            while fee > 0:
                lot = position["lots"][0]; available = amount(lot["qty"]); taken = min(available, fee)
                fee -= taken
                if available == taken:
                    position["lots"].pop(0)
                else:
                    lot["qty"] = text(available - taken)
        post(ledger, charge["asset"], -amount(charge["amount"]), charge["kind"], record["reference"])
        position["charges"].append(dict(charge, reference=record["reference"]))
    position["mark"] = fill["price"]


def rebuild(config, economic, fills):
    ledger = empty_ledger(config)
    def order(entry):
        if entry["kind"] == "FILL":
            f = fills[entry["fill_key"]]["fill"]
            return utc_us(f["executed_at"]), 0, f["execution_sequence"]
        return utc_us(entry["effective_at"]), 1 if entry["kind"] == "MARK" else 2, entry["reference"]
    for entry in sorted(economic, key=order):
        kind, instrument = entry["kind"], entry.get("instrument_id")
        if kind == "FILL":
            if not fills[entry["fill_key"]]["busted"]:
                apply_fill(ledger, config, fills[entry["fill_key"]])
        elif kind == "MARK":
            ledger["positions"][instrument]["mark"] = entry["price"]
        elif kind == "SETTLE":
            s, position = config["instruments"][instrument], ledger["positions"][instrument]
            price = amount(entry["price"])
            pnl = sum((amount(lot["qty"]) * (price - amount(lot["basis_price"])) * amount(s["contract_multiplier"]) for lot in position["lots"]), Fraction(0))
            post(ledger, s["pnl_asset"], pnl, "SETTLEMENT", entry["reference"])
            for lot in position["lots"]:
                lot["basis_price"] = entry["price"]
            position.update(mark=entry["price"], settlement_ref=entry["reference"])
        else:
            raise ValidationError("Unknown economic entry")
    return ledger


def unsettled(position, spec):
    if position["mark"] is None:
        return Fraction(0)
    return sum((amount(lot["qty"]) * (amount(position["mark"]) - amount(lot["basis_price"])) * amount(spec["contract_multiplier"]) for lot in position["lots"]), Fraction(0))


def reserved(order):
    return Fraction(0) if order["finalized"] else max(amount(order["quantity"]) - amount(order["filled"]), Fraction(0))


def valuation(config, state):
    ledger, asset = state["ledger"], config["valuation_asset"]
    pnl = Fraction(0)
    initial = maintenance = order_margin = Fraction(0)
    cash_reserved = {}
    for instrument, p in ledger["positions"].items():
        s = config["instruments"][instrument]
        if config["ledger_model"] == "FUTURES_MARGIN":
            pnl += unsettled(p, s)
            initial += abs(net(p)) * amount(s["initial_per_contract"])
            maintenance += abs(net(p)) * amount(s["maintenance_per_contract"])
        elif net(p) and p["mark"] is not None:
            pnl += net(p) * amount(p["mark"])
    for order in state["orders"].values():
        remaining = reserved(order)
        if not remaining:
            continue
        s = config["instruments"][order["instrument_id"]]
        if config["ledger_model"] == "FUTURES_MARGIN" and order["position_effect"] == "OPEN":
            order_margin += remaining * amount(s["initial_per_contract"])
        elif config["ledger_model"] == "CASH" and order["side"] == "BUY":
            cash_reserved[s["quote_asset"]] = cash_reserved.get(s["quote_asset"], Fraction(0)) + remaining * amount(order["limit_price"])
        elif config["ledger_model"] == "CASH":
            cash_reserved[s["base_asset"]] = cash_reserved.get(s["base_asset"], Fraction(0)) + remaining
        fee = order["fee_reserve"]
        cash_reserved[fee["asset"]] = cash_reserved.get(fee["asset"], Fraction(0)) + amount(fee["amount"])
    equity = amount(ledger["cash"].get(asset, "0")) + pnl
    available = equity - initial - order_margin - cash_reserved.get(asset, Fraction(0))
    valued_inventory = {s["base_asset"] for i, s in config["instruments"].items() if config["ledger_model"] == "CASH" and ledger["positions"][i]["mark"] is not None}
    unvalued = [key for key, value in ledger["cash"].items() if key != asset and key not in valued_inventory and amount(value)]
    gross = Fraction(0)
    for instrument, p in ledger["positions"].items():
        s = config["instruments"][instrument]
        orders = [o for o in state["orders"].values() if o["instrument_id"] == instrument and reserved(o)]
        buy = sum((reserved(o) for o in orders if o["side"] == "BUY"), Fraction(0))
        sell = sum((reserved(o) for o in orders if o["side"] == "SELL"), Fraction(0))
        prices = [abs(amount(o["limit_price"])) for o in orders] + ([abs(amount(p["mark"]))] if p["mark"] is not None else [])
        if prices:
            gross += max(abs(net(p) + buy), abs(net(p) - sell)) * max(prices) * amount(s["contract_multiplier"])
    return {"asset": asset, "equity": text(equity), "unsettled_pnl": text(pnl) if config["ledger_model"] == "FUTURES_MARGIN" else "0",
            "initial_used": text(initial), "maintenance_required": text(maintenance), "order_reserved": text(order_margin), "available_margin": text(available),
            "cash_reserved": {key: text(value) for key, value in cash_reserved.items()}, "unvalued_assets": unvalued, "gross_notional": text(gross)}
