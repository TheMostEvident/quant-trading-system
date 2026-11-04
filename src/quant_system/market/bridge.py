"""Windows collector shell. COM calls stay on the GUI thread; no order methods.

The portable controller is tested with a fake port. Actual QAxWidget/OCX and
the selected futures registration method require Extra Issue 1 (#33).
"""

import argparse
import hashlib
import json
import os
import platform
import struct
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .mapping import FIDS, STATUS_TYPE
from .spool import CallbackSpool
from .diagnostics import EventObservation, INITIAL_FIELDS, INITIAL_RQ, INITIAL_SCREEN, INITIAL_TR, collection_diagnosis


def _api_result(stage, method, value):
    # Return codes and the server-kind flag are safe; never stringify an
    # unexpected COM object or include arbitrary exception messages.
    safe = type(value) in (int, bool) or value is None or (type(value) is str and value in ("", "0", "1"))
    return {"stage": stage, "api_method": method,
            "return_value": value if safe else None, "return_type": type(value).__name__}


class BridgeFailure(RuntimeError):
    def __init__(self, details):
        self.details = dict(details)
        super().__init__("Bridge operation did not confirm success")


def _record_failure(report, error, stage):
    failure = {"error": type(error).__name__,
               "error_details": dict(error.details) if isinstance(error, BridgeFailure) else {"stage": stage}}
    if report.get("error") is None:
        report.update(failure)
    else:
        report.setdefault("secondary_errors", []).append(failure)


def _finish_report(report, destination, result):
    report["report_version"] = 1
    report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if destination:
        try:
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, sort_keys=True)
                output.write("\n")
        except OSError as exc:
            _record_failure(report, exc, "REPORT_WRITE")
            result = 1
    print(json.dumps(report, sort_keys=True))
    return result


class BridgeController:
    def __init__(self, port, sink, codes, *, producer_id="kiwoom-ocx", epoch=None, clock=None, bootstrap_tr=False):
        if not codes or len(codes) > 10 or len(set(codes)) != len(codes) or any(not isinstance(c, str) or not c or not c.isascii() or not c.isalnum() for c in codes):
            raise ValueError("One to ten explicitly selected ASCII future codes required")
        if type(bootstrap_tr) is not bool or (bootstrap_tr and len(codes) != 1):
            raise ValueError("Initial TR diagnosis requires exactly one selected future")
        self.port, self.sink, self.codes = port, sink, tuple(codes)
        self.producer_id, self.epoch = producer_id, epoch or str(uuid.uuid4())
        self.clock = clock or (lambda: (datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), time.monotonic_ns()))
        self.sequence = 0
        self.connected = False
        self.mock_confirmed = False
        self.login_error_code = None
        self.registration_confirmed = False
        self.real_counts = {name: 0 for name in FIDS}
        self.observations = EventObservation()
        self.initial_tr = {"enabled": bootstrap_tr, "requested": False, "response_count": 0,
                           "request_result": None, "response": None}

    def _send(self, event, stamp=None):
        received_at, monotonic = stamp or self.clock()
        self.sequence += 1
        frame = {"frame_version": 1, "producer_id": self.producer_id, "epoch": self.epoch, "sequence": self.sequence,
                 "received_at": received_at, "monotonic_ns": monotonic, "data_origin": "OBSERVED", "event": event}
        if not self.sink.submit(frame):
            raise RuntimeError("Callback transport failed; stop collection and reconcile")
        return frame

    def login(self):
        result = self.port.login()
        if result != 0:
            self.on_connect(int(result))
        return result

    def on_connect(self, error):
        self.connected = False
        self.registration_confirmed = False
        self.login_error_code = int(error)
        # Read only the server flag, never account lists, user IDs or balances.
        server_kind = self.port.server_kind() if error == 0 else None
        mock = error == 0 and server_kind == "1"
        self.mock_confirmed = mock
        self._send({"kind": "CONNECTION", "connected": mock, "server": "MOCK" if mock else "UNKNOWN", "error_code": int(error) if error else (0 if mock else -1)})
        if not mock:
            raise BridgeFailure(_api_result("MOCK_LOGIN", "OnEventConnect" if error else "KOA_Functions:GetServerGubun", error if error else server_kind))
        self.port.register(self.codes, sorted(set(fid for fields in FIDS.values() for fid in fields)))
        self.connected = True
        self.registration_confirmed = True
        if self.initial_tr["enabled"]:
            self.initial_tr["requested"] = True
            try:
                self.port.request_initial(self.codes[0])
            finally:
                self.initial_tr["request_result"] = self.port.initial_request_result

    def on_initial_tr(self, screen, request_name, tr_code, record_name, prev_next, data_length, error_code, message, supplemental):
        if not self.initial_tr["requested"] or (screen, request_name, tr_code.lower()) != (INITIAL_SCREEN, INITIAL_RQ, INITIAL_TR):
            return
        received_at, monotonic = self.clock()
        self.initial_tr["response_count"] += 1
        if self.initial_tr["response"] is None:
            numeric_error = None
            if type(error_code) is int:
                numeric_error = error_code
            elif isinstance(error_code, str) and len(error_code) <= 16 and error_code.lstrip("-").isdigit():
                numeric_error = int(error_code)
            if numeric_error not in (None, 0):
                self.initial_tr["response"] = {"received_at": received_at, "error_code": numeric_error, "diagnostic_only": True}
                raise BridgeFailure(_api_result("INITIAL_TR_RESPONSE", "OnReceiveTrData", numeric_error))
            fields = self.port.initial_fields(tr_code, record_name or request_name)
            if any(not isinstance(value, str) or len(value) > 4096 for value in fields.values()):
                raise RuntimeError("Unexpected initial quote field representation")
            # This can be a cached quote. It never creates a MarketSnapshot,
            # refreshes a book, or changes the session/feature state.
            self.initial_tr["response"] = {"received_at": received_at, "monotonic_ns": monotonic,
                                           "error_code": numeric_error, "fields": fields, "diagnostic_only": True}

    def on_real(self, code, real_type, raw_real_data):
        stamp = self.clock()  # Taken before GetCommRealData field extraction.
        self.observations.record(real_type, tr_requested=self.initial_tr["requested"], tr_received=bool(self.initial_tr["response_count"]))
        if real_type not in FIDS:
            self.observations.filtered_type += 1
            return None
        if real_type != STATUS_TYPE and code not in self.codes:
            self.observations.filtered_code += 1
            return None
        values = {str(fid): self.port.real(code, fid) for fid in FIDS[real_type]}
        if any(not isinstance(value, str) for value in values.values()) or not isinstance(raw_real_data, str):
            raise RuntimeError("OCX did not return the expected original strings")
        self.real_counts[real_type] += 1
        return self._send({"kind": "REAL", "code": code, "real_type": real_type, "fields": values, "raw_real_data": raw_real_data}, stamp)

    def heartbeat(self):
        if self.connected and self.port.connected() != 1:
            self.connected = False
            self._send({"kind": "CONNECTION", "connected": False, "server": "UNKNOWN", "error_code": -106})
        return self._send({"kind": "HEARTBEAT", "connected": self.connected})


