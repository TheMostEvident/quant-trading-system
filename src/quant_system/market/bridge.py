"""Windows collector shell. COM calls stay on the GUI thread; no order methods.

The portable controller is tested with a fake port. Actual QAxWidget/OCX and
the selected futures registration method require Extra Issue 1 (#33).
"""

import argparse
import json
import os
import platform
import struct
import sys
import time
import uuid
from datetime import datetime, timezone

from .mapping import FIDS, STATUS_TYPE
from .spool import CallbackSpool


class BridgeController:
    def __init__(self, port, sink, codes, *, producer_id="kiwoom-ocx", epoch=None, clock=None):
        if not codes or len(codes) > 10 or len(set(codes)) != len(codes) or any(not isinstance(c, str) or not c or not c.isascii() or not c.isalnum() for c in codes):
            raise ValueError("One to ten explicitly selected ASCII future codes required")
        self.port, self.sink, self.codes = port, sink, tuple(codes)
        self.producer_id, self.epoch = producer_id, epoch or str(uuid.uuid4())
        self.clock = clock or (lambda: (datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), time.monotonic_ns()))
        self.sequence = 0
        self.connected = False
        self.mock_confirmed = False
        self.real_counts = {name: 0 for name in FIDS}

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
        # Read only the server flag, never account lists, user IDs or balances.
        mock = error == 0 and self.port.server_kind() == "1"
        self._send({"kind": "CONNECTION", "connected": mock, "server": "MOCK" if mock else "UNKNOWN", "error_code": int(error) if error else (0 if mock else -1)})
        if not mock:
            raise RuntimeError("Mock login was not confirmed")
        self.port.register(self.codes, sorted(set(fid for fields in FIDS.values() for fid in fields)))
        self.connected = True
        self.mock_confirmed = True

    def on_real(self, code, real_type, raw_real_data):
        stamp = self.clock()  # Taken before GetCommRealData field extraction.
        if real_type not in FIDS or (real_type != STATUS_TYPE and code not in self.codes):
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

    def login(self):
        return self.control.dynamicCall("CommConnect()")

    def server_kind(self):
        return str(self.control.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", ""))

    def register(self, codes, fids):
        result = self.control.dynamicCall("SetRealReg(QString, QString, QString, QString)", "9000", ";".join(codes), ";".join(map(str, fids)), "0")
        if result != 0:
            raise RuntimeError("Real-time registration failed")
        result = self.control.dynamicCall("SetRealReg(QString, QString, QString, QString)", "9001", "", "215", "0")
        if result != 0:
            raise RuntimeError("Session registration failed")

    def real(self, code, fid):
        return self.control.dynamicCall("GetCommRealData(QString, int)", code, fid)

    def connected(self):
        return self.control.dynamicCall("GetConnectState()")

    def close(self):
        for screen in ("9000", "9001"):
            self.control.dynamicCall("DisconnectRealData(QString)", screen)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mock-login market-data collector; no account/order calls")
    parser.add_argument("--probe", action="store_true", help="Create OCX and run GUI loop without login")
    parser.add_argument("--output")
    parser.add_argument("--codes", nargs="+")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if args.seconds < 1 or args.seconds > 3600 or (not args.probe and (not args.output or not args.codes)):
        parser.error("Use --probe, or --output and --codes; seconds must be 1..3600")
    if os.name != "nt":
        parser.error("OCX collection requires Windows; use synthetic ingestion elsewhere")
    report = {"os": platform.system(), "python": platform.python_version(), "process_bits": struct.calcsize("P") * 8,
              "com_created": False, "gui_loop_reached": False, "login_attempted": not args.probe,
              "real_feed_verified": False, "error": None}
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
        report["error"] = type(exc).__name__
        print(json.dumps(report, sort_keys=True))
        return 1
    spool = None if args.probe else CallbackSpool(args.output)
    port = QtPort(control)

    def guarded(callback):
        def call(*values):
            try:
                return callback(*values)
            except Exception as exc:
                report["error"] = type(exc).__name__
                app.exit(1)
        return call

    def entered():
        report["gui_loop_reached"] = True

    QTimer.singleShot(0, entered)
    timer = QTimer()
    if args.probe:
        QTimer.singleShot(100, app.quit)
    else:
        bridge = BridgeController(port, spool, args.codes)
        control.OnEventConnect.connect(guarded(bridge.on_connect))
        control.OnReceiveRealData.connect(guarded(bridge.on_real))
        timer.timeout.connect(guarded(bridge.heartbeat))
        timer.start(500)
        QTimer.singleShot(0, guarded(bridge.login))
        QTimer.singleShot(args.seconds * 1000, app.quit)
    try:
        result = app.exec_()
    except KeyboardInterrupt:
        result = 130
        report["error"] = "INTERRUPTED"
    timer.stop()
    if spool:
        try:
            port.close()
            bridge._send({"kind": "CONNECTION", "connected": False, "server": "UNKNOWN", "error_code": 0})
        except Exception as exc:
            report["error"] = type(exc).__name__
            result = 1
        finally:
            report["transport"] = spool.close()
        report["mock_login_confirmed"] = bridge.mock_confirmed
        report["real_callback_counts"] = bridge.real_counts
        if not bridge.mock_confirmed or not all(bridge.real_counts.values()):
            report["error"] = report["error"] or "INCOMPLETE_COLLECTION"
            result = result or 2
        if not report["transport"]["healthy"]:
            result = 1
    control.clear()
    print(json.dumps(report, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
