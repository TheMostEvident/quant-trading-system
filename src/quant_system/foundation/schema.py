"""Private storage schema, versioned independently of the message contract."""

STORAGE_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS journal(
 ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs,
 kind TEXT NOT NULL, ready_us INTEGER NOT NULL, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS refs(
 ref_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs, kind TEXT NOT NULL,
 seq INTEGER NOT NULL REFERENCES journal, ready_us INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS blobs(
 sha256 TEXT PRIMARY KEY, byte_length INTEGER NOT NULL, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS captures(
 capture_id TEXT PRIMARY KEY REFERENCES refs, seq INTEGER UNIQUE NOT NULL REFERENCES journal,
 run_id TEXT NOT NULL REFERENCES runs, sha256 TEXT NOT NULL REFERENCES blobs,
 metadata BLOB NOT NULL, producer_id TEXT NOT NULL, epoch TEXT NOT NULL,
 local_sequence INTEGER NOT NULL, monotonic_ns INTEGER NOT NULL,
 UNIQUE(run_id,producer_id,epoch,local_sequence));
CREATE TABLE IF NOT EXISTS epochs(
 run_id TEXT NOT NULL REFERENCES runs, producer_id TEXT NOT NULL, epoch TEXT NOT NULL,
 seq INTEGER NOT NULL REFERENCES journal, PRIMARY KEY(run_id,producer_id,epoch));
CREATE TABLE IF NOT EXISTS artifacts(
 artifact_id TEXT PRIMARY KEY REFERENCES refs, sha256 TEXT NOT NULL, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
 message_id TEXT PRIMARY KEY REFERENCES refs, seq INTEGER UNIQUE NOT NULL REFERENCES journal,
 run_id TEXT NOT NULL REFERENCES runs, ready_us INTEGER NOT NULL,
 capture_id TEXT NOT NULL REFERENCES captures, sha256 TEXT NOT NULL, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS identities(
 run_id TEXT NOT NULL, namespace TEXT NOT NULL, identity_key TEXT NOT NULL,
 raw_sha256 TEXT NOT NULL, semantic_sha256 TEXT NOT NULL,
 message_id TEXT NOT NULL REFERENCES messages, PRIMARY KEY(run_id,namespace,identity_key));
CREATE TABLE IF NOT EXISTS outbox(
 consumer_id TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES messages,
 status TEXT NOT NULL, PRIMARY KEY(consumer_id,message_id));
CREATE TABLE IF NOT EXISTS deliveries(
 delivery_seq INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER UNIQUE NOT NULL REFERENCES journal,
 run_id TEXT NOT NULL REFERENCES runs, consumer_id TEXT NOT NULL,
 message_id TEXT NOT NULL REFERENCES messages, seen_us INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS delivery_consumer ON deliveries(run_id,consumer_id,delivery_seq);
CREATE TABLE IF NOT EXISTS owners(
 run_id TEXT NOT NULL, object_key TEXT NOT NULL, epoch TEXT NOT NULL, generation INTEGER NOT NULL,
 PRIMARY KEY(run_id,object_key,epoch), UNIQUE(run_id,object_key,generation));
CREATE TABLE IF NOT EXISTS versions(
 message_id TEXT PRIMARY KEY REFERENCES messages, run_id TEXT NOT NULL,
 object_key TEXT NOT NULL, owner_epoch TEXT NOT NULL, generation INTEGER NOT NULL,
 revision INTEGER NOT NULL, effective_us INTEGER NOT NULL, expires_us INTEGER,
 tombstone INTEGER NOT NULL, UNIQUE(run_id,object_key,generation,revision));
CREATE TABLE IF NOT EXISTS boundaries(
 decision_id TEXT PRIMARY KEY, data BLOB NOT NULL, seq INTEGER NOT NULL REFERENCES journal);
CREATE TABLE IF NOT EXISTS effects(
 run_id TEXT NOT NULL, consumer_id TEXT NOT NULL, effect_key TEXT NOT NULL,
 seq INTEGER NOT NULL REFERENCES journal, sha256 TEXT NOT NULL, data BLOB NOT NULL,
 PRIMARY KEY(run_id,consumer_id,effect_key));
CREATE TABLE IF NOT EXISTS projections(
 run_id TEXT NOT NULL, consumer_id TEXT NOT NULL, data BLOB NOT NULL,
 PRIMARY KEY(run_id,consumer_id));
CREATE TABLE IF NOT EXISTS health(
 seq INTEGER PRIMARY KEY REFERENCES journal, run_id TEXT NOT NULL REFERENCES runs,
 healthy INTEGER NOT NULL, ready_us INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manifests(
 manifest_id TEXT PRIMARY KEY, seq INTEGER NOT NULL REFERENCES journal,
 sha256 TEXT NOT NULL, data BLOB NOT NULL);
"""

# Operator tamper resistance is not claimed. Runtime writes to historical tables
# cannot UPDATE/DELETE accidentally; only reconstructible projections can change.
IMMUTABLE = ("runs", "journal", "refs", "blobs", "captures", "epochs", "artifacts",
             "messages", "identities", "deliveries", "owners", "versions",
             "boundaries", "effects", "health", "manifests")