class QtPort:
    def __init__(self, control):
        self.control = control
        self.registration_results = []
        self.initial_request_result = None

    def login(self):
        return self.control.dynamicCall("CommConnect()")

    def server_kind(self):
        return str(self.control.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", ""))

    def register(self, codes, fids):
        # The controller includes FID 215 in this instrument-scoped request.
        # Do not add an empty-code global request: the installed OCX rejected
        # it with -105. Global/futures status semantics still need verification.
        result = self.control.dynamicCall("SetRealReg(QString, QString, QString, QString)", "9000", ";".join(codes), ";".join(map(str, fids)), "0")
        details = _api_result("MARKET_REGISTRATION", "SetRealReg", result)
        details.update(screen="9000", registration_scope="SELECTED_INSTRUMENTS")
        self.registration_results.append(details)
        if result != 0:
            raise BridgeFailure(details)

    def real(self, code, fid):
        return self.control.dynamicCall("GetCommRealData(QString, int)", code, fid)

    def request_initial(self, code):
        self.control.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        result = self.control.dynamicCall("CommRqData(QString, QString, int, QString)", INITIAL_RQ, INITIAL_TR, 0, INITIAL_SCREEN)
        self.initial_request_result = _api_result("INITIAL_TR_REQUEST", "CommRqData", result)
        if type(result) is not int or result != 0:
            raise BridgeFailure(self.initial_request_result)

    def initial_fields(self, tr_code, record_name):
        return {field: self.control.dynamicCall("GetCommData(QString, QString, int, QString)", tr_code, record_name, 0, field)
                for field in INITIAL_FIELDS}

    def connected(self):
        return self.control.dynamicCall("GetConnectState()")

    def close(self):
        for screen in ("9000", "9001", INITIAL_SCREEN):
            self.control.dynamicCall("DisconnectRealData(QString)", screen)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mock-login market-data collector; no account/order calls")
    parser.add_argument("--probe", action="store_true", help="Create OCX and run GUI loop without login")
    parser.add_argument("--output")
    parser.add_argument("--codes", nargs="+")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--bootstrap-tr", action="store_true", help="Request one read-only OPT50001 quote after mock login")
    parser.add_argument("--report", help="Save final diagnostic JSON; destination must be new")
    parser.add_argument("--max-market-events", type=int, help="Bound the field sample; all required callback types are still checked")
    args = parser.parse_args(argv)
    if args.seconds < 1 or args.seconds > 3600 or (not args.probe and (not args.output or not args.codes)):
        parser.error("Use --probe, or --output and --codes; seconds must be 1..3600")
    if args.bootstrap_tr and (args.probe or len(args.codes or []) != 1):
        parser.error("--bootstrap-tr requires one --codes value and cannot be used with --probe")
    if args.report and Path(args.report).exists():
        parser.error("Report destination already exists")
    if args.max_market_events is not None and not 1 <= args.max_market_events <= 100000:
        parser.error("--max-market-events must be 1..100000")
    if os.name != "nt":
        parser.error("OCX collection requires Windows; use synthetic ingestion elsewhere")
    report = {"os": platform.system(), "python": platform.python_version(), "process_bits": struct.calcsize("P") * 8,
              "com_created": False, "gui_loop_reached": False, "login_attempted": not args.probe,
              "real_feed_verified": False, "error": None, "error_details": None}
    try:
        # Optional Windows-only dependency, outside the stdlib-only core install.
        from PyQt5.QAxContainer import QAxWidget
        from PyQt5.QtCore import QTimer
        from PyQt5.QtWidgets import QApplication
        app = QApplication([sys.argv[0]])
        control = QAxWidget()
        if not control.setControl("KHOPENAPI.KHOpenAPICtrl.1") or control.isNull():
            raise RuntimeError("OCX creation failed")
        report["com_created"] = True
    except Exception as exc:
        _record_failure(report, exc, "OCX_INITIALIZE")
        return _finish_report(report, args.report, 1)
    try:
        spool = None if args.probe else CallbackSpool(args.output)
    except Exception as exc:
        _record_failure(report, exc, "SPOOL_INITIALIZE")
        control.clear()
        return _finish_report(report, args.report, 1)
    port = QtPort(control)

    def guarded(callback, stage):
        def call(*values):
            try:
                return callback(*values)
            except Exception as exc:
                _record_failure(report, exc, stage)
                app.exit(1)
        return call

    def entered():
        report["gui_loop_reached"] = True

    QTimer.singleShot(0, entered)
    timer = QTimer()
    if args.probe:
        QTimer.singleShot(100, app.quit)
    else:
        bridge = BridgeController(port, spool, args.codes, bootstrap_tr=args.bootstrap_tr)
        control.OnEventConnect.connect(guarded(bridge.on_connect, "LOGIN_AND_REGISTER"))
        def market_callback(*values):
            bridge.on_real(*values)
            if args.max_market_events is not None and sum(bridge.real_counts.values()) >= args.max_market_events:
                app.quit()
        control.OnReceiveRealData.connect(guarded(market_callback, "COPY_MARKET_FIELDS"))
        if args.bootstrap_tr:
            control.OnReceiveTrData.connect(guarded(bridge.on_initial_tr, "INITIAL_TR_RESPONSE"))
        timer.timeout.connect(guarded(bridge.heartbeat, "HEARTBEAT"))
        timer.start(500)
        QTimer.singleShot(0, guarded(bridge.login, "LOGIN_REQUEST"))
        QTimer.singleShot(args.seconds * 1000, app.quit)
    try:
        result = app.exec_()
    except KeyboardInterrupt:
        result = 130
        if report["error"] is None:
            report.update(error="INTERRUPTED", error_details={"stage": "EVENT_LOOP"})
        else:
            _record_failure(report, KeyboardInterrupt(), "EVENT_LOOP")
    timer.stop()
    if spool:
        try:
            port.close()
            bridge._send({"kind": "CONNECTION", "connected": False, "server": "UNKNOWN", "error_code": 0})
        except Exception as exc:
            _record_failure(report, exc, "SHUTDOWN")
            result = 1
        finally:
            try:
                report["transport"] = spool.close()
            except Exception as exc:
                _record_failure(report, exc, "TRANSPORT_CLOSE")
                report["transport"] = {"healthy": False}
                result = 1
        report["mock_login_confirmed"] = bridge.mock_confirmed
        report["login_error_code"] = bridge.login_error_code
        report["registration_confirmed"] = bridge.registration_confirmed
        report["registration_results"] = port.registration_results
        report["real_callback_counts"] = bridge.real_counts
        report["observed_real_callbacks"] = bridge.observations.summary()
        report["initial_tr"] = bridge.initial_tr
        report["diagnosis"] = collection_diagnosis(mock=bridge.mock_confirmed, registered=bridge.registration_confirmed,
                                                  observations=report["observed_real_callbacks"], accepted=bridge.real_counts, initial_tr=bridge.initial_tr)
        if not bridge.mock_confirmed or not all(bridge.real_counts.values()):
            if report["error"] is None:
                report.update(error="INCOMPLETE_COLLECTION", error_details={"stage": "CALLBACK_REQUIREMENTS"})
            result = result or 2
        if not report["transport"]["healthy"]:
            result = 1
        report["capture_filename"] = Path(args.output).name
        try:
            with Path(args.output).open("rb") as source:
                report["capture_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as exc:
            _record_failure(report, exc, "CAPTURE_DIGEST")
            result = 1
    control.clear()
    return _finish_report(report, args.report, result)


if __name__ == "__main__":
    raise SystemExit(main())
