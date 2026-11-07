from pathlib import Path

from quant_system.foundation.errors import ValidationError
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode
from quant_system.market.field_run import write_new_json

from .adapters import config
from .collector import collect
from .transport import FixtureTransport, HTTPTransport


def run_collection(config_path, database, run_id, *, fixture=None, live=False, report=None):
    if (fixture is None) == (not live):
        raise ValidationError("Choose exactly one of --fixture and --live")
    c = config(decode(Path(config_path).read_bytes()))
    if report and Path(report).exists():
        raise ValidationError("Report path already exists")
    transport = FixtureTransport(fixture) if fixture else HTTPTransport(c["source"])
    with JournalStore(database) as store:
        result = collect(store, run_id, c, transport)
        store.verify_integrity()
    if report:
        write_new_json(report, result)
    return result
