"""OMS event receipt, projection and command queue commit in one transaction.

Composes JournalStore's single writer/WAL/FULL and raw-first capture. Additional
tables have an independent version; foundation storage/message versions stay 1/0.2.
"""

from quant_system.foundation.errors import IntegrityFailure, StorageFailure, ValidationError
from quant_system.foundation.models import Capture, Version
from quant_system.foundation.values import decode, digest, encode, utc_us
from quant_system.strategy.engine import clock

from .engine import conflict_transition, fault, initial, transition
from .ledger import validate_config
from .snapshots import messages

SCHEMA = """
CREATE TABLE IF NOT EXISTS oms_meta(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT OR IGNORE INTO oms_meta VALUES(1);
CREATE TABLE IF NOT EXISTS oms_events(
 seq INTEGER PRIMARY KEY REFERENCES journal, run_id TEXT NOT NULL, account_id TEXT NOT NULL,
 event_id TEXT NOT NULL, input_sha TEXT NOT NULL, input BLOB NOT NULL, context BLOB NOT NULL,
 result BLOB NOT NULL, state_sha TEXT NOT NULL, capture_id TEXT NOT NULL REFERENCES captures,
 UNIQUE(run_id,account_id,event_id));
CREATE TABLE IF NOT EXISTS oms_projection(
 run_id TEXT NOT NULL, account_id TEXT NOT NULL, config_ref TEXT NOT NULL REFERENCES artifacts,
 revision INTEGER NOT NULL, last_event TEXT, state_sha TEXT NOT NULL, state BLOB NOT NULL,
 PRIMARY KEY(run_id,account_id));
"""


