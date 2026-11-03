from dataclasses import dataclass


@dataclass(frozen=True)
class Capture:
    capture_id: str
    run_id: str
    producer_id: str
    epoch: str
    local_sequence: int
    source: str
    stream: str
    body: bytes
    received_at: str
    ready_at: str
    monotonic_ns: int
    native_id: str | None = None
    identity_scope: str | None = None
    event_at: str | None = None
    media_type: str = "application/octet-stream"
    representation: str = "WIRE_BYTES"


@dataclass(frozen=True)
class Boundary:
    store_id: str
    run_id: str
    consumer_id: str
    as_of_us: int
    watermark: int
    delivery_cursor: int
    decision_id: str


@dataclass(frozen=True)
class Version:
    object_key: str
    revision: int
    effective_at: str
    expires_at: str | None = None
    tombstone: bool = False
    owner_epoch: str = "initial"
    owner_transition: bool = False
