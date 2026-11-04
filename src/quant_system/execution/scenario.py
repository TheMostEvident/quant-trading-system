"""Archive explicit synthetic commands/reports; never infer fills from a book."""

from pathlib import Path

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.replay import build_manifest, ReplaySession
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode

from .journal import ExecutionJournal
from .ledger import net, text, valuation, validate_config


def run_scenario(path, output_dir):
    value = decode(Path(path).read_bytes())
    if set(value) != {"scenario_version", "config", "events"} or value["scenario_version"] != "oms-scenario-v1" or not isinstance(value["events"], list) or not 1 <= len(value["events"]) <= 10000:
        raise ValidationError("Expected 1..10000 explicit OMS inputs")
    config = validate_config(value["config"])
    directory = Path(output_dir)
    if directory.exists() and any(directory.iterdir()):
        raise ValidationError("OMS scenario output directory must be empty")
    directory.mkdir(parents=True, exist_ok=True)
    trace = []
    with JournalStore(directory / "history.db") as store:
        store.register_run(config["run_id"], mode="SYNTHETIC_REPLAY", config={"oms": config, "scenario_sha256": digest(encode(value))}, code_version="synthetic-oms-v1")
        journal = ExecutionJournal(store, config)
        for event in value["events"]:
            result = journal.apply(event)
            snapshots = journal.publish_snapshots(["oms-audit"])
            state = journal.state(); v = valuation(config, state)
            trace.append({"event_id": event["event_id"], "kind": event["kind"], "result": result,
                          "positions": {key: text(net(p)) for key, p in state["ledger"]["positions"].items()},
                          "balances": state["ledger"]["cash"], "valuation": v, "faults": state["faults"],
                          "account_ref": snapshots[0]["message_id"], "position_refs": [m["message_id"] for m in snapshots[1:]]})
            deliveries = store.dispatch(config["run_id"], "oms-audit", seen_at=event["at"], consumer_epoch="oms-audit-1", monotonic_ns=event["clock"]["monotonic_ns"])
            for delivery in deliveries:
                store.acknowledge("oms-audit", delivery["message_id"], at=event["at"])
        boundary = store.boundary(config["run_id"], "oms-audit", at=value["events"][-1]["at"], decision_id=config["run_id"] + ":oms-final")
        manifest = build_manifest(store, boundary)
        replay = ReplaySession(manifest)
        state = journal.state()
        summary = {"data_origin": "SYNTHETIC", "events": len(trace), "reports_applied": len(state["fills"]), "broker_network_calls": 0,
                   "commands": len(state["commands"]), "claimed_commands": sum(bool(c.get("dispatched_at")) for c in state["commands"].values()),
                   "accounting_model": config["model"], "integrity": journal.verify(), "trace_sha256": digest(encode(trace)),
                   "state_sha256": digest(encode(state)), "manifest_sha256": manifest["sha256"],
                   "delivery_replay_speed_independent": replay.trace_digest(target_run_id="oms-replay", speed=1) == replay.trace_digest(target_run_id="oms-replay", speed=1000),
                   "positions": trace[-1]["positions"], "balances": trace[-1]["balances"], "valuation": trace[-1]["valuation"]}
        for name, obj in (("inputs.json", value), ("trace.json", trace), ("manifest.json", manifest), ("summary.json", summary)):
            with (directory / name).open("xb") as handle:
                handle.write(encode(obj) + b"\n")
    return summary