class ExecutionJournal:
    def __init__(self, store, config):
        if store.read_only:
            raise ValidationError("OMS requires the owning writer")
        self.store, self.config = store, validate_config(config)
        self.run_id, self.account_id = self.config["run_id"], self.config["account_id"]
        run = store.run(self.run_id)
        if run["mode"] != "SYNTHETIC_REPLAY" or run["data_origin"] != "SYNTHETIC":
            raise ValidationError("OMS currently executes only synthetic replay inputs")
        store.connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
        if [r[0] for r in store.connection.execute("SELECT version FROM oms_meta").fetchall()] != [1]:
            raise ValidationError("Unsupported OMS storage version")
        for op in ("UPDATE", "DELETE"):
            store.connection.execute(f"CREATE TRIGGER IF NOT EXISTS oms_immutable_{op.lower()} BEFORE {op} ON oms_events BEGIN SELECT RAISE(ABORT,'immutable OMS history'); END")
        self.config_ref = self.run_id + ":oms:" + self.account_id + ":config"
        row = self._row()
        self.reopened = row is not None
        if row:
            artifact = store.connection.execute("SELECT data FROM artifacts WHERE artifact_id=?", (self.config_ref,)).fetchone()
            if not artifact or bytes(artifact[0]) != encode(self.config):
                raise ValidationError("OMS configuration is immutable")
        else:
            at = self.config["ready_at"]; utc_us(at)
            store.put_artifact(self.config_ref, self.run_id, self.config, ready_at=at)
            for instrument, s in self.config["instruments"].items():
                artifact = self.config.get("source_bindings", {}).get(instrument, s)
                store.put_artifact(s["instrument_spec_ref"], self.run_id, artifact, ready_at=at)
            if self.config["ledger_model"] == "FUTURES_MARGIN":
                store.put_artifact(self.config["margin_rule_ref"], self.run_id, {"model": self.config["model"], "rates": {k: {x: s[x] for x in ("initial_per_contract", "maintenance_per_contract")} for k, s in self.config["instruments"].items()}}, ready_at=at)
            state = initial(self.config); raw = encode(state)
            with store.transaction():
                store.connection.execute("INSERT INTO oms_projection VALUES(?,?,?,?,?,?,?)", (self.run_id, self.account_id, self.config_ref, 0, None, digest(raw), raw))

    def _row(self):
        return self.store.connection.execute("SELECT * FROM oms_projection WHERE run_id=? AND account_id=?", (self.run_id, self.account_id)).fetchone()

    def state(self):
        row = self._row(); raw = bytes(row["state"])
        if digest(raw) != row["state_sha"]:
            raise IntegrityFailure("OMS projection digest mismatch")
        state = decode(raw)
        if self.reopened:
            fault(state, "JOURNAL_REOPEN_RECONCILIATION_REQUIRED")
        return state

    def apply(self, event, *, before_commit=None):
        if not self.store.storage_healthy:
            raise StorageFailure("Unhealthy store may not produce OMS commands")
        e = decode(encode(event))
        for key in ("event_id", "kind", "at", "clock", "data"):
            if key not in e:
                raise ValidationError("OMS input envelope incomplete")
        if not isinstance(e["event_id"], str) or not e["event_id"]:
            raise ValidationError("OMS event identity required")
        utc_us(e["at"]); clock(e["clock"])
        raw = encode(e); sha = digest(raw)
        existing = self.store.connection.execute("SELECT * FROM oms_events WHERE run_id=? AND account_id=? AND event_id=?", (self.run_id, self.account_id, e["event_id"])).fetchone()
        if existing and existing["input_sha"] == sha:
            if e["kind"] == "DISPATCH":
                return {"status": "ALREADY_CLAIMED", "reason": "DO_NOT_RESEND"}
            return decode(existing["result"])
        conflict = existing is not None
        receipt = e["event_id"] + (":conflict:" + sha if conflict else "")
        if conflict:
            prior_conflict = self.store.connection.execute("SELECT result FROM oms_events WHERE run_id=? AND account_id=? AND event_id=?", (self.run_id, self.account_id, receipt)).fetchone()
            if prior_conflict:
                return decode(prior_conflict[0])
        prefix = self.run_id + ":oms:" + self.account_id
        capture_id = prefix + ":raw:" + digest(encode(receipt))
        event_ref = prefix + ":event:" + digest(encode(receipt))
        if not self.store.connection.execute("SELECT 1 FROM captures WHERE capture_id=?", (capture_id,)).fetchone():
            producer = "oms/" + self.account_id
            seq = self.store.connection.execute("SELECT COALESCE(MAX(local_sequence),0)+1 FROM captures WHERE run_id=? AND producer_id=? AND epoch='oms-v1'", (self.run_id, producer)).fetchone()[0]
            self.store.record_capture(Capture(capture_id, self.run_id, producer, "oms-v1", seq, "oms-input", "commands-and-reports", raw,
                                             e["at"], e["at"], seq, media_type="application/json", representation="OMS_INPUT_JSON"))
        with self.store.transaction():
            old = self.state()
            health = self.store.connection.execute("SELECT healthy FROM health WHERE run_id=? ORDER BY seq DESC LIMIT 1", (self.run_id,)).fetchone()
            unhealthy = bool(health is not None and not health[0] and e["kind"] in ("TARGET", "DISPATCH"))
            if unhealthy:
                fault(old, "FOUNDATION_HEALTH_RECONCILIATION_REQUIRED")
            if conflict:
                new, result = conflict_transition(old, e, event_ref)
            else:
                new, result = transition(self.config, old, dict(e, event_ref=event_ref))
            data = {"input_ref": capture_id, "input_sha256": sha, "result_sha256": digest(encode(result)), "oms_revision": new["revision"]}
            artifact_bytes = encode(data)
            seq = self.store._journal(self.run_id, "ARTIFACT", e["at"], {"id": event_ref, "sha256": digest(artifact_bytes)})
            self.store._ref(event_ref, self.run_id, "ARTIFACT", seq, e["at"])
            self.store.connection.execute("INSERT INTO artifacts VALUES(?,?,?)", (event_ref, digest(artifact_bytes), artifact_bytes))
            new_raw = encode(new)
            context = {"reopened": self.reopened, "unhealthy": unhealthy, "conflict": conflict, "event_ref": event_ref}
            self.store.connection.execute("INSERT INTO oms_events VALUES(?,?,?,?,?,?,?,?,?,?)", (seq, self.run_id, self.account_id, receipt, sha, raw, encode(context), encode(result), digest(new_raw), capture_id))
            self.store.connection.execute("UPDATE oms_projection SET revision=?,last_event=?,state_sha=?,state=? WHERE run_id=? AND account_id=?", (new["revision"], receipt, digest(new_raw), new_raw, self.run_id, self.account_id))
            if before_commit:
                before_commit()
        self.reopened = False
        return result

    def snapshots(self):
        state = self.state(); row = self._row()
        if row["last_event"] is None:
            raise ValidationError("No committed OMS input to anchor snapshots")
        r = self.store.connection.execute("SELECT seq,capture_id FROM oms_events WHERE run_id=? AND account_id=? AND event_id=?", (self.run_id, self.account_id, row["last_event"])).fetchone()
        ref = self.store.connection.execute("SELECT ref_id FROM refs WHERE seq=?", (r["seq"],)).fetchone()[0]
        return messages(self.config, state, at=state["last_at"], event_ref=ref, config_ref=self.config_ref)

    def publish_snapshots(self, consumers=()):
        if self.reopened:
            raise ValidationError("Reopened OMS must reconcile before publishing a new version")
        state = self.state(); row = self._row()
        capture_id = self.store.connection.execute("SELECT capture_id FROM oms_events WHERE run_id=? AND account_id=? AND event_id=?", (self.run_id, self.account_id, row["last_event"])).fetchone()[0]
        output = self.snapshots()
        for m in output:
            p = m["payload"]; key = self.account_id + ":" + m["message_type"] + ":" + (m["instrument_id"] or "account")
            revision = p.get("account_version", p.get("position_version"))
            self.store.publish(capture_id, m, consumers, version=Version(key, revision, m["ready_at"]), normalization_version="synthetic-oms-v1")
        return output

    def submit_at_boundary(self, boundary, *, signal_ref, policy_ref, quote_ref, event):
        self.store._boundary(boundary)
        if boundary.run_id != self.run_id or utc_us(event["at"]) != boundary.as_of_us or not self.store.healthy_at(boundary):
            raise ValidationError("OMS decision must use its recorded boundary")
        visible = {m["message_id"]: m for m in self.store.visible(boundary)}
        if policy_ref not in visible or quote_ref not in visible:
            raise ValidationError("Policy/quote were not visible at t/W/D")
        row = self.store.connection.execute("SELECT r.*,a.data FROM refs r JOIN artifacts a ON a.artifact_id=r.ref_id WHERE r.ref_id=?", (signal_ref,)).fetchone()
        if not row or row["run_id"] != self.run_id or row["seq"] > boundary.watermark or row["ready_us"] > boundary.as_of_us:
            raise ValidationError("Signal artifact unavailable at boundary")
        signal = decode(row["data"])
        if not set(signal["input_refs"]) <= set(visible):
            raise ValidationError("Signal inputs unavailable at t/W/D")
        recorded = self.store.connection.execute("SELECT j.data FROM boundaries b JOIN journal j ON j.ingest_seq=b.seq WHERE b.decision_id=?", (boundary.decision_id,)).fetchone()
        if not set(signal["input_refs"]) | {policy_ref, quote_ref} <= set(decode(recorded[0])["input_refs"]):
            raise ValidationError("Inputs were not declared at the decision boundary")
        versions = self.store.connection.execute("SELECT * FROM versions WHERE message_id IN (?,?)", (policy_ref, quote_ref)).fetchall()
        by_ref = {r["message_id"]: r for r in versions}
        if set(by_ref) != {policy_ref, quote_ref}:
            raise ValidationError("Versioned policy/book objects required")
        latest_book = self.store.latest(boundary, by_ref[quote_ref]["object_key"])
        if latest_book is None or latest_book["message_id"] != quote_ref or not self.store.usable(boundary, by_ref[quote_ref]["object_key"]):
            raise ValidationError("Quote superseded, expired or invalidated at boundary")
        candidates = self.store.connection.execute("SELECT * FROM versions WHERE run_id=? AND object_key=? AND effective_us<=? ORDER BY generation DESC,revision DESC", (self.run_id, by_ref[policy_ref]["object_key"], boundary.as_of_us)).fetchall()
        latest = next((r for r in candidates if r["message_id"] in visible), None)
        if latest is None or latest["message_id"] != policy_ref or latest["tombstone"]:
            raise ValidationError("Policy superseded or withdrawn at boundary")
        d = dict(event["data"], signal=signal, policy=visible[policy_ref], quote=visible[quote_ref],
                 seen_at={key: event["at"] for key in (policy_ref, quote_ref)})
        return self.apply(dict(event, kind="TARGET", data=d, decision_boundary={name: getattr(boundary, name) for name in boundary.__dataclass_fields__}))

    def verify(self):
        rows = self.store.connection.execute("SELECT * FROM oms_events WHERE run_id=? AND account_id=? ORDER BY seq", (self.run_id, self.account_id)).fetchall()
        last_revision = 0
        state = initial(self.config)
        for row in rows:
            if digest(bytes(row["input"])) != row["input_sha"]:
                raise IntegrityFailure("OMS input history digest mismatch")
            context = decode(row["context"])
            if context["reopened"]:
                fault(state, "JOURNAL_REOPEN_RECONCILIATION_REQUIRED")
            if context["unhealthy"]:
                fault(state, "FOUNDATION_HEALTH_RECONCILIATION_REQUIRED")
            if context["conflict"]:
                state, result = conflict_transition(state, decode(row["input"]), context["event_ref"])
            else:
                state, result = transition(self.config, state, dict(decode(row["input"]), event_ref=context["event_ref"]))
            if digest(encode(state)) != row["state_sha"] or encode(result) != bytes(row["result"]):
                raise IntegrityFailure("OMS history differs from causal replay")
            last_revision += 1
        projection = self._row()
        if projection["revision"] != last_revision or (rows and projection["state_sha"] != rows[-1]["state_sha"]):
            raise IntegrityFailure("OMS projection/history mismatch")
        self.state()
        return {"events": len(rows), "revision": last_revision, "integrity": self.store.verify_integrity()}
