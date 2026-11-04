"""Recompute features from saved normalized inputs and their raw receive clocks."""

import base64

from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.replay import ReplaySession, referenced_ids
from quant_system.foundation.values import decode, digest, encode, utc_us

from .features import FeatureClock, FeatureEngine, VERSION
from .feed import validate_frame
from .mapping import BOOK_TYPE, TRADE_TYPE


def verify_feature_replay(manifest, *, target_run_id="feature-replay", speed=1):
    session = ReplaySession(manifest)
    view = session.reference_view(target_run_id=target_run_id)
    definition = session.manifest["payload"]["run_configuration"]["config"].get("features")
    if not isinstance(definition, dict) or set(definition) != {"feature_config", "effective_max_gap_ms", "coverage", "source_depth"}:
        raise ValidationError("Manifest has no supported immutable feature definition")
    definitions = [(ref, record) for ref, record in view.items() if record["kind"] == "ARTIFACT" and record.get("data") == definition]
    if len(definitions) != 1:
        raise IntegrityFailure("Feature configuration artifact is missing or ambiguous")
    config_ref = definitions[0][0]
    engine = FeatureEngine(definition["feature_config"], config_ref=config_ref, max_gap_ms=definition["effective_max_gap_ms"],
                           coverage=definition["coverage"], source_depth=definition["source_depth"])
    if engine.definition() != definition:
        raise IntegrityFailure("Effective feature definition differs from the recorded definition")
    cutoff = session.manifest["payload"]["watermark"]
    messages = []
    journal_sequences = set()
    for ref, record in view.items():
        seq = record.get("journal_seq")
        if type(seq) is not int or not 1 <= seq <= cutoff or seq in journal_sequences:
            raise IntegrityFailure("Invalid provenance journal order")
        journal_sequences.add(seq)
        if type(record.get("ready_us")) is not int or record["ready_us"] > session.manifest["payload"]["as_of_us"]:
            raise IntegrityFailure("Provenance exceeds availability cutoff")
        if record["kind"] == "MESSAGE":
            if record["ready_us"] != utc_us(record["data"]["ready_at"]):
                raise IntegrityFailure("Recorded message readiness mismatch")
            messages.append((seq, ref, record))
    output = []
    books = trades = 0
    for seq, ref, record in sorted(messages):
        message = record["data"]
        if message["message_type"] != "MarketSnapshot":
            continue
        # All declared producer dependencies must already be committed/ready.
        for parent in referenced_ids(message):
            dependency = view.get(parent) or view.get(parent.rsplit(":", 1)[0])
            if dependency is None or dependency["journal_seq"] >= seq or dependency["ready_us"] > utc_us(message["ready_at"]):
                raise IntegrityFailure("Feature provenance refers to a future/unavailable input")
        payload = message["payload"]
        specification_record = view.get(payload["instrument_spec_ref"])
        if specification_record is None or specification_record["kind"] != "ARTIFACT":
            raise IntegrityFailure("Instrument specification not available for recomputation")
        binding = specification_record["data"]
        specification = binding.get("specification", binding)
        raw_refs = [parent for parent in message["lineage"] if view.get(parent, {}).get("kind") == "RAW"]
        if len(raw_refs) != 1:
            raise IntegrityFailure("A unique current callback is required")
        raw_ref = raw_refs[0]
        raw = base64.b64decode(view[raw_ref]["bytes_base64"], validate=True)
        clock = None
        try:
            frame = validate_frame(decode(raw))
            clock = FeatureClock(frame["producer_id"], frame["epoch"], frame["sequence"], frame["monotonic_ns"], raw_ref)
        except ValidationError:
            if payload["update_kind"] != "STATUS":
                raise IntegrityFailure("Market features have no valid raw callback clock")
        if payload["update_kind"] in ("BOOK", "TRADE"):
            expected_type = BOOK_TYPE if payload["update_kind"] == "BOOK" else TRADE_TYPE
            if frame["event"]["kind"] != "REAL" or frame["event"]["real_type"] != expected_type:
                raise IntegrityFailure("Callback type differs from its normalized observation")
            if frame["data_origin"] != message["data_origin"] or frame["event"]["code"] != binding.get("source_code"):
                raise IntegrityFailure("Callback origin/instrument differs from its binding")
        expected = engine.process(message, clock, specification)
        stored = payload["features"]
        if encode(expected) != encode(stored):
            raise IntegrityFailure("Stored features differ from causal recomputation: " + record["source_ref_id"])
        if expected is not None:
            if expected["feature_version"] != VERSION:
                raise ValidationError("Unsupported feature version")
            output.append({"source_message_id": record["source_ref_id"], "features": expected})
            books += payload["update_kind"] == "BOOK"
            trades += payload["update_kind"] == "TRADE"
    if not output:
        raise ValidationError("No computed feature observations to verify")
    trace = session.play(target_run_id=target_run_id, speed=speed)
    return {"verified": True, "book_features": books, "trade_features": trades, "feature_messages": len(output),
            "producer_messages": len(messages), "consumer_deliveries": len(trace), "target_run_id": target_run_id,
            "recomputed_sha256": digest(encode(output)), "trace_sha256": digest(encode(trace))}
