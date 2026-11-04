"""v0.2 projections. References point to committed OMS inputs/configuration."""

from fractions import Fraction

from quant_system.contracts import validate_message

from .ledger import amount, average, net, reserved, text, unsettled, valuation


def messages(config, state, *, at, event_ref, config_ref):
    revision = state["revision"] + 1
    prefix = config["run_id"] + ":oms:" + config["account_id"]
    account_id = prefix + ":account:" + str(revision)
    value = valuation(config, state)
    flags = (["STATE_UNCERTAIN"] if state["faults"] or not state["connected"] else []) + (["VALUATION_UNRESOLVED"] if value["unvalued_assets"] else [])

    def envelope(kind, message_id, instrument, payload, lineage):
        m = {"schema_version": "0.2.0", "message_type": kind, "message_id": message_id, "run_id": config["run_id"],
             "instrument_id": instrument, "source": "synthetic-oms", "source_event_id": None, "event_at": None,
             "received_at": at, "ready_at": at, "data_origin": "SYNTHETIC", "quality_flags": list(flags), "lineage": lineage, "payload": payload}
        validate_message(m)
        return m

    balances = []
    for asset, wallet in sorted(state["ledger"]["cash"].items()):
        locked = amount(value["cash_reserved"].get(asset, "0"))
        if config["ledger_model"] == "FUTURES_MARGIN" and asset == value["asset"]:
            locked += amount(value["initial_used"]) + amount(value["order_reserved"])
        balances.append({"asset": asset, "wallet": wallet, "free": text(amount(wallet) - locked), "locked": text(locked)})
    margin = None
    if config["ledger_model"] == "FUTURES_MARGIN":
        margin = {key: value[key] for key in ("asset", "initial_used", "maintenance_required", "order_reserved", "available_margin")}
        margin.update(rule_ref=config["margin_rule_ref"], status="UNKNOWN" if flags else "NORMAL" if amount(value["available_margin"]) >= 0 else "SHORTFALL")
    account = envelope("AccountState", account_id, None,
                       {"account_id": config["account_id"], "account_version": revision, "ledger_model": config["ledger_model"], "balances": balances,
                        "margin": margin, "valuation": {"equity": value["equity"], "asset": value["asset"], "as_of": at, "model_ref": config_ref, "fx_refs": []},
                        "unsettled_pnl": [{"asset": value["asset"], "amount": value["unsettled_pnl"]}], "adjustments": list(dict.fromkeys(x["reference"] for x in state["ledger"]["postings"] + state.get("corrections", []))),
                        "reconciliation": state["reconciliation"]}, list(dict.fromkeys([event_ref, config_ref] + state["input_refs"])))
    output = [account]
    for instrument, p in state["ledger"]["positions"].items():
        spec = config["instruments"][instrument]; qty = net(p)
        orders = [o for o in state["orders"].values() if o["instrument_id"] == instrument and not o["finalized"]]
        status = ("UNKNOWN" if flags else "HALTED" if state["halted"] else "CLOSING" if any(o["position_effect"] == "CLOSE" for o in orders)
                  else "PARTIALLY_FILLED" if orders and qty else "PENDING" if orders else "OPEN" if qty else "FLAT")
        avg_entry = average(sum((amount(l["qty"]) * amount(l["entry_price"]) for l in p["lots"]), Fraction(0)) / qty) if qty else None
        basis = average(sum((amount(l["qty"]) * amount(l["basis_price"]) for l in p["lots"]), Fraction(0)) / qty) if qty else None
        payload = {"account_id": config["account_id"], "account_state_ref": account_id, "position_version": revision, "instrument_spec_ref": spec["instrument_spec_ref"],
                   "position_status": status, "position_mode": "NET", "net_qty": text(qty), "quantity_unit": spec["quantity_unit"], "avg_entry_price": avg_entry,
                   "pnl_reference_price": basis, "mark_price": p["mark"], "contract_multiplier": spec["contract_multiplier"], "pnl_asset": spec["pnl_asset"],
                   "reserved_buy_qty": text(sum((reserved(o) for o in orders if o["side"] == "BUY"), Fraction(0))),
                   "reserved_sell_qty": text(sum((reserved(o) for o in orders if o["side"] == "SELL"), Fraction(0))),
                   "active_orders": [o["order_id"] for o in orders], "unrealized_pnl_gross": text(unsettled(p, spec)),
                   "charges_paid": [{k: c[k] for k in ("kind", "asset", "amount")} for c in p["charges"]], "settlement_ref": p["settlement_ref"],
                   "reconciliation": state["reconciliation"], "first_fill_ref": p["first_fill_ref"], "first_fill_clock": p["first_fill_clock"]}
        output.append(envelope("PositionState", prefix + ":position:" + instrument + ":" + str(revision), instrument, payload, [event_ref, account_id, config_ref]))
    return output
