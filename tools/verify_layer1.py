"""Run the reproducible synthetic L1 checks and retain test IDs/failure logs."""

import argparse
import io
import json
from pathlib import Path
import time
import unittest


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.outcome = {}

    def startTest(self, test):
        self.started = time.perf_counter_ns()
        super().startTest(test)

    def addSuccess(self, test):
        self.outcome[test.id()] = "PASS"
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.outcome[test.id()] = "FAIL"
        super().addFailure(test, err)

    def addError(self, test, err):
        self.outcome[test.id()] = "ERROR"
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.outcome[test.id()] = "SKIP"
        super().addSkip(test, reason)

    def stopTest(self, test):
        self.records.append({"id": test.id(), "status": self.outcome.get(test.id(), "REVIEW"), "seconds": (time.perf_counter_ns() - self.started) / 1000000000})
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_layer1.py")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordedResult).run(suite)
    report = {"scope": "synthetic L1 integration/reference/fault tests; no field acceptance", "tests": result.testsRun,
              "successful": result.wasSuccessful() and result.testsRun > 0 and not result.skipped,
              "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "cases": result.records,
              "field_validation": "PENDING_EXTRA_ISSUES_33_43_45"}
    (directory / "tests.log").write_text(stream.getvalue(), encoding="utf-8")
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, sort_keys=True))
    return 0 if report["successful"] else 2


if __name__ == "__main__": raise SystemExit(main())
