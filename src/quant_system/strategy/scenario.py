"""Offline bundle runner. Supplied position changes are fixtures, never fills."""

from pathlib import Path

from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.values import decode, digest, encode

from .engine import StaticSignalEngine


def evaluate_scenario(value):
    scenario = decode(encode(value))
    if set(scenario) != {"scenario_version", "bundles"} or scenario["scenario_version"] != "static-scenario-v1":
        raise ValidationError("Unsupported scenario")
    if not isinstance(scenario["bundles"], list) or not scenario["bundles"] or len(scenario["bundles"]) > 100_000:
        raise ValidationError("Scenario requires 1..100000 input bundles")
    engine = StaticSignalEngine()
    decisions = []
    # Unique decision IDs in the archive; duplicate market messages remain
    # legal and exercise delivery/timer idempotence.
    ids = set()
    for bundle in scenario["bundles"]:
        if bundle["decision_id"] in ids:
            raise ValidationError("Scenario decision ID duplicated")
        ids.add(bundle["decision_id"])
        decisions.append(engine.process(bundle))
    return {"scenario_sha256": digest(encode(scenario)), "decisions_sha256": digest(encode(decisions)),
            "decisions": decisions, "orders_created": 0, "positions_written": 0}


def run_scenario(path, *, output_dir=None):
    value = decode(Path(path).read_bytes())
    first = evaluate_scenario(value)
    second = evaluate_scenario(decode(encode(value)))
    if encode(first) != encode(second):
        raise IntegrityFailure("Scenario replay differs")
    if output_dir is not None:
        directory = Path(output_dir)
        if directory.exists() and any(directory.iterdir()):
            raise ValidationError("Scenario output directory must be empty")
        directory.mkdir(parents=True, exist_ok=True)
        for name, obj in (("inputs.json", value), ("decisions.json", first["decisions"]), ("summary.json", {k: v for k, v in first.items() if k != "decisions"})):
            with (directory / name).open("xb") as handle:
                handle.write(encode(obj) + b"\n")
    return dict(first, deterministic_replay=True)
