from __future__ import annotations

import base64
import math
from copy import deepcopy

from quant_system.contracts import validate_message

from .errors import IntegrityFailure, ValidationError
from .references import REFERENCE_KEYS, referenced_ids
from .values import decode, digest, encode, timestamp


def build_manifest(store, boundary, *, seed=0, max_bytes=64 * 1024 * 1024):
    if type(seed) is not int:
        raise ValidationError("Replay seed must be an integer")
    store._boundary(boundary)
    rows = store.connection.execute("""SELECT m.message_id,d.delivery_seq,d.seq,d.seen_us,j.data FROM messages m
        JOIN deliveries d ON d.message_id=m.message_id JOIN journal j ON j.ingest_seq=d.seq
        WHERE m.run_id=? AND d.consumer_id=? AND m.seq<=? AND d.seq<=? AND d.delivery_seq<=?
         AND m.ready_us<=? AND d.seen_us<=? ORDER BY d.delivery_seq""", store._filter(boundary)).fetchall()
    records = {}

    def include(ref_id):
        if ref_id in records:
            return ref_id
        row = store.connection.execute("SELECT * FROM refs WHERE ref_id=?", (ref_id,)).fetchone()
        if row is None and ":" in ref_id:
            # Document block/page anchors are not separate raw records.
            prefix = ref_id.rsplit(":", 1)[0]
            row = store.connection.execute("SELECT * FROM refs WHERE ref_id=?", (prefix,)).fetchone()
            if row:
                return include(prefix)
        if row is None or row["run_id"] != boundary.run_id or row["seq"] > boundary.watermark or row["ready_us"] > boundary.as_of_us:
            raise ValidationError("Unresolved or future manifest reference: " + ref_id)
        kind = row["kind"]
        if kind == "RAW":
            c = store.connection.execute("SELECT * FROM captures WHERE capture_id=?", (ref_id,)).fetchone()
            raw = store.raw(ref_id)
            records[ref_id] = {"kind": kind, "sha256": digest(raw), "bytes_base64": base64.b64encode(raw).decode("ascii"), "metadata": decode(c["metadata"])}
        elif kind == "ARTIFACT":
            a = store.connection.execute("SELECT * FROM artifacts WHERE artifact_id=?", (ref_id,)).fetchone()
            if digest(bytes(a["data"])) != a["sha256"]:
                raise IntegrityFailure("Artifact digest mismatch")
            records[ref_id] = {"kind": kind, "sha256": a["sha256"], "data": decode(a["data"])}
            for parent in referenced_ids(records[ref_id]["data"]):
                include(parent)
        else:
            m = store.message(ref_id)
            records[ref_id] = {"kind": kind, "sha256": digest(encode(m)), "data": m}
            journal = store.connection.execute("SELECT j.data FROM messages m JOIN journal j ON j.ingest_seq=m.seq WHERE m.message_id=?", (ref_id,)).fetchone()
            records[ref_id]["normalization_version"] = decode(journal[0])["normalization_version"]
            capture = store.connection.execute("SELECT capture_id FROM messages WHERE message_id=?", (ref_id,)).fetchone()[0]
            include(capture)
            for parent in referenced_ids(m):
                include(parent)
        return ref_id

    deliveries = []
    for row in rows:
        include(row["message_id"])
        event = decode(row["data"])
        deliveries.append({"message_id": row["message_id"], "delivery_seq": row["delivery_seq"],
                           "journal_seq": row["seq"],
                           "seen_us": row["seen_us"], "consumer_epoch": event["epoch"], "monotonic_ns": event["monotonic_ns"]})
    health_events = [dict(row) for row in store.connection.execute(
        "SELECT seq,ready_us,healthy,reason FROM health WHERE run_id=? AND seq<=? AND ready_us<=? ORDER BY seq",
        (boundary.run_id, boundary.watermark, boundary.as_of_us))]
    decision = store.connection.execute("SELECT j.data FROM boundaries b JOIN journal j ON j.ingest_seq=b.seq WHERE b.decision_id=?", (boundary.decision_id,)).fetchone()
    payload = {"store_id": store.store_id, "source_run_id": boundary.run_id,
               "consumer_id": boundary.consumer_id, "watermark": boundary.watermark,
               "decision_id": boundary.decision_id, "input_refs": decode(decision[0])["input_refs"],
               "delivery_cursor": boundary.delivery_cursor, "as_of_us": boundary.as_of_us,
               "run_configuration": store.run(boundary.run_id), "seed": seed,
               "encoding": "python-json-v1", "records": records, "deliveries": deliveries, "health_events": health_events,
               "healthy": store.healthy_at(boundary)}
    checksum = digest(encode(payload))
    manifest = {"manifest_version": 1, "manifest_id": "manifest:" + checksum, "sha256": checksum, "payload": payload}
    raw = encode(manifest)
    if len(raw) > max_bytes:
        raise ValidationError("Self-contained replay manifest exceeds configured byte limit")
    with store.transaction():
        old = store.connection.execute("SELECT data FROM manifests WHERE manifest_id=?", (manifest["manifest_id"],)).fetchone()
        if old and bytes(old[0]) != raw:
            raise IntegrityFailure("Manifest hash collision")
        if not old:
            seq = store._journal(boundary.run_id, "MANIFEST", timestamp(boundary.as_of_us), {"id": manifest["manifest_id"]})
            store.connection.execute("INSERT INTO manifests VALUES(?,?,?,?)", (manifest["manifest_id"], seq, digest(raw), raw))
    return manifest


