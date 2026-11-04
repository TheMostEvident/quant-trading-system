"""Run from a separate Windows/OCX-compatible interpreter, without the DB core."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from quant_system.market.bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
