"""Persist synthetic market/command inputs and generated fills through the OMS."""

from pathlib import Path

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.models import Capture
from quant_system.foundation.replay import ReplaySession, build_manifest
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode, utc_us

from .journal import ExecutionJournal
from .ledger import amount, net, text, valuation, validate_config
from .paper import MODEL, PaperBroker


def run_paper_scenario(path, output_dir):
    raw = Path(path).read_bytes()
    if len(raw) > 32 * 1024 * 1024:
        raise ValidationError("Use a bounded paper scenario")
    value = decode(raw)
    if set(value) != {"scenario_version", "config", "assumptions", "events"} or value["scenario_version"] != "paper-scenario-v1" or not isinstance(value["events"], list) or not 1 <= len(value["events"]) <= 10000:
        raise ValidationError("Expected 1..10000 synthetic paper inputs")
    config = validate_config(value["config"])
    broker = PaperBroker(config, value["assumptions"])
    directory = Path(output_dir)
    if directory.exists() and any(directory.iterdir()):
        raise ValidationError("Paper output directory must be empty")
    directory.mkdir(parents=True, exist_ok=True)
    trace = []
    reports_delivered = 0
    with JournalStore(directory / "history.db") as store:
        store.register_run(config["run_id"], mode="SYNTHETIC_REPLAY", config={"oms": config, "paper": broker.parameters,
                           "paper_inputs_sha256": digest(encode(value))}, code_version=MODEL)
        journal = ExecutionJournal(store, config)
        store.put_artifact(config["run_id"] + ":paper-assumptions", config["run_id"], broker.parameters, ready_at=config["ready_at"])

        def apply(event, source):
            result = journal.apply(event)
            if event["kind"] == "DISPATCH" and result["status"] == "CLAIMED":
                broker.submit(result["command"], at=event["at"], clock=event["clock"])
            messages = journal.publish_snapshots(["paper-audit"])
            deliveries = store.dispatch(config["run_id"], "paper-audit", seen_at=event["at"], consumer_epoch="paper-audit-v1", monotonic_ns=event["clock"]["monotonic_ns"])
            for row in deliveries:
                store.acknowledge("paper-audit", row["message_id"], at=event["at"])
            state = journal.state()
            trace.append({"event_id": event["event_id"], "kind": event["kind"], "source": source, "at": event["at"], "result": result,
                          "positions": {key: text(net(position)) for key, position in state["ledger"]["positions"].items()},
                          "balances": state["ledger"]["cash"], "valuation": valuation(config, state), "faults": state["faults"],
                          "account_ref": messages[0]["message_id"]})

        def deliver(reports):
            nonlocal reports_delivered
            for record in reports:
                message = record["report"]
                apply({"event_id": "paper-generated:" + message["message_id"], "kind": "REPORT", "at": record["at"],
                       "clock": record["clock"], "instrument_id": message["instrument_id"], "data": {"report": message}}, "PAPER_GENERATED")
                reports_delivered += 1

        allowed = {"TARGET", "DISPATCH", "CANCEL", "TICK", "MARK", "SETTLE", "HALT", "RESUME", "DISCONNECT", "UNCERTAIN"}
        for index, item in enumerate(value["events"], 1):
            if not isinstance(item, dict) or item.get("kind") not in ("OMS", "MARKET", "TICK"):
                raise ValidationError("Unsupported paper scenario input")
            if item["kind"] == "OMS":
                event = item["event"]
                if event["kind"] not in allowed or event["event_id"].startswith("paper-generated:"):
                    raise ValidationError("External reports/reconciliation cannot inject paper fills")
                at, clock = event["at"], event["clock"]
            elif item["kind"] == "MARKET":
                at, clock = item["message"]["ready_at"], item["clock"]
            else:
                at, clock = item["at"], item["clock"]
            if utc_us(at) < utc_us(config["ready_at"]):
                raise ValidationError("Paper inputs precede configuration availability")
            # Preserve the original input before any matching or OMS effect.
            store.record_capture(Capture(config["run_id"] + ":paper-input:" + str(index), config["run_id"], "paper-scenario", "v1", index,
                                         "synthetic-paper-input", "market-and-commands", encode(item), at, at, clock["monotonic_ns"],
                                         media_type="application/json", representation="SYNTHETIC_INPUT_JSON"))
            deliver(broker.advance(at=at, clock=clock))
            if item["kind"] == "OMS":
                apply(event, "EXPLICIT_SYNTHETIC_INPUT")
            elif item["kind"] == "MARKET":
                message = item["message"]
                store.put_artifact(message["message_id"], config["run_id"], message, ready_at=at)
                deliver(broker.on_book(message, clock=clock))
                trace.append({"kind": "MARKET", "at": at, "message_id": message["message_id"], "matching": broker.last_book_result})
            else:
                trace.append({"kind": "TICK", "at": at})
        if not trace or not any(row.get("account_ref") for row in trace):
            raise ValidationError("Paper scenario has no committed OMS input")
        last_at = at
        boundary = store.boundary(config["run_id"], "paper-audit", at=last_at, decision_id=config["run_id"] + ":paper-end")
        manifest = build_manifest(store, boundary)
        replay = ReplaySession(manifest)
        state, model = journal.state(), broker.snapshot()
        summary = {"data_origin": "SYNTHETIC", "execution_model": MODEL, "broker_network_calls": 0,
                   "input_events": len(value["events"]), "generated_fills": broker.fill_sequence, "reports_delivered": reports_delivered,
                   "pending_reports": len(broker.pending_reports), "pending_command_arrivals": sum(x["kind"] != "EXPIRE" for x in broker.arrivals),
                   "unfilled_contracts": text(sum((amount(o["quantity"]) - amount(o["filled"]) for o in broker.orders.values()), amount("0"))),
                   "positions": {key: text(net(p)) for key, p in state["ledger"]["positions"].items()},
                   "balances": state["ledger"]["cash"], "valuation": valuation(config, state), "faults": state["faults"],
                   "integrity": journal.verify(), "trace_sha256": digest(encode(trace)), "state_sha256": digest(encode(state)),
                   "paper_state_sha256": digest(encode(model)), "manifest_sha256": manifest["sha256"],
                   "delivery_replay_speed_independent": replay.trace_digest(target_run_id="paper-replay", speed=1) == replay.trace_digest(target_run_id="paper-replay", speed=1000),
                   "queue_position": "UNOBSERVED_NO_PASSIVE_QUEUE_FILLS", "liquidity_model": "PER_PRICE_SESSION_HIGH_WATER_NO_ASSUMED_REPLENISHMENT",
                   "field_validation": "PENDING_EXTRA_ISSUE_2_43"}
        for name, obj in (("inputs.json", value), ("trace.json", trace), ("paper-state.json", model), ("manifest.json", manifest), ("summary.json", summary)):
            with (directory / name).open("xb") as output:
                output.write(encode(obj) + b"\n")
    return summary
