"""Synthetic integration inputs. Expected values are handwritten separately."""

from copy import deepcopy
from pathlib import Path

from quant_system.foundation.values import decode, timestamp, utc_us

ROOT = Path(__file__).resolve().parents[1]
BASE = utc_us("2026-11-05T00:00:00.000000Z")


def at(ms):
    return timestamp(BASE + ms * 1000)


def scenario():
    config = decode((ROOT / "config/oms.synthetic.json").read_bytes())
    config.update(run_id="layer1-synthetic", ready_at=at(0))
    policy = decode((ROOT / "config/static-policy.example.json").read_bytes())
    policy.update(run_id=config["run_id"], ready_at=at(0), received_at=at(0))
    policy["payload"].update(valid_from=at(0), expires_at=at(60000))
    profile = decode((ROOT / "config/kiwoom.synthetic.json").read_bytes())
    profile["instruments"][0]["trading_date"] = "2026-11-05"
    parameters = {"model": "synthetic-depth-highwater-v1", "data_origin": "SYNTHETIC", "order_latency_ms": 10, "cancel_latency_ms": 20,
                  "report_latency_ms": 5, "max_quote_age_ms": 1000, "participation": "1", "slippage_ticks": 0, "max_contracts_per_book": 100,
                  "costs": {"SIM:KR-FUT1": {"asset": "KRW", "per_contract": "20", "notional_rate": "0", "tax_rate": "0"}},
                  "contract_expiry": {"SIM:KR-FUT1": at(60000)}, "evidence": "Synthetic integration assumptions only"}
    value = {"scenario_version": "layer1-scenario-v1", "config": config, "market_profile": profile,
             "feature_config": {"feature_version": "microstructure-v1", "depth": 1, "weights": ["1"],
                                "smoothing": {"method": "EVENT_EMA", "tau_ms": None, "alpha": 1},
                                "window_ms": 1000, "max_observation_gap_ms": 2000, "max_window_events": 4096},
             "assumptions": parameters, "policies": {policy["instrument_id"]: policy}, "risk": {"max_abs_position_qty": "2"},
             "fee_reserve": {"asset": "KRW", "amount": "100"}, "clock_domain": {"producer_id": "layer1-source", "epoch": "v1"}, "events": []}
    sequence = 0

    def callback(ms, event):
        nonlocal sequence
        sequence += 1
        frame = {"frame_version": 1, "producer_id": "layer1-source", "epoch": "v1", "sequence": sequence,
                 "received_at": at(ms), "monotonic_ns": ms * 1000000, "data_origin": "SYNTHETIC", "event": event}
        value["events"].append({"kind": "CALLBACK", "ready_at": at(ms), "frame": frame})

    def book(ms, bid_qty, ask_qty):
        fields = {"21": "090000", "121": bid_qty, "125": ask_qty}
        for rank in range(1, 6):
            ask_tenths, bid_tenths = 1010 + rank - 1, 1009 - rank + 1
            fields[str(40 + rank)] = f"+{ask_tenths // 10}.{ask_tenths % 10}"
            fields[str(50 + rank)] = f"+{bid_tenths // 10}.{bid_tenths % 10}"
            fields[str(60 + rank)] = ask_qty if rank == 1 else "1"
            fields[str(70 + rank)] = bid_qty if rank == 1 else "1"
        callback(ms, {"kind": "REAL", "code": "SIMFUT1", "real_type": "선물호가잔량", "fields": fields, "raw_real_data": "synthetic top-five"})

    def timer(ms):
        value["events"].append({"kind": "TIMER", "at": at(ms)})

    callback(0, {"kind": "CONNECTION", "connected": True, "server": "MOCK", "error_code": 0})
    callback(10, {"kind": "REAL", "code": "", "real_type": "장시작시간", "fields": {"215": "3", "20": "090000", "214": "0"}, "raw_real_data": "synthetic status"})
    for ms in (100, 200, 300): book(ms, "9", "1")
    book(320, "18", "2"); timer(330)
    callback(335, {"kind": "REAL", "code": "SIMFUT1", "real_type": "선물시세", "fields": {"20": "090000", "10": "+101.0", "15": "1", "13": "1"}, "raw_real_data": "synthetic trade"})
    book(400, "6", "4"); book(500, "1", "9"); book(520, "2", "18"); timer(530)
    short = deepcopy(policy); short.update(message_id="policy-short", ready_at=at(540), received_at=at(540))
    short["payload"].update(revision=2, target_position_qty="-2", valid_from=at(540))
    value["events"].append({"kind": "POLICY", "at": at(540), "message": short})
    for ms in (600, 700, 800): book(ms, "1", "9")
    book(820, "2", "18"); timer(830)
    value["events"].append({"kind": "OMS", "at": at(840), "event_kind": "MARK", "instrument_id": "SIM:KR-FUT1", "data": {"price": "100"}})
    callback(900, {"kind": "CONNECTION", "connected": False, "server": "UNKNOWN", "error_code": 0})
    return value