def validate_manifest(manifest):
    if manifest.get("manifest_version") != 1:
        raise ValidationError("Unsupported replay manifest")
    payload = manifest["payload"]
    sha = digest(encode(payload))
    if manifest["sha256"] != sha or manifest["manifest_id"] != "manifest:" + sha:
        raise IntegrityFailure("Manifest digest mismatch")
    for ref_id, record in payload["records"].items():
        if record["kind"] == "RAW":
            try:
                raw = base64.b64decode(record["bytes_base64"], validate=True)
            except ValueError as exc:
                raise IntegrityFailure("Invalid raw encoding") from exc
        else:
            raw = encode(record["data"])
            if record["kind"] == "MESSAGE":
                validate_message(record["data"])
                if record["data"]["message_id"] != ref_id or record["data"]["run_id"] != payload["source_run_id"]:
                    raise IntegrityFailure("Manifest message identity mismatch")
                for parent in referenced_ids(record["data"]):
                    if parent not in payload["records"] and parent.rsplit(":", 1)[0] not in payload["records"]:
                        raise IntegrityFailure("Manifest is missing referenced provenance")
        if digest(raw) != record["sha256"]:
            raise IntegrityFailure("Manifest record digest mismatch")
    previous = -1
    for item in payload["deliveries"]:
        if item["delivery_seq"] <= previous or item["delivery_seq"] > payload["delivery_cursor"]:
            raise IntegrityFailure("Replay delivery order is not monotone")
        previous = item["delivery_seq"]
        record = payload["records"].get(item["message_id"])
        if record is None or record["kind"] != "MESSAGE":
            raise IntegrityFailure("Missing replay input")
        if item["journal_seq"] > payload["watermark"] or item["seen_us"] > payload["as_of_us"]:
            raise IntegrityFailure("Replay delivery exceeds its decision cutoff")
    previous = -1
    for event in payload["health_events"]:
        if event["seq"] <= previous or event["seq"] > payload["watermark"] or event["ready_us"] > payload["as_of_us"]:
            raise IntegrityFailure("Invalid replay health history")
        previous = event["seq"]


class VirtualClock:
    def __init__(self):
        self.epoch = None
        self.monotonic_ns = None
        self.utc_us = None

    def advance(self, epoch, utc, monotonic):
        delta = None
        if epoch == self.epoch:
            delta = monotonic - self.monotonic_ns
            if delta < 0:
                raise IntegrityFailure("Monotonic time reversed within one consumer epoch")
        self.epoch, self.utc_us, self.monotonic_ns = epoch, utc, monotonic
        return delta


class ReplaySession:
    """Offline input delivery only. No network, sleep, AI, strategy or broker hooks."""

    def __init__(self, manifest):
        self.manifest = deepcopy(manifest)
        validate_manifest(self.manifest)

    def reference_view(self, *, target_run_id):
        """Resolve the remapped refs without rewriting stored source bytes."""
        validate_manifest(self.manifest)
        if not isinstance(target_run_id, str) or not target_run_id:
            raise ValidationError("Replay run ID is required")
        p = self.manifest["payload"]
        mapping = {ref: target_run_id + ":" + digest(ref.encode())[:24] for ref in p["records"]}

        def remap(value, key=""):
            if isinstance(value, dict):
                return {k: remap(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [remap(v, key) for v in value]
            if isinstance(value, str) and (key.endswith("_ref") or key in REFERENCE_KEYS):
                if value in mapping:
                    return mapping[value]
                if ":" in value:
                    base, anchor = value.rsplit(":", 1)
                    if base in mapping:
                        return mapping[base] + ":" + anchor
            return value

        view = {}
        for source_id, record in p["records"].items():
            projected = deepcopy(record)
            projected["source_ref_id"] = source_id
            projected["source_sha256"] = record["sha256"]
            if "data" in record:
                projected["data"] = remap(deepcopy(record["data"]))
                if record["kind"] == "MESSAGE":
                    projected["data"]["message_id"] = mapping[source_id]
                    projected["data"]["run_id"] = target_run_id
                projected["sha256"] = digest(encode(projected["data"]))
            view[mapping[source_id]] = projected
        return view

    def play(self, *, target_run_id, speed=1):
        if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
            raise ValidationError("Replay speed must be positive and finite")
        view = self.reference_view(target_run_id=target_run_id)
        p = self.manifest["payload"]
        by_source = {r["source_ref_id"]: r for r in view.values()}
        clock = VirtualClock()
        output = []
        for delivery in p["deliveries"]:
            source_id = delivery["message_id"]
            m = deepcopy(by_source[source_id]["data"])
            delta = clock.advance(delivery["consumer_epoch"], delivery["seen_us"], delivery["monotonic_ns"])
            eligible_health = [h for h in p["health_events"] if h["seq"] <= delivery["journal_seq"] and h["ready_us"] <= delivery["seen_us"]]
            healthy = bool(eligible_health[-1]["healthy"]) if eligible_health else True
            output.append({"source_message_id": source_id, "message": m,
                           "virtual_utc": timestamp(clock.utc_us), "consumer_epoch": clock.epoch,
                           "elapsed_ns": delta, "healthy": healthy})
        # Speed only affects a potential external display. No wall-clock delays
        # or callbacks are executed by this deterministic data core.
        return output

    def trace_digest(self, *, target_run_id="replay", speed=1):
        return digest(encode(self.play(target_run_id=target_run_id, speed=speed)))
