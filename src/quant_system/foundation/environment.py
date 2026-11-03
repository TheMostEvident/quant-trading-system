from __future__ import annotations

import os
import platform
import sqlite3
import struct
import sys
from pathlib import Path

from .errors import EnvironmentFailure, WriterBusy

MIN_SQLITE = (3, 51, 3)


def require_runtime(version=None):
    actual = sqlite3.sqlite_version_info if version is None else tuple(version)
    if sys.version_info < (3, 12) or actual < MIN_SQLITE:
        raise EnvironmentFailure("Requires Python >=3.12 and SQLite >=3.51.3 for the WAL profile")


def local_path(path):
    raw = os.fspath(path)
    if raw.startswith(("//", "\\\\")) or "://" in raw:
        raise EnvironmentFailure("A local filesystem path is required; remote DB access is not supported")
    result = Path(raw).expanduser().resolve()
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(result.anchor)) == 4:
            raise EnvironmentFailure("Mapped network drives are not supported for WAL")
    return result


class WriterLease:
    """OS-managed lifetime lock. A stale file does not imply a stale lease."""

    def __init__(self, path):
        self.file = open(str(path) + ".writer.lock", "a+b")
        try:
            if self.file.tell() == 0:
                self.file.write(b"\0"); self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise WriterBusy("Another process owns the journal writer") from exc

    def close(self):
        if self.file.closed:
            return
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


def environment_report(*, probe_kiwoom=False):
    report = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "os": platform.system(), "process_bits": struct.calcsize("P") * 8,
              "wal_runtime_supported": sqlite3.sqlite_version_info >= MIN_SQLITE,
              "kiwoom": {"status": "NOT_PROBED", "account_access_tested": False,
                          "com_activation_tested": False, "event_loop_tested": False}}
    if probe_kiwoom:
        if os.name != "nt":
            report["kiwoom"]["status"] = "UNAVAILABLE_ON_THIS_PLATFORM"
        else:
            import winreg
            found = []
            for bits, flag in ((32, winreg.KEY_WOW64_32KEY), (64, winreg.KEY_WOW64_64KEY)):
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"KHOPENAPI.KHOpenAPICtrl.1\CLSID", 0, winreg.KEY_READ | flag) as key:
                        clsid = winreg.QueryValueEx(key, None)[0]
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "CLSID\\" + clsid + r"\InprocServer32", 0, winreg.KEY_READ | flag) as key:
                        module = winreg.QueryValueEx(key, None)[0]
                    found.append({"registry_view_bits": bits, "module_exists": Path(module.strip('"')).is_file()})
                except OSError:
                    continue
            report["kiwoom"].update({"status": "REGISTRATION_FOUND" if found else "NOT_REGISTERED", "registrations": found})
    return report
