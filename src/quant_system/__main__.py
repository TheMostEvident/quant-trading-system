import argparse
from pathlib import Path

from .demo import run_demo
from .foundation.environment import environment_report
from .foundation.replay import ReplaySession
from .foundation.store import JournalStore
from .foundation.values import decode, encode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline quant research data foundation; no live orders")
    sub = parser.add_subparsers(dest="command", required=True)
    env = sub.add_parser("environment")
    env.add_argument("--probe-kiwoom", action="store_true")
    integrity = sub.add_parser("integrity")
    integrity.add_argument("database")
    backup = sub.add_parser("backup")
    backup.add_argument("database"); backup.add_argument("destination")
    replay = sub.add_parser("replay")
    replay.add_argument("manifest"); replay.add_argument("--run-id", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--output-dir", required=True)
    demo.add_argument("--config")
    market = sub.add_parser("market-ingest", help="Normalize a callback JSONL spool into a new run")
    market.add_argument("--input", required=True)
    market.add_argument("--database", required=True)
    market.add_argument("--run-id", required=True)
    market.add_argument("--config", required=True)
    market.add_argument("--manifest")
    market.add_argument("--follow", action="store_true")
    market.add_argument("--seconds", type=float, default=60)
    market.add_argument("--simulate-clock", action="store_true")
    market.add_argument("--feature-config")
    feature_check = sub.add_parser("verify-features", help="Recompute saved features from normalized inputs and raw producer clocks")
    feature_check.add_argument("manifest")
    feature_check.add_argument("--run-id", default="feature-replay")
    feature_check.add_argument("--speed", type=float, default=1)
    signals = sub.add_parser("signal-scenario", help="Evaluate fixed-policy targets from explicit offline inputs; no orders")
    signals.add_argument("scenario")
    signals.add_argument("--output-dir")
    oms = sub.add_parser("oms-scenario", help="Process explicit synthetic OMS inputs and persist accounting; no broker transport")
    oms.add_argument("scenario")
    oms.add_argument("--output-dir", required=True)
    profile_draft = sub.add_parser("market-profile-draft", help="Create an unverified observed profile; never enables the input gate")
    profile_draft.add_argument("--template", default="config/kiwoom.observed.example.json")
    profile_draft.add_argument("--code", required=True)
    profile_draft.add_argument("--trading-date", required=True)
    profile_draft.add_argument("--output", required=True)
    review = sub.add_parser("review-market-profile", help="List missing public instrument/session verification evidence")
    review.add_argument("profile")
    audit = sub.add_parser("audit-market-session", help="Cross-check raw capture, collector report, DB and replay; no API calls")
    audit.add_argument("directory")
    audit.add_argument("--previous-session")
    audit.add_argument("--fsync-budget-ms", type=float, default=1000)
    audit.add_argument("--output", required=True)
    field = sub.add_parser("market-field-run", help="User-host Windows mock capture plus concurrent core follow and field audit")
    field.add_argument("--bridge-python", required=True)
    field.add_argument("--profile", required=True)
    field.add_argument("--output-dir", required=True)
    field.add_argument("--seconds", type=int, default=120)
    field.add_argument("--max-market-events", type=int, default=500)
    field.add_argument("--without-initial-tr", action="store_true")
    field.add_argument("--fsync-budget-ms", type=float, default=1000)
    field.add_argument("--previous-session")
    args = parser.parse_args(argv)
    exit_code = 0
    if args.command == "environment":
        result = environment_report(probe_kiwoom=args.probe_kiwoom)
    elif args.command == "integrity":
        with JournalStore(args.database, read_only=True) as store:
            result = {"integrity": store.verify_integrity(), "store_id": store.store_id}
    elif args.command == "backup":
        with JournalStore(args.database) as store:
            result = store.backup(args.destination)
    elif args.command == "demo":
        result = run_demo(args.output_dir, decode(Path(args.config).read_bytes()) if args.config else None)
    elif args.command == "market-ingest":
        from .market.cli import ingest_file
        result = ingest_file(args.input, args.database, args.run_id, decode(Path(args.config).read_bytes()), manifest_path=args.manifest,
                             follow=args.follow, seconds=args.seconds, simulate_clock=args.simulate_clock,
                             feature_config=decode(Path(args.feature_config).read_bytes()) if args.feature_config else None)
    elif args.command == "verify-features":
        from .market.feature_replay import verify_feature_replay
        result = verify_feature_replay(decode(Path(args.manifest).read_bytes()), target_run_id=args.run_id, speed=args.speed)
    elif args.command == "signal-scenario":
        from .strategy.scenario import run_scenario
        result = run_scenario(args.scenario, output_dir=args.output_dir)
    elif args.command == "oms-scenario":
        from .execution.scenario import run_scenario
        result = run_scenario(args.scenario, args.output_dir)
    elif args.command == "market-profile-draft":
        from .market.field_run import make_profile, write_new_json
        result = make_profile(decode(Path(args.template).read_bytes()), args.code, args.trading_date)
        write_new_json(args.output, result)
    elif args.command == "review-market-profile":
        from .market.verification import profile_review, load_json
        result = profile_review(load_json(args.profile))
        exit_code = 0 if result["ready_for_field_validation"] else 2
    elif args.command == "audit-market-session":
        from .market.verification import audit_session
        from .market.field_run import write_new_json
        result = audit_session(args.directory, fsync_budget_ms=args.fsync_budget_ms, previous_directory=args.previous_session)
        write_new_json(args.output, result)
        exit_code = 0 if result["field_checks_passed"] else 2
    elif args.command == "market-field-run":
        from .market.field_run import run_field_session
        result = run_field_session(args.bridge_python, decode(Path(args.profile).read_bytes()), args.output_dir,
                                   seconds=args.seconds, max_market_events=args.max_market_events, bootstrap_tr=not args.without_initial_tr,
                                   fsync_budget_ms=args.fsync_budget_ms, previous_directory=args.previous_session)
        exit_code = 0 if result["field_checks_passed"] else 2
    else:
        session = ReplaySession(decode(Path(args.manifest).read_bytes()))
        result = {"trace": session.play(target_run_id=args.run_id), "trace_sha256": session.trace_digest(target_run_id=args.run_id)}
    print(encode(result).decode())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
