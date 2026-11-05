"""Raw-first collection receipts and immutable document observations.

Fetch/item artifacts and their identity index commit together; failed transactions
leave the previously committed raw entity available for inspection.
"""

from quant_system.foundation.errors import IntegrityFailure, ValidationError
from quant_system.foundation.models import Capture
from quant_system.foundation.values import decode, digest, encode, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_meta(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT OR IGNORE INTO collection_meta VALUES(1);
CREATE TABLE IF NOT EXISTS collection_fetches(
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts, run_id TEXT NOT NULL REFERENCES runs,
 capture_id TEXT NOT NULL UNIQUE REFERENCES captures);
CREATE TABLE IF NOT EXISTS collection_items(
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts, source TEXT NOT NULL,
 item_key TEXT NOT NULL, variant TEXT NOT NULL, origin TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS collection_identity ON collection_items(source,item_key,origin,variant);
"""


class CollectionJournal:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id
        if store.read_only:
            raise ValidationError("Collection requires the owning writer")
        self.origin = store.run(run_id)["data_origin"]
        store.connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
        if [r[0] for r in store.connection.execute("SELECT version FROM collection_meta")] != [1]:
            raise ValidationError("Unsupported collection storage version")
        for table in ("collection_meta", "collection_fetches", "collection_items"):
            for op in ("UPDATE", "DELETE"):
                store.connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable collection history'); END")

    def raw(self, req, response, sequence):
        ref = self.run_id + ":source:raw:" + str(sequence)
        self.store.record_capture(Capture(ref, self.run_id, "source-collector", "collection-v1", sequence,
            req.source, req.url, response.body, response.received_at, response.received_at, response.monotonic_ns,
            media_type=response.media_type, representation=response.representation))
        return ref

    def _artifact(self, ref, value, at):
        raw = encode(value)
        seq = self.store._journal(self.run_id, "ARTIFACT", at, {"id": ref, "sha256": digest(raw)})
        self.store._ref(ref, self.run_id, "ARTIFACT", seq, at)
        self.store.connection.execute("INSERT INTO artifacts VALUES(?,?,?)", (ref, digest(raw), raw))

    def receipt(self, c, req, response, capture_id, parsed, error, *, before_commit=None):
        # Available time is the collector's commit clock, never source publication.
        at = now_utc()
        if at < response.received_at:
            raise ValidationError("Collection clock moved backwards or fixture is in the future")
        ref = capture_id.replace(":raw:", ":fetch:")
        docs, statuses = [], []
        with self.store.transaction():
            for index, item in enumerate(parsed["documents"] if parsed else []):
                key = digest(encode([req.source, item["item_key"]]))
                variant = digest(encode(item))
                prior = self.store.connection.execute("SELECT a.artifact_id,a.data FROM collection_items i JOIN artifacts a USING(artifact_id) JOIN refs r ON r.ref_id=a.artifact_id WHERE i.source=? AND i.item_key=? AND i.origin=? ORDER BY r.seq LIMIT 1", (req.source, key, self.origin)).fetchone()
                same = self.store.connection.execute("SELECT a.artifact_id FROM collection_items i JOIN artifacts a USING(artifact_id) JOIN refs r ON r.ref_id=a.artifact_id WHERE i.source=? AND i.item_key=? AND i.origin=? AND i.variant=? ORDER BY r.seq LIMIT 1", (req.source, key, self.origin, variant)).fetchone()
                item_ref = ref + ":item:" + str(index)
                status = "REPEATED_VARIANT" if same else "NEW_VARIANT" if prior else "FIRST_OBSERVATION"
                value = {"schema": "source-document-v1", "source": req.source, "data_origin": self.origin,
                    "item_key_sha256": key, "variant_sha256": variant, "document": item,
                    "capture_ref": capture_id, "received_at": response.received_at, "ready_at": at,
                    "first_observation_ref": prior["artifact_id"] if prior else item_ref,
                    "first_received_at": decode(prior["data"])["first_received_at"] if prior else response.received_at,
                    "same_variant_ref": same[0] if same else None, "observation_status": status}
                self._artifact(item_ref, value, at)
                self.store.connection.execute("INSERT INTO collection_items VALUES(?,?,?,?,?)", (item_ref, req.source, key, variant, self.origin))
                docs.append(item_ref); statuses.append(status)
            receipt = {"schema": "source-fetch-v1", "config": c, "request": req.public(), "capture_ref": capture_id,
                       "status": response.status, "media_type": response.media_type, "error": error,
                       "representation": response.representation, "received_at": response.received_at, "ready_at": at,
                       "data_origin": self.origin, "raw_sha256": digest(response.body),
                       "parsed_sha256": digest(encode(parsed)) if parsed is not None else None,
                       "coverage": parsed["coverage"] if parsed is not None else None,
                       "document_refs": docs, "observation_statuses": statuses}
            self._artifact(ref, receipt, at)
            self.store.connection.execute("INSERT INTO collection_fetches VALUES(?,?,?)", (ref, self.run_id, capture_id))
            if before_commit:
                before_commit()
        return receipt


def verify_collection(store, run_id):
    """Independently reparse saved raw entities; never calls a source or publishes events."""
    from .adapters import config, parse, request
    store.verify_integrity()
    rows = store.connection.execute("SELECT a.data FROM collection_fetches f JOIN artifacts a USING(artifact_id) JOIN refs r ON r.ref_id=a.artifact_id WHERE f.run_id=? ORDER BY r.seq", (run_id,)).fetchall()
    if not rows:
        raise ValidationError("No committed collection receipts for this run")
    parsed_count = 0
    for row in rows:
        f = decode(row[0]); raw = store.raw(f["capture_ref"])
        if digest(raw) != f["raw_sha256"]:
            raise IntegrityFailure("Collection/raw digest mismatch")
        if f["parsed_sha256"] is None:
            continue
        c = config(f["config"]); req = request(c, f["request"]["cursor"])
        if req.public() != f["request"]:
            raise IntegrityFailure("Stored provider request does not match config")
        normalized = parse(c, req, raw, f["media_type"])
        if digest(encode(normalized)) != f["parsed_sha256"] or len(normalized["documents"]) != len(f["document_refs"]):
            raise IntegrityFailure("Collection normalization replay differs")
        for item, ref in zip(normalized["documents"], f["document_refs"]):
            saved = store.connection.execute("SELECT data FROM artifacts WHERE artifact_id=?", (ref,)).fetchone()
            if not saved or decode(saved[0])["document"] != item or decode(saved[0])["capture_ref"] != f["capture_ref"]:
                raise IntegrityFailure("Collection item lineage differs")
        parsed_count += 1
    return {"verified": True, "run_id": run_id, "fetches": len(rows), "reparsed_fetches": parsed_count,
            "live_access_verified": False, "meaning": "stored-byte replay only"}
