"""Explicit synthetic callbacks through features, policy, OMS and paper fills."""

from copy import deepcopy
from pathlib import Path
import time

from quant_system.contracts import validate_message
from quant_system.execution.journal import ExecutionJournal
from quant_system.execution.ledger import amount, net, reserved, text, valuation, validate_config
from quant_system.execution.paper import PaperBroker
from quant_system.foundation.errors import ValidationError
from quant_system.foundation.models import Capture, Version
from quant_system.foundation.replay import ReplaySession, build_manifest
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode, timestamp, utc_us
from quant_system.market.feature_replay import verify_feature_replay
from quant_system.market.feed import MarketFeed
from quant_system.market.latency import LatencyHistogram
from quant_system.market.profile import validate_profile
from quant_system.strategy.engine import StaticSignalEngine


def run_layer1(path, output_dir):
    raw = Path(path).read_bytes()
    if len(raw) > 32 * 1024 * 1024:
        raise ValidationError("Bounded synthetic layer-1 scenario required")
    value = decode(raw)
    required = {"scenario_version", "config", "market_profile", "feature_config", "assumptions", "policies", "risk", "fee_reserve", "clock_domain", "events"}
    if set(value) != required or value["scenario_version"] != "layer1-scenario-v1" or not isinstance(value["events"], list) or not 1 <= len(value["events"]) <= 10000:
        raise ValidationError("Unsupported layer-1 scenario")
    config, profile = validate_config(value["config"]), validate_profile(value["market_profile"])
    if config["data_origin"] != "SYNTHETIC" or profile["data_origin"] != "SYNTHETIC":
        raise ValidationError("Layer-1 validation never upgrades observed or unverified evidence")
    if not profile["session_mapping_verified"] or any(not b["spec_verified"] for b in profile["instruments"]):
        raise ValidationError("Explicit synthetic instrument/session rules required")
    if {b["instrument_id"] for b in profile["instruments"]} != set(config["instruments"]) or set(value["policies"]) != set(config["instruments"]):
        raise ValidationError("Profile, policies and ledger instruments must agree")
    config["source_bindings"] = {}
    for binding in profile["instruments"]:
        instrument = binding["instrument_id"]
        spec = {k: v for k, v in config["instruments"][instrument].items() if k != "instrument_spec_ref"}
        if any(binding["specification"].get(k) != spec.get(k) for k in ("quantity_unit", "price_unit", "tick_size")):
            raise ValidationError("Source and ledger units/tick differ")
        binding["specification"] = spec
        config["instruments"][instrument]["instrument_spec_ref"] = config["run_id"] + ":spec:" + digest(encode(binding))
        config["source_bindings"][instrument] = deepcopy(binding)
    config = validate_config(config)
    broker = PaperBroker(config, value["assumptions"])
    directory = Path(output_dir)
    if directory.exists() and any(directory.iterdir()):
        raise ValidationError("Layer-1 output directory must be empty")
    directory.mkdir(parents=True, exist_ok=True)
    trace, decisions, policies = [], [], {}
    timings = LatencyHistogram()
    counter = 0
    base = utc_us(config["ready_at"])

    def simulation_clock(at):
        return dict(value["clock_domain"], monotonic_ns=(utc_us(at) - base) * 1000)

    with JournalStore(directory / "history.db") as store:
        feed = MarketFeed(store, config["run_id"], profile, consumer="layer1", feature_config=value["feature_config"])
        feed._initialize(config["ready_at"])
        journal = ExecutionJournal(store, config)
        engine = StaticSignalEngine()
        store.put_artifact(config["run_id"] + ":paper-assumptions", config["run_id"], broker.parameters, ready_at=config["ready_at"])
        store.put_artifact(config["run_id"] + ":layer1-inputs", config["run_id"], value, ready_at=config["ready_at"])

        def deliver(at):
            for row in store.dispatch(config["run_id"], "layer1", seen_at=at, consumer_epoch="layer1-audit-v1", monotonic_ns=simulation_clock(at)["monotonic_ns"]):
                store.acknowledge("layer1", row["message_id"], at=at)

        def apply(kind, at, data=None, instrument=None, event_id=None):
            nonlocal counter
            counter += 1
            event = {"event_id": event_id or "layer1-oms-" + str(counter), "kind": kind, "at": at, "clock": simulation_clock(at),
                     "instrument_id": instrument or next(iter(config["instruments"])), "data": data or {}}
            result = journal.apply(event)
            if kind == "DISPATCH" and result["status"] == "CLAIMED":
                broker.submit(result["command"], at=at, clock=event["clock"])
            snapshots = journal.publish_snapshots(["layer1"])
            state = journal.state()
            trace.append({"event_id": event["event_id"], "kind": kind, "at": at, "result": result,
                          "positions": {key: text(net(p)) for key, p in state["ledger"]["positions"].items()},
                          "balances": state["ledger"]["cash"], "valuation": valuation(config, state), "faults": state["faults"],
                          "account_ref": snapshots[0]["message_id"]})
            return result

        def reports(records):
            for record in records:
                message = record["report"]
                apply("REPORT", record["at"], {"report": message}, message["instrument_id"], "generated:" + message["message_id"])

        def publish_policy(message):
            message = deepcopy(message)
            instrument = message["instrument_id"]
            if message["run_id"] != config["run_id"] or message["data_origin"] != "SYNTHETIC" or instrument not in config["instruments"]:
                raise ValidationError("Policy scope mismatch")
            message["payload"].update(instrument_spec_ref=config["instruments"][instrument]["instrument_spec_ref"], feature_config_ref=feed.feature_ref)
            validate_message(message)
            seq = len([x for x in trace if x["kind"] == "POLICY"]) + 1
            capture = config["run_id"] + ":policy-input:" + str(seq)
            store.record_capture(Capture(capture, config["run_id"], "synthetic-policy", "v1", seq, "static-policy", "policy", encode(message),
                                         message["received_at"], message["ready_at"], simulation_clock(message["ready_at"])["monotonic_ns"], representation="SYNTHETIC_INPUT_JSON"))
            store.publish(capture, message, ["layer1"], version=Version("policy:" + instrument, message["payload"]["revision"], message["payload"]["valid_from"],
                          message["payload"]["expires_at"], owner_epoch=message["payload"]["provider_epoch"]))
            policies[instrument] = message
            trace.append({"kind": "POLICY", "at": message["ready_at"], "message_id": message["message_id"]})

        def decide(market, at):
            nonlocal counter
            instrument = market["instrument_id"]
            account, *positions = journal.snapshots()
            position = next(p for p in positions if p["instrument_id"] == instrument)
            policy = policies[instrument]
            state = journal.state(); spec = config["instruments"][instrument]
            q = net(state["ledger"]["positions"][instrument]); target = amount(policy["payload"]["target_position_qty"])
            other_gross = amount("0")
            for key, p in state["ledger"]["positions"].items():
                if key == instrument:
                    continue
                orders = [o for o in state["orders"].values() if o["instrument_id"] == key and reserved(o)]
                prices = [abs(amount(o["limit_price"])) for o in orders] + ([abs(amount(p["mark"]))] if p["mark"] is not None else [])
                if prices:
                    buys = sum((reserved(o) for o in orders if o["side"] == "BUY"), amount("0"))
                    sells = sum((reserved(o) for o in orders if o["side"] == "SELL"), amount("0"))
                    other_gross += max(abs(net(p) + buys), abs(net(p) - sells)) * max(prices) * amount(config["instruments"][key]["contract_multiplier"])
            total_required = amount(account["payload"]["margin"]["initial_used"]) + (abs(target) - abs(q)) * amount(spec["initial_per_contract"])
            context_id = config["run_id"] + ":context:" + str(len(decisions) + 1)
            context = {"evidence_version": "static-context-v1", "evidence_id": context_id, "run_id": config["run_id"], "account_id": config["account_id"],
                       "instrument_id": instrument, "data_origin": "SYNTHETIC", "ready_at": at, "valid_from": config["ready_at"],
                       "expires_at": value["assumptions"]["contract_expiry"][instrument],
                       "instrument_spec_ref": spec["instrument_spec_ref"], "universe_ref": policy["payload"]["universe_ref"],
                       "risk_budget_ref": policy["payload"]["risk_budget_ref"], "feature_config_ref": feed.feature_ref,
                       "specification": spec, "feature_config": feed.features.config,
                       "account_capability": {"verified": True, "can_open_long": spec["can_open_long"], "can_open_short": spec["can_open_short"]},
                       "risk": {"max_abs_position_qty": value["risk"]["max_abs_position_qty"],
                                "max_gross_notional": {"asset": config["valuation_asset"], "amount": config["max_gross_notional"]},
                                "other_gross_notional": {"asset": config["valuation_asset"], "amount": text(other_gross)},
                                "margin_quote": {"account_state_ref": account["message_id"], "position_state_ref": position["message_id"],
                                                 "quote_ref": market["message_id"], "target_position_qty": text(target), "margin_rule_ref": config["margin_rule_ref"],
                                                 "available_basis": "NET_OF_ORDER_RESERVED", "total_initial_required": {"asset": config["valuation_asset"], "amount": text(total_required)},
                                                 "incremental_required": {"asset": config["valuation_asset"], "amount": text(max(total_required - amount(account["payload"]["margin"]["initial_used"]), 0))}}},
                       "position_lifecycle": {"position_state_ref": position["message_id"], "first_fill_ref": position["payload"]["first_fill_ref"],
                                              "first_fill_clock": position["payload"]["first_fill_clock"]} if q else None}
            store.put_artifact(context_id, config["run_id"], context, ready_at=at)
            bundle = {"decision_id": config["run_id"] + ":decision:" + str(len(decisions) + 1), "run_id": config["run_id"], "account_id": config["account_id"],
                      "instrument_id": instrument, "as_of": at, "clock": simulation_clock(at), "policy": policy, "market": market,
                      "account": account, "position": position, "evidence": context,
                      "seen_at": {m["message_id"]: at for m in (policy, market, account, position)}}
            bundle["seen_at"][context_id] = at
            decision = engine.process(bundle)
            store.put_artifact(decision["decision_id"], config["run_id"], decision, ready_at=at)
            outcome = None
            deliver(at)
            boundary = store.boundary(config["run_id"], "layer1", at=at, decision_id=decision["decision_id"] + ":boundary", input_refs=decision["input_refs"])
            if decision["action"] in ("ENTER", "INCREASE", "REDUCE", "EXIT") and decision["quote_ref"] and store.healthy_at(boundary):
                counter += 1
                quote_clock = market["payload"]["features"]["details"]["clock"]
                event = {"event_id": "layer1-target-" + str(counter), "kind": "TARGET", "at": at, "clock": simulation_clock(at), "instrument_id": instrument,
                         "data": {"intent_id": decision["decision_id"], "intent_expires_at": timestamp(utc_us(at) + policy["payload"]["execution_limits"]["order_ttl_ms"] * 1000),
                                  "quote_clock": {k: quote_clock[k] for k in ("producer_id", "epoch", "monotonic_ns")},
                                  "limit_price": market["payload"]["book"]["asks" if amount(decision["target_position_qty"]) > q else "bids"][0]["price"],
                                  "fee_reserve": value["fee_reserve"]}}
                outcome = journal.submit_at_boundary(boundary, signal_ref=decision["decision_id"], policy_ref=policy["message_id"], quote_ref=market["message_id"], event=event)
                journal.publish_snapshots(["layer1"])
            decisions.append({"decision": decision, "oms_result": outcome, "boundary": boundary.decision_id})

        at = config["ready_at"]
        apply("TICK", at)
        for message in value["policies"].values():
            if message["ready_at"] != at:
                raise ValidationError("Seed policies must be available at configuration time")
            p = message["payload"]
            for ref, data in ((p["universe_ref"], {"synthetic_only": True, "seed_instruments": sorted(config["instruments"])}),
                              (p["risk_budget_ref"], {"synthetic_only": True, "risk": value["risk"], "account_gross": config["max_gross_notional"]}),
                              (p["hold_exit"]["emergency_rule_ref"], {"synthetic_only": True, "rule": "close target subject to unchanged OMS guards"})):
                store.put_artifact(ref, config["run_id"], data, ready_at=at)
            publish_policy(message)
        for item in value["events"]:
            started = time.perf_counter_ns()
            kind = item["kind"]
            at = item["ready_at"] if kind == "CALLBACK" else item["at"]
            reports(broker.advance(at=at, clock=simulation_clock(at)))
            apply("TICK", at)
            if kind == "CALLBACK":
                output = feed.process(encode(item["frame"]) + b"\n", ready_at=at)
                for market in output:
                    reports(broker.on_book(market, clock=simulation_clock(at)))
                    deliver(at)
                    decide(market, at)
            elif kind == "POLICY":
                publish_policy(item["message"])
            elif kind == "TIMER":
                for market in feed.tick(at=at, transport_failed=item.get("transport_failed", False)):
                    reports(broker.on_book(market, clock=simulation_clock(at)))
                    deliver(at); decide(market, at)
            elif kind == "OMS" and item["event_kind"] in ("HALT", "RESUME", "DISCONNECT", "CANCEL", "UNCERTAIN", "MARK", "SETTLE"):
                apply(item["event_kind"], at, item.get("data"), item.get("instrument_id"))
            else:
                raise ValidationError("Unsupported layer-1 control input")
            for command in list(journal.state()["commands"].values()):
                if command["status"] == "QUEUED":
                    apply("DISPATCH", at, {"command_id": command["command_id"]}, command["payload"]["instrument_id"])
            deliver(at)
            timings.record((time.perf_counter_ns() - started) / 1000000)
        state = journal.state()
        boundary = store.boundary(config["run_id"], "layer1", at=at, decision_id=config["run_id"] + ":layer1-end")
        manifest = build_manifest(store, boundary)
        replay = ReplaySession(manifest)
        summary = {"data_origin": "SYNTHETIC", "broker_network_calls": 0, "run_id": config["run_id"],
                   "positions": {key: text(net(p)) for key, p in state["ledger"]["positions"].items()}, "balances": state["ledger"]["cash"],
                   "valuation": valuation(config, state), "faults": state["faults"], "decisions": len(decisions), "commands": len(broker.commands),
                   "generated_fills": broker.fill_sequence, "pending_reports": len(broker.pending_reports), "source_metrics": feed.metrics,
                   "integrity": journal.verify(), "feature_recomputation": verify_feature_replay(manifest),
                   "replay_speed_independent": replay.trace_digest(target_run_id="layer1-replay", speed=1) == replay.trace_digest(target_run_id="layer1-replay", speed=1000),
                   "trace_sha256": digest(encode(trace)), "decisions_sha256": digest(encode(decisions)), "state_sha256": digest(encode(state)),
                   "manifest_sha256": manifest["sha256"], "step_processing_latency": timings.summary(),
                   "timing_scope": "local synthetic step processing, not broker latency or HFT capacity", "field_validation": "PENDING_EXTRA_ISSUES_33_43_45"}
        for filename, data in (("inputs.json", value), ("effective-config.json", config), ("trace.json", trace), ("decisions.json", decisions),
                               ("manifest.json", manifest), ("summary.json", summary)):
            with (directory / filename).open("xb") as target:
                target.write(encode(data) + b"\n")
    return summary
