from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from quant_system.contracts import validate_message

from .environment import WriterLease, environment_report, local_path, require_runtime
from .errors import DataConflict, IntegrityFailure, StorageFailure, ValidationError
from .models import Boundary, Capture, Version
from .references import referenced_ids
from .schema import IMMUTABLE, SCHEMA, STORAGE_VERSION
from .values import decimal_value, decode, digest, encode, now_utc, utc_us


class JournalStore:
    """One local writer; readers use independent WAL snapshot connections.

    This is persistence and delivery infrastructure, not a broker or a ledger
    that understands margin calculations. All writes are explicit transactions.
    """

    def __init__(self, path, *, read_only=False, max_raw_bytes=16 * 1024 * 1024):
        require_runtime()
        self.path = local_path(path)
        self.read_only = read_only
        self.max_raw_bytes = max_raw_bytes
        self.storage_healthy = True
        self._opened_ok = False
        self._lease = None
        self.connection = None
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._lease = WriterLease(self.path)
        try:
            self.connection = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rwc"), uri=True, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, STORAGE_VERSION) or (read_only and version != STORAGE_VERSION):
                raise ValidationError("Unsupported storage version")
            if version == 0 and self.connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
                raise ValidationError("Refusing to initialize an unrelated existing database")
            if not read_only:
                mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.execute("PRAGMA wal_autocheckpoint=0")
                if mode.lower() != "wal" or self.connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise StorageFailure("Durability profile could not be applied")
                self.connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + f"\nPRAGMA user_version={STORAGE_VERSION};\nCOMMIT;")
                for table in IMMUTABLE:
                    for operation in ("UPDATE", "DELETE"):
                        self.connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable history'); END")
                self.connection.execute("INSERT OR IGNORE INTO metadata VALUES('store_id',?)", (str(uuid.uuid4()),))
            self.store_id = self.connection.execute("SELECT value FROM metadata WHERE key='store_id'").fetchone()[0]
            if not read_only:
                clean = self.connection.execute("SELECT value FROM metadata WHERE key='writer_clean'").fetchone()
                with self.transaction():
                    self.connection.execute("INSERT INTO metadata VALUES('writer_clean','0') ON CONFLICT(key) DO UPDATE SET value='0'")
                    if clean and clean[0] != "1":
                        for run in self.connection.execute("SELECT run_id FROM runs").fetchall():
                            self._health(run[0], False, "UNCLEAN_WRITER_RESTART", now_utc())
            self._opened_ok = True
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.connection is not None:
            if self._opened_ok and not self.read_only and self.storage_healthy and not self.connection.in_transaction:
                self.connection.execute("UPDATE metadata SET value='1' WHERE key='writer_clean'")
            self.connection.close()
            self.connection = None
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise StorageFailure("Read-only store")
        if self.connection.in_transaction:
            raise StorageFailure("Nested transactions are not supported")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except BaseException as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            if isinstance(exc, sqlite3.Error):
                self.storage_healthy = False
                raise StorageFailure("SQLite transaction failed; no output may be dispatched") from exc
            raise

    def _journal(self, run_id, kind, ready_at, data):
        return self.connection.execute("INSERT INTO journal(run_id,kind,ready_us,data) VALUES(?,?,?,?)", (run_id, kind, utc_us(ready_at), encode(data))).lastrowid

    def _ref(self, ref_id, run_id, kind, seq, ready_at):
        self.connection.execute("INSERT INTO refs VALUES(?,?,?,?,?)", (ref_id, run_id, kind, seq, utc_us(ready_at)))

    def register_run(self, run_id, *, mode, config, data_origin="SYNTHETIC", code_version="unknown"):
        if not run_id or mode not in ("SYNTHETIC_REPLAY", "RECORDED_REPLAY", "SHADOW"):
            raise ValidationError("Unknown or unsafe run mode")
        if data_origin not in ("SYNTHETIC", "OBSERVED", "DERIVED", "MIXED"):
            raise ValidationError("Unknown origin")
        if mode == "SYNTHETIC_REPLAY" and data_origin != "SYNTHETIC":
            raise ValidationError("Synthetic replay must declare synthetic input")
        data = encode({"mode": mode, "config": config, "data_origin": data_origin, "code_version": code_version,
                       "encoding": "python-json-v1", "runtime": environment_report(), "storage_profile": self.profile()})
        with self.transaction():
            prior = self.connection.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if prior:
                if bytes(prior[0]) != data:
                    raise DataConflict("Run configuration is immutable")
                return
            self.connection.execute("INSERT INTO runs VALUES(?,?)", (run_id, data))
            self._journal(run_id, "RUN", now_utc(), decode(data))

    def run(self, run_id):
        row = self.connection.execute("SELECT data FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValidationError("Unknown run")
        return decode(row[0])

    def _health(self, run_id, healthy, reason, at):
        seq = self._journal(run_id, "HEALTH", at, {"healthy": bool(healthy), "reason": reason})
        self.connection.execute("INSERT INTO health VALUES(?,?,?,?,?)", (seq, run_id, int(healthy), utc_us(at), reason))

    def set_health(self, run_id, healthy, reason, *, at, recovery_evidence=None):
        if healthy and not recovery_evidence:
            raise ValidationError("Recovery requires explicit reconciliation evidence")
        with self.transaction():
            self._health(run_id, healthy, reason, at)
            if recovery_evidence:
                self._journal(run_id, "RECOVERY_EVIDENCE", at, recovery_evidence)

    def put_artifact(self, artifact_id, run_id, data, *, ready_at):
        raw = encode(data)
        with self.transaction():
            old = self.connection.execute("SELECT data FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if old:
                if bytes(old[0]) != raw:
                    raise DataConflict("Artifact ID reused with different content")
                return
            seq = self._journal(run_id, "ARTIFACT", ready_at, {"id": artifact_id, "sha256": digest(raw)})
            self._ref(artifact_id, run_id, "ARTIFACT", seq, ready_at)
            self.connection.execute("INSERT INTO artifacts VALUES(?,?,?)", (artifact_id, digest(raw), raw))

    def record_capture(self, capture: Capture):
        if not isinstance(capture.body, bytes) or len(capture.body) > self.max_raw_bytes:
            self.storage_healthy = False
            raise StorageFailure("Raw payload type/size violates the configured limit")
        for value in (capture.capture_id, capture.run_id, capture.producer_id, capture.epoch, capture.source, capture.stream):
            if not isinstance(value, str) or not value:
                raise ValidationError("Missing capture identity")
        if type(capture.local_sequence) is not int or capture.local_sequence < 0 or type(capture.monotonic_ns) is not int or capture.monotonic_ns < 0:
            raise ValidationError("Invalid capture counter or monotonic offset")
        if utc_us(capture.received_at) > utc_us(capture.ready_at):
            raise ValidationError("Capture is ready before collection")
        if capture.event_at is not None:
            utc_us(capture.event_at)
        if capture.native_id is not None and not capture.identity_scope:
            raise ValidationError("Native ID requires a verified identity scope")
        metadata = asdict(capture)
        metadata.pop("body")
        raw_hash = digest(capture.body)
        meta = encode(metadata)
        with self.transaction():
            old = self.connection.execute("SELECT seq,sha256,metadata FROM captures WHERE capture_id=?", (capture.capture_id,)).fetchone()
            if old:
                if old["sha256"] != raw_hash or bytes(old["metadata"]) != meta:
                    raise DataConflict("Capture ID collision")
                return old["seq"]
            progress = self.connection.execute("SELECT local_sequence,monotonic_ns FROM captures WHERE run_id=? AND producer_id=? AND epoch=? ORDER BY local_sequence DESC LIMIT 1", (capture.run_id, capture.producer_id, capture.epoch)).fetchone()
            if progress and (capture.local_sequence <= progress[0] or capture.monotonic_ns < progress[1]):
                raise ValidationError("Local capture counter/clock moved backwards within an epoch")
            epoch = self.connection.execute("SELECT 1 FROM epochs WHERE run_id=? AND producer_id=? AND epoch=?", (capture.run_id, capture.producer_id, capture.epoch)).fetchone()
            if not epoch:
                prior_epoch = self.connection.execute("SELECT 1 FROM epochs WHERE run_id=? AND producer_id=?", (capture.run_id, capture.producer_id)).fetchone()
                seq = self._journal(capture.run_id, "PRODUCER_EPOCH", capture.ready_at, {"producer": capture.producer_id, "epoch": capture.epoch})
                self.connection.execute("INSERT INTO epochs VALUES(?,?,?,?)", (capture.run_id, capture.producer_id, capture.epoch, seq))
                if prior_epoch:
                    self._health(capture.run_id, False, "PRODUCER_RESTART_REQUIRES_RECONCILIATION", capture.ready_at)
            self.connection.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (raw_hash, len(capture.body), capture.body))
            seq = self._journal(capture.run_id, "RAW", capture.ready_at, metadata | {"raw_sha256": raw_hash})
            self._ref(capture.capture_id, capture.run_id, "RAW", seq, capture.ready_at)
            self.connection.execute("INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,?)", (capture.capture_id, seq, capture.run_id, raw_hash, meta, capture.producer_id, capture.epoch, capture.local_sequence, capture.monotonic_ns))
            return seq

    def raw(self, capture_id):
        row = self.connection.execute("SELECT b.payload,b.sha256,b.byte_length FROM captures c JOIN blobs b ON b.sha256=c.sha256 WHERE capture_id=?", (capture_id,)).fetchone()
        if row is None:
            raise ValidationError("Unknown capture")
        raw = bytes(row["payload"])
        if digest(raw) != row["sha256"] or len(raw) != row["byte_length"]:
            raise IntegrityFailure("Raw bytes no longer match their digest")
        return raw

    def publish(self, capture_id, message, consumers=(), *, version: Version | None = None, normalization_version="v1", fault=None):
        if not isinstance(normalization_version, str) or not normalization_version:
            raise ValidationError("Normalization version is required")
        capture = self.connection.execute("SELECT * FROM captures WHERE capture_id=?", (capture_id,)).fetchone()
        if capture is None:
            raise ValidationError("Raw capture must be committed before normalization")
        try:
            validate_message(message)
        except (ValidationError, ValueError, TypeError, KeyError, IndexError) as exc:
            with self.transaction():
                ready = decode(capture["metadata"])["ready_at"]
                self._journal(capture["run_id"], "NORMALIZE_ERROR", ready, {"capture_id": capture_id, "reason": str(exc)})
                self._health(capture["run_id"], False, "NORMALIZATION_FAILURE", ready)
            raise ValidationError("Normalization failed; raw capture was retained") from exc
        if message["run_id"] != capture["run_id"]:
            raise ValidationError("Message and capture run mismatch")
        run = self.run(message["run_id"])
        if run["data_origin"] == "SYNTHETIC" and message["data_origin"] != "SYNTHETIC":
            raise ValidationError("Synthetic lineage cannot become observed data")
        if run["data_origin"] in ("OBSERVED", "DERIVED") and message["data_origin"] == "SYNTHETIC":
            raise ValidationError("Synthetic/observed mixtures must be explicitly declared MIXED")
        raw = encode(message)
        sem = digest(encode({k: message[k] for k in ("schema_version", "message_type", "instrument_id", "event_at", "data_origin", "quality_flags", "payload")}))
        meta = decode(capture["metadata"])
        identity = None if meta["native_id"] is None else encode([meta["source"], meta["stream"], meta["identity_scope"], meta["native_id"]]).decode()
        namespace = message["message_type"] + ":" + normalization_version
        conflict = None
        with self.transaction():
            old = self.connection.execute("SELECT sha256 FROM messages WHERE message_id=?", (message["message_id"],)).fetchone()
            if old:
                if old[0] != digest(raw):
                    conflict = "Message ID collision"
                else:
                    return message["message_id"]
            prior = None if identity is None else self.connection.execute("SELECT * FROM identities WHERE run_id=? AND namespace=? AND identity_key=?", (message["run_id"], namespace, identity)).fetchone()
            if prior and conflict is None:
                if prior["raw_sha256"] != capture["sha256"] or prior["semantic_sha256"] != sem:
                    conflict = "Same source event has conflicting content"
                else:
                    self._journal(message["run_id"], "DUPLICATE_EVENT", message["ready_at"], {"capture_id": capture_id, "message_id": prior["message_id"]})
                    return prior["message_id"]
            if conflict is not None:
                self._journal(message["run_id"], "CONFLICT", message["ready_at"], {"capture_id": capture_id, "reason": conflict})
                self._health(message["run_id"], False, "SOURCE_EVENT_CONFLICT", message["ready_at"])
            else:
                for parent in referenced_ids(message) | {capture_id}:
                    ref = self.connection.execute("SELECT * FROM refs WHERE ref_id=?", (parent,)).fetchone()
                    if ref is None and ":" in parent:
                        ref = self.connection.execute("SELECT * FROM refs WHERE ref_id=?", (parent.rsplit(":", 1)[0],)).fetchone()
                    if ref is None or ref["run_id"] != message["run_id"] or ref["ready_us"] > utc_us(message["ready_at"]):
                        raise ValidationError("Missing, cross-run or future lineage: " + parent)
                seq = self._journal(message["run_id"], "MESSAGE", message["ready_at"], {"message_id": message["message_id"], "sha256": digest(raw), "normalization_version": normalization_version})
                self._ref(message["message_id"], message["run_id"], "MESSAGE", seq, message["ready_at"])
                self.connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (message["message_id"], seq, message["run_id"], utc_us(message["ready_at"]), capture_id, digest(raw), raw))
                if version:
                    self._version(message, version)
                if identity is not None:
                    self.connection.execute("INSERT INTO identities VALUES(?,?,?,?,?,?)", (message["run_id"], namespace, identity, capture["sha256"], sem, message["message_id"]))
                for consumer in set(consumers):
                    if not isinstance(consumer, str) or not consumer:
                        raise ValidationError("Invalid consumer ID")
                    self.connection.execute("INSERT INTO outbox VALUES(?,?,'PENDING')", (consumer, message["message_id"]))
                if fault:
                    fault("before_message_commit")
        if conflict:
            raise DataConflict(conflict)
        return message["message_id"]

    def _version(self, message, v):
        if type(v.revision) is not int or v.revision < 1 or not v.object_key or not v.owner_epoch:
            raise ValidationError("Invalid object version")
        effective = utc_us(v.effective_at)
        payload = message["payload"]
        declared_revision = next((payload[k] for k in ("revision", "event_revision", "account_version", "position_version") if k in payload), None)
        if declared_revision is not None and declared_revision != v.revision:
            raise ValidationError("Stored revision differs from the message contract")
        if "valid_from" in payload and effective != utc_us(payload["valid_from"]):
            raise ValidationError("Stored effective time differs from the message contract")
        if message["message_type"] == "TradingPolicy" and v.owner_epoch != payload["provider_epoch"]:
            raise ValidationError("Policy owner epoch differs from its provider epoch")
        expires = v.expires_at or payload.get("expires_at")
        end = None if expires is None else utc_us(expires)
        if end is not None and payload.get("expires_at") is not None and end > utc_us(payload["expires_at"]):
            raise ValidationError("Storage cannot extend a message validity interval")
        if end is not None and end <= effective:
            raise ValidationError("Empty object validity")
        owner = self.connection.execute("SELECT generation FROM owners WHERE run_id=? AND object_key=? AND epoch=?", (message["run_id"], v.object_key, v.owner_epoch)).fetchone()
        if owner:
            generation = owner[0]
        else:
            latest = self.connection.execute("SELECT MAX(generation) FROM owners WHERE run_id=? AND object_key=?", (message["run_id"], v.object_key)).fetchone()[0]
            if latest is not None and not v.owner_transition:
                raise ValidationError("New owner epoch requires explicit transition")
            generation = 0 if latest is None else latest + 1
            self.connection.execute("INSERT INTO owners VALUES(?,?,?,?)", (message["run_id"], v.object_key, v.owner_epoch, generation))
        self.connection.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?)", (message["message_id"], message["run_id"], v.object_key, v.owner_epoch, generation, v.revision, effective, end, int(v.tombstone)))

    def message(self, message_id):
        row = self.connection.execute("SELECT sha256,data FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            raise ValidationError("Unknown message")
        if digest(bytes(row["data"])) != row["sha256"]:
            raise IntegrityFailure("Message digest mismatch")
        return decode(row["data"])

    def dispatch(self, run_id, consumer, *, seen_at, consumer_epoch, monotonic_ns, limit=100):
        if not self.storage_healthy:
            raise StorageFailure("Storage is unhealthy")
        if type(limit) is not int or limit < 1 or not consumer_epoch or type(monotonic_ns) is not int or monotonic_ns < 0:
            raise ValidationError("Invalid delivery context")
        result = []
        with self.transaction():
            previous = self.connection.execute("SELECT j.data FROM deliveries d JOIN journal j ON j.ingest_seq=d.seq WHERE d.run_id=? AND d.consumer_id=? ORDER BY d.delivery_seq DESC LIMIT 1", (run_id, consumer)).fetchone()
            if previous:
                prior = decode(previous[0])
                if prior["epoch"] == consumer_epoch and monotonic_ns < prior["monotonic_ns"]:
                    raise ValidationError("Consumer monotonic time reversed within epoch")
                if prior["epoch"] != consumer_epoch:
                    self._health(run_id, False, "CONSUMER_RESTART_REQUIRES_RECONCILIATION", seen_at)
            rows = self.connection.execute("SELECT m.* FROM outbox o JOIN messages m ON m.message_id=o.message_id WHERE o.consumer_id=? AND m.run_id=? AND o.status!='ACKED' ORDER BY m.seq LIMIT ?", (consumer, run_id, limit)).fetchall()
            for row in rows:
                info = {"consumer": consumer, "message_id": row["message_id"], "seen_at": seen_at, "epoch": consumer_epoch, "monotonic_ns": monotonic_ns}
                seq = self._journal(run_id, "DELIVERY", seen_at, info)
                cursor = self.connection.execute("INSERT INTO deliveries(seq,run_id,consumer_id,message_id,seen_us) VALUES(?,?,?,?,?)", (seq, run_id, consumer, row["message_id"], utc_us(seen_at))).lastrowid
                self.connection.execute("UPDATE outbox SET status='INFLIGHT' WHERE consumer_id=? AND message_id=?", (consumer, row["message_id"]))
                result.append(info | {"delivery_seq": cursor, "journal_seq": seq})
        return result

    def acknowledge(self, consumer, message_id, *, at):
        with self.transaction():
            row = self.connection.execute("SELECT m.run_id FROM outbox o JOIN messages m ON m.message_id=o.message_id WHERE o.consumer_id=? AND o.message_id=?", (consumer, message_id)).fetchone()
            if not row:
                raise ValidationError("Cannot acknowledge an unregistered delivery")
            sent = self.connection.execute("SELECT 1 FROM deliveries WHERE consumer_id=? AND message_id=?", (consumer, message_id)).fetchone()
            if not sent:
                raise ValidationError("Cannot acknowledge a message before delivery")
            self._journal(row[0], "ACK", at, {"consumer": consumer, "message_id": message_id})
            self.connection.execute("UPDATE outbox SET status='ACKED' WHERE consumer_id=? AND message_id=?", (consumer, message_id))

    def apply_once(self, run_id, consumer, message_id, effect_key, effect, new_state, *, at):
        if not effect_key:
            raise ValidationError("A verified business effect key is required")
        raw = encode(effect)
        with self.transaction():
            delivered = self.connection.execute("SELECT 1 FROM deliveries d JOIN messages m ON m.message_id=d.message_id WHERE d.run_id=? AND d.consumer_id=? AND d.message_id=? AND d.seen_us<=? AND m.ready_us<=?", (run_id, consumer, message_id, utc_us(at), utc_us(at))).fetchone()
            if not delivered:
                raise ValidationError("Effect input was not delivered")
            existing = self.connection.execute("SELECT sha256 FROM effects WHERE run_id=? AND consumer_id=? AND effect_key=?", (run_id, consumer, effect_key)).fetchone()
            if existing:
                if existing[0] != digest(raw):
                    raise DataConflict("Business effect key reused with conflicting effect")
                return False
            seq = self._journal(run_id, "EFFECT", at, {"key": effect_key, "message_id": message_id, "state": new_state})
            self.connection.execute("INSERT INTO effects VALUES(?,?,?,?,?,?)", (run_id, consumer, effect_key, seq, digest(raw), raw))
            self.connection.execute("INSERT INTO projections VALUES(?,?,?) ON CONFLICT(run_id,consumer_id) DO UPDATE SET data=excluded.data", (run_id, consumer, encode(new_state)))
            return True

    def boundary(self, run_id, consumer, *, at, decision_id, input_refs=()):
        self.run(run_id)
        if not isinstance(input_refs, (list, tuple)) or any(not isinstance(ref, str) for ref in input_refs):
            raise ValidationError("Decision inputs require an ordered list/tuple of message IDs")
        with self.transaction():
            if self.connection.execute("SELECT 1 FROM boundaries WHERE decision_id=?", (decision_id,)).fetchone():
                raise DataConflict("Decision ID is immutable")
            w = self.connection.execute("SELECT COALESCE(MAX(ingest_seq),0) FROM journal").fetchone()[0]
            d = self.connection.execute("SELECT COALESCE(MAX(delivery_seq),0) FROM deliveries WHERE run_id=? AND consumer_id=?", (run_id, consumer)).fetchone()[0]
            b = Boundary(self.store_id, run_id, consumer, utc_us(at), w, d, decision_id)
            data = asdict(b)
            data["visible_refs"] = [x["message_id"] for x in self.visible(b, verify=False)]
            data["input_refs"] = list(input_refs)
            if len(set(data["input_refs"])) != len(data["input_refs"]) or not set(data["input_refs"]) <= set(data["visible_refs"]):
                raise ValidationError("Decision inputs must be distinct messages visible at this boundary")
            seq = self._journal(run_id, "DECISION_BOUNDARY", at, data)
            self.connection.execute("INSERT INTO boundaries VALUES(?,?,?)", (decision_id, encode(asdict(b)), seq))
            return b

    def _boundary(self, b):
        row = self.connection.execute("SELECT data FROM boundaries WHERE decision_id=?", (b.decision_id,)).fetchone()
        if b.store_id != self.store_id or not row or bytes(row[0]) != encode(asdict(b)):
            raise ValidationError("Unknown or altered decision boundary")

    @staticmethod
    def _filter(b):
        return (b.run_id, b.consumer_id, b.watermark, b.watermark, b.delivery_cursor, b.as_of_us, b.as_of_us)

    def visible(self, b: Boundary, *, verify=True):
        if verify:
            self._boundary(b)
        rows = self.connection.execute("""SELECT m.data,MIN(d.delivery_seq) AS first_delivery FROM messages m
            JOIN deliveries d ON d.message_id=m.message_id
            WHERE m.run_id=? AND d.consumer_id=? AND m.seq<=? AND d.seq<=?
             AND d.delivery_seq<=? AND m.ready_us<=? AND d.seen_us<=?
            GROUP BY m.message_id ORDER BY first_delivery""", self._filter(b)).fetchall()
        return [decode(row["data"]) for row in rows]

    def latest(self, b: Boundary, object_key):
        self._boundary(b)
        row = self.connection.execute("""SELECT v.expires_us,v.tombstone,m.data FROM versions v
            JOIN messages m ON m.message_id=v.message_id JOIN deliveries d ON d.message_id=m.message_id
            WHERE m.run_id=? AND d.consumer_id=? AND m.seq<=? AND d.seq<=? AND d.delivery_seq<=?
             AND m.ready_us<=? AND d.seen_us<=? AND v.object_key=? AND v.effective_us<=?
            ORDER BY v.generation DESC,v.revision DESC LIMIT 1""", self._filter(b) + (object_key, b.as_of_us)).fetchone()
        # Do not filter expiry/tombstones before selecting the latest known
        # effective revision: that would resurrect an older usable policy.
        if not row or row["tombstone"] or (row["expires_us"] is not None and row["expires_us"] <= b.as_of_us):
            return None
        return decode(row["data"])

    def healthy_at(self, b):
        self._boundary(b)
        row = self.connection.execute("SELECT healthy FROM health WHERE run_id=? AND seq<=? AND ready_us<=? ORDER BY seq DESC LIMIT 1", (b.run_id, b.watermark, b.as_of_us)).fetchone()
        # Historical state cannot depend on a failure that happens after W.
        # Live callers separately inspect storage_healthy and CaptureBuffer.
        return row is None or bool(row[0])

    def usable(self, b, object_key):
        m = self.latest(b, object_key)
        if not self.healthy_at(b) or m is None:
            return False
        if set(m["quality_flags"]) & {"STALE", "GAP", "OUT_OF_ORDER", "INVALID_BOOK", "CLOCK_UNVERIFIED"}:
            return False
        p = m["payload"]
        if m["message_type"] == "MarketSnapshot":
            return p["market_status"] == "CONTINUOUS" and p["book"] is not None and p["book"]["sync_status"] == "SYNCED"
        if p.get("position_status") in ("UNKNOWN", "HALTED"):
            return False
        if m["message_type"] == "AccountState" and p["ledger_model"] == "FUTURES_MARGIN":
            if p["margin"] is None or p["margin"]["status"] != "NORMAL" or decimal_value(p["margin"]["available_margin"]) < 0:
                return False
        if p.get("status") in ("UNKNOWN", "RETRACTED") or p.get("reconciliation") in ("UNRECONCILED", "MISMATCH"):
            return False
        return True

    @contextmanager
    def read_snapshot(self):
        reader = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        reader.row_factory = sqlite3.Row
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT MAX(ingest_seq) FROM journal").fetchone()
            yield reader
        finally:
            reader.rollback()
            reader.close()

    def backup(self, destination):
        target = local_path(destination)
        if target == self.path or target.exists() or self.connection.in_transaction:
            raise ValidationError("Backup requires a fresh path outside an active transaction")
        target.parent.mkdir(parents=True, exist_ok=True)
        other = sqlite3.connect(target)
        try:
            self.connection.backup(other)
        finally:
            other.close()
        with JournalStore(target, read_only=True) as restored:
            restored.verify_integrity()
        return {"store_id": self.store_id, "sha256": digest(target.read_bytes()), "path": str(target)}

    def checkpoint(self, mode="PASSIVE"):
        if self.read_only or self.connection.in_transaction or mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValidationError("Checkpoint requires an idle owning writer and a supported mode")
        busy, log_frames, checkpointed = self.connection.execute("PRAGMA wal_checkpoint(" + mode + ")").fetchone()
        wal = Path(str(self.path) + "-wal")
        return {"busy": busy, "log_frames": log_frames, "checkpointed_frames": checkpointed,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0}

    def verify_integrity(self):
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or self.connection.execute("PRAGMA foreign_key_check").fetchone():
            raise IntegrityFailure("SQLite integrity or foreign key failure")
        for row in self.connection.execute("SELECT * FROM blobs"):
            if len(row["payload"]) != row["byte_length"] or digest(bytes(row["payload"])) != row["sha256"]:
                raise IntegrityFailure("Raw BLOB digest mismatch")
        for table in ("messages", "artifacts", "effects", "manifests"):
            for row in self.connection.execute(f"SELECT sha256,data FROM {table}"):
                if digest(bytes(row["data"])) != row["sha256"]:
                    raise IntegrityFailure(table + " digest mismatch")
        return True

    def profile(self):
        return {"sqlite": sqlite3.sqlite_version, "journal_mode": self.connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": self.connection.execute("PRAGMA synchronous").fetchone()[0], "store_id": self.store_id,
                "path": str(self.path), "filesystem_locality": "caller-asserted; UNC/mapped Windows network paths rejected"}
