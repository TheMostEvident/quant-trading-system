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
    args = parser.parse_args(argv)
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
    else:
        session = ReplaySession(decode(Path(args.manifest).read_bytes()))
        result = {"trace": session.play(target_run_id=args.run_id), "trace_sha256": session.trace_digest(target_run_id=args.run_id)}
    print(encode(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
