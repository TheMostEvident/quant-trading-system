"""Run separate Windows bridge/core processes into an immutable field bundle."""

from datetime import date
from pathlib import Path
import os
import math
import subprocess
import time
import uuid

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.environment import local_path, require_runtime
from quant_system.foundation.values import encode

from .cli import ingest_file
from .profile import validate_profile
from .verification import audit_session, profile_review


def write_new_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(encode(value) + b"\n")


def make_profile(template, code, trading_date):
    profile = validate_profile(template)
    if not isinstance(code, str) or not code.isascii() or not code.isalnum():
        raise ValidationError("Explicit public instrument code required")
    date.fromisoformat(trading_date)
    binding = profile["instruments"][0]
    binding.update(source_code=code, instrument_id="KIWOOM:" + code, trading_date=trading_date, spec_verified=False)
    binding["specification"] = {"quantity_unit": "CONTRACT", "price_unit": None, "tick_size": None,
                                "contract_month": None, "verification_evidence": None, "trading_date_evidence": None}
    profile.update(data_origin="OBSERVED", instruments=[binding], status_codes={}, session_mapping_verified=False)
    profile["mapping_evidence"] = "PENDING: verify the installed futures session/status mapping"
    return validate_profile(profile)


def run_field_session(bridge_python, profile, output_dir, *, seconds=120, max_market_events=500,
                      bootstrap_tr=True, fsync_budget_ms=1000, previous_directory=None):
    profile = validate_profile(profile)
    if os.name != "nt":
        raise ValidationError("Field orchestration requires the user's Windows/OCX host")
    require_runtime()
    if profile["data_origin"] != "OBSERVED" or len(profile["instruments"]) != 1:
        raise ValidationError("First field validation uses one explicit observed instrument")
    if type(seconds) is not int or not 10 <= seconds <= 1800 or type(max_market_events) is not int or not 1 <= max_market_events <= 2000:
        raise ValidationError("Use 10..1800 seconds and 1..2000 market events")
    if type(bootstrap_tr) is not bool or type(fsync_budget_ms) not in (int, float) or not math.isfinite(fsync_budget_ms) or fsync_budget_ms <= 0:
        raise ValidationError("Explicit TR option and positive finite latency budget required")
    interpreter = Path(bridge_python).resolve()
    if not interpreter.is_file():
        raise ValidationError("Bridge interpreter not found")
    root = local_path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    run_id = "field-" + uuid.uuid4().hex
    write_new_json(root / "profile.json", profile)
    write_new_json(root / "session.json", {"run_id": run_id, "mode": "OBSERVED_MOCK_FIELD_RUN",
                   "seconds": seconds, "max_market_events": max_market_events, "bootstrap_tr": bootstrap_tr,
                   "fsync_budget_ms": fsync_budget_ms, "profile_review": profile_review(profile)})
    source_root = str(Path(__file__).resolve().parents[2])
    launcher = "import sys;sys.path.insert(0,sys.argv.pop(1));from quant_system.market.bridge import main;raise SystemExit(main())"
    command = [str(interpreter), "-c", launcher, source_root, "--codes", profile["instruments"][0]["source_code"],
               "--output", str(root / "callbacks.jsonl"), "--report", str(root / "collector.json"),
               "--seconds", str(seconds), "--max-market-events", str(max_market_events)]
    if bootstrap_tr:
        command.append("--bootstrap-tr")
    collector = None
    try:
        with (root / "collector-console.log").open("xb") as log:
            collector = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, shell=False)
            started = time.monotonic()
            while not (root / "callbacks.jsonl").exists():
                if collector.poll() is not None or time.monotonic() - started > 20:
                    raise ValidationError("Collector did not create a capture; inspect collector-console.log")
                time.sleep(.05)
            ingestion = ingest_file(root / "callbacks.jsonl", root / "history.db", run_id, profile,
                                    manifest_path=root / "manifest.json", follow=True, seconds=seconds + 15,
                                    producer_finished=lambda: collector.poll() is not None)
            write_new_json(root / "ingest.json", ingestion)
            collector.wait(timeout=15)
        result = audit_session(root, fsync_budget_ms=fsync_budget_ms, previous_directory=previous_directory)
        write_new_json(root / "audit.json", result)
        return result
    except BaseException as error:
        if collector is not None and collector.poll() is None:
            collector.terminate()
            try:
                collector.wait(timeout=10)
            except subprocess.TimeoutExpired:
                collector.kill()
                collector.wait(timeout=5)
        write_new_json(root / "field-error.json", {"error": type(error).__name__, "field_checks_passed": False})
        raise
