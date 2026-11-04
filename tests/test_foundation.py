from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from quant_system.contracts import validate_message
from quant_system.foundation.buffer import CaptureBuffer
from quant_system.foundation.environment import environment_report, local_path, require_runtime
from quant_system.foundation.errors import DataConflict, EnvironmentFailure, IntegrityFailure, StorageFailure, ValidationError, WriterBusy
from quant_system.foundation.models import Capture, Version
from quant_system.foundation.replay import ReplaySession, VirtualClock, build_manifest, referenced_ids
from quant_system.foundation.store import JournalStore
from quant_system.foundation.values import decode, digest, encode, timestamp, utc_us

FIXTURES = json.loads((Path(__file__).parent / "fixtures/contracts-v0.2.json").read_text(encoding="utf-8"))
BASE = utc_us("2026-11-04T00:00:00.000000Z")


def at(seconds=0):
    return timestamp(BASE + int(seconds * 1_000_000))


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.db"
        self.store = JournalStore(self.path)
        self.store.register_run("run", mode="SYNTHETIC_REPLAY", config={"policy": "fixed", "seed": 7}, code_version="test-v1")
        self.store.put_artifact("spec-fut-v1", "run", {"fixture_only": True}, ready_at=at(-100))
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def capture(self, *, body=b"same raw bytes", native="event", ready=1, epoch="epoch-1", producer="producer", scope="SIM/session", **changes):
        self.counter += 1
        return Capture(capture_id="capture-" + str(self.counter), run_id="run", producer_id=producer, epoch=epoch,
                       local_sequence=self.counter, source="fixture", stream="book", body=body,
                       received_at=at(ready), ready_at=at(ready), monotonic_ns=self.counter * 1_000_000,
                       native_id=native, identity_scope=scope if native is not None else None, **changes)

    def message(self, c, *, key=None, ready=None, kind="MarketSnapshot", payload_change=None):
        m = deepcopy(next(x for x in FIXTURES if x["message_type"] == kind))
        m.update(message_id=key or "message-" + c.capture_id, run_id="run", received_at=c.received_at,
                 ready_at=c.ready_at if ready is None else at(ready), event_at=at(0), lineage=[c.capture_id])
        if kind == "MarketSnapshot":
            m["payload"]["features"]["basis"] = []
        if payload_change:
            payload_change(m["payload"])
        return m

    def publish(self, *, native=None, ready=1, version=None, flags=(), **capture_args):
        c = self.capture(native=native, ready=ready, **capture_args)
        self.store.record_capture(c)
        m = self.message(c)
        m["quality_flags"] = list(flags)
        self.store.publish(c.capture_id, m, ["consumer"], version=version)
        return c, m

    def deliver(self, seconds=2, *, epoch="consumer-1", mono=None):
        return self.store.dispatch("run", "consumer", seen_at=at(seconds), consumer_epoch=epoch,
                                   monotonic_ns=int(seconds * 1e9) if mono is None else mono)

    def ack(self, message, seconds=2):
        self.store.acknowledge("consumer", message["message_id"], at=at(seconds))

    def boundary(self, seconds=10, key=None):
        return self.store.boundary("run", "consumer", at=at(seconds), decision_id=key or "decision-" + str(self.store.connection.execute("SELECT COUNT(*) FROM boundaries").fetchone()[0]))

    def count(self, table):
        return self.store.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def add_required_artifacts(self, messages):
        ids = set().union(*(referenced_ids(m) for m in messages)) - {m["message_id"] for m in messages}
        # Block anchors resolve to their immutable document rather than a fake
        # duplicate document. All remaining refs are explicitly synthetic seed.
        ids = {x.rsplit(":", 1)[0] if ":block-" in x else x for x in ids}
        for ref in ids:
            if not self.store.connection.execute("SELECT 1 FROM refs WHERE ref_id=?", (ref,)).fetchone():
                self.store.put_artifact(ref, "run", {"fixture_only": True, "ref_id": ref}, ready_at=at(-100))

    def test_F01_raw_bytes_and_callback_representation(self):
        c = self.capture(body=" 뉴스\n  A ".encode() + b"\x00\xff", representation="OCX_CALLBACK_FIELDS", media_type="application/x-ocx-fields")
        self.store.record_capture(c)
        self.assertEqual(self.store.raw(c.capture_id), c.body)
        self.assertEqual(self.store.connection.execute("SELECT byte_length FROM blobs").fetchone()[0], len(c.body))
        meta = decode(self.store.connection.execute("SELECT metadata FROM captures").fetchone()[0])
        self.assertEqual(meta["representation"], "OCX_CALLBACK_FIELDS")
        self.assertTrue(self.store.verify_integrity())

    def test_F02_same_source_event_two_captures_one_logical_event(self):
        a = self.capture(native="native-1"); b = self.capture(native="native-1")
        self.store.record_capture(a); self.store.record_capture(b)
        first = self.store.publish(a.capture_id, self.message(a), ["consumer"])
        second = self.store.publish(b.capture_id, self.message(b), ["consumer"])
        self.assertEqual(first, second)
        self.assertEqual((self.count("captures"), self.count("blobs"), self.count("messages"), self.count("outbox")), (2, 1, 1, 1))

    def test_F03_identical_content_different_events_stay_distinct(self):
        self.publish(native="trade-10"); self.publish(native="trade-11")
        self.assertEqual((self.count("captures"), self.count("blobs"), self.count("messages")), (2, 1, 2))

    def test_F04_source_identity_collision_is_audited(self):
        self.publish(native="id", body=b"original")
        c = self.capture(native="id", body=b"changed")
        self.store.record_capture(c)
        with self.assertRaises(DataConflict):
            self.store.publish(c.capture_id, self.message(c), ["consumer"])
        self.assertEqual(self.count("captures"), 2)
        self.assertEqual(self.count("messages"), 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM journal WHERE kind='CONFLICT'").fetchone()[0], 1)

    def test_F05_no_native_id_does_not_deduplicate_by_hash(self):
        self.publish(native=None); self.publish(native=None)
        self.assertEqual((self.count("blobs"), self.count("messages")), (1, 2))

    def test_F06_lost_ack_and_duplicate_fill_effect_are_idempotent(self):
        c, m = self.publish(native="fill-1")
        self.deliver(2)
        self.assertTrue(self.store.apply_once("run", "consumer", m["message_id"], "verified/account/order/fill-1", {"delta": "2"}, {"quantity": "2"}, at=at(2)))
        self.deliver(3)  # ACK was lost.
        self.assertFalse(self.store.apply_once("run", "consumer", m["message_id"], "verified/account/order/fill-1", {"delta": "2"}, {"quantity": "4"}, at=at(3)))
        self.assertEqual(self.count("effects"), 1)
        state = decode(self.store.connection.execute("SELECT data FROM projections").fetchone()[0])
        self.assertEqual(state["quantity"], "2")
        self.assertEqual(self.count("deliveries"), 2)
        self.ack(m, 3)
        self.assertEqual(self.deliver(4), [])

    def test_F07_transaction_failure_cannot_publish_half_an_outbox(self):
        c = self.capture(); self.store.record_capture(c)
        def fail(_stage):
            raise RuntimeError("injected before commit")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.store.publish(c.capture_id, self.message(c), ["consumer"], fault=fail)
        self.assertEqual((self.count("captures"), self.count("messages"), self.count("outbox")), (1, 0, 0))
        self.assertEqual(self.deliver(), [])
        self.assertTrue(self.store.verify_integrity())

    def test_F08_normalization_failure_keeps_raw_and_can_retry(self):
        c = self.capture(); self.store.record_capture(c)
        invalid = self.message(c); invalid["schema_version"] = "future"
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, invalid, ["consumer"])
        self.assertEqual(self.store.raw(c.capture_id), c.body)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM journal WHERE kind='NORMALIZE_ERROR'").fetchone()[0], 1)
        self.store.publish(c.capture_id, self.message(c), ["consumer"])
        self.assertEqual(self.count("messages"), 1)

    def test_F09_late_retroactive_correction_cannot_rewrite_past(self):
        _, first = self.publish(ready=0, version=Version("rule", 1, at(0)))
        self.deliver(0); self.ack(first, 0)
        before = self.boundary(9)
        _, correction = self.publish(ready=10, version=Version("rule", 2, at(0)))
        self.deliver(10)
        self.assertEqual(self.store.latest(before, "rule")["message_id"], first["message_id"])
        self.assertEqual(self.store.latest(self.boundary(9), "rule")["message_id"], first["message_id"])
        self.assertEqual(self.store.latest(self.boundary(10), "rule")["message_id"], correction["message_id"])

    def test_F10_announced_future_rule_waits_until_effective_time(self):
        _, old = self.publish(ready=0, version=Version("rule", 1, at(0)))
        self.deliver(0); self.ack(old, 0)
        _, future = self.publish(ready=20, version=Version("rule", 2, at(60)))
        self.deliver(20)
        self.assertEqual(self.store.latest(self.boundary(30), "rule")["message_id"], old["message_id"])
        self.assertEqual(self.store.latest(self.boundary(60), "rule")["message_id"], future["message_id"])

    def test_F11_ready_and_consumer_arrival_both_constrain_visibility(self):
        self.publish(ready=5)
        self.deliver(8)
        self.assertEqual(self.store.visible(self.boundary(4)), [])
        self.assertEqual(self.store.visible(self.boundary(7)), [])
        self.assertEqual(len(self.store.visible(self.boundary(8))), 1)

    def test_F12_watermark_and_delivery_cursor_exclude_backdated_inputs(self):
        _, original = self.publish(ready=1)
        self.deliver(2); self.ack(original, 2)
        first = self.boundary(10)
        _, pending = self.publish(ready=1)  # Old timestamps, but committed later.
        second = self.boundary(10)  # Already committed, not yet delivered.
        self.deliver(2)
        self.assertEqual([x["message_id"] for x in self.store.visible(first)], [original["message_id"]])
        self.assertEqual([x["message_id"] for x in self.store.visible(second)], [original["message_id"]])
        self.assertEqual(len(self.store.visible(self.boundary(10))), 2)
        with self.assertRaises(ValidationError):
            self.store.visible(replace(first, watermark=10**9))

    def test_F13_reader_snapshot_and_old_boundary_do_not_change(self):
        _, m = self.publish(ready=1); self.deliver(2); self.ack(m, 2)
        boundary = self.boundary(10)
        with self.store.read_snapshot() as reader:
            count = reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.publish(ready=1)
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0], count)
        self.assertEqual(len(self.store.visible(boundary)), 1)
        self.assertEqual(self.count("messages"), 2)

    def test_F14_gap_expiry_and_tombstone_do_not_resurrect_old_policy(self):
        _, good = self.publish(ready=0, version=Version("rule", 1, at(0)))
        self.deliver(0); self.ack(good, 0)
        _, bad = self.publish(ready=1, version=Version("rule", 2, at(1), at(5)), flags=["GAP"])
        self.deliver(2); self.ack(bad, 2)
        self.assertFalse(self.store.usable(self.boundary(2), "rule"))
        self.assertIsNone(self.store.latest(self.boundary(5), "rule"))
        self.publish(ready=6, version=Version("rule", 3, at(6), tombstone=True))
        self.deliver(6)
        self.assertIsNone(self.store.latest(self.boundary(6), "rule"))

    def test_F15_restart_epochs_reset_monotonic_basis_and_require_recovery(self):
        first, m = self.publish(ready=1, epoch="source-1")
        self.deliver(2, epoch="consumer-1", mono=9999); self.ack(m, 2)
        second = self.capture(ready=3, epoch="source-2")
        second = replace(second, local_sequence=0, monotonic_ns=1)
        self.store.record_capture(second)
        self.store.publish(second.capture_id, self.message(second), ["consumer"])
        self.deliver(4, epoch="consumer-2", mono=1)
        b = self.boundary(5)
        self.assertFalse(self.store.healthy_at(b))
        self.add_required_artifacts(self.store.visible(b))
        manifest = build_manifest(self.store, self.boundary(5))
        trace = ReplaySession(manifest).play(target_run_id="replay")
        self.assertIsNone(trace[1]["elapsed_ns"])
        with self.assertRaises(ValidationError):
            self.store.set_health("run", True, "skip", at=at(6))
        self.store.set_health("run", True, "reconciled", at=at(6), recovery_evidence={"snapshot": "verified"})
        self.assertTrue(self.store.healthy_at(self.boundary(6)))

    def test_F16_bounded_queue_and_actual_sqlite_full_are_not_silent(self):
        buffer = CaptureBuffer(max_items=1, max_bytes=100)
        a = self.capture(native="a"); b = self.capture(native="b")
        self.assertTrue(buffer.submit(a)); self.assertFalse(buffer.submit(b))
        self.assertFalse(buffer.healthy)
        self.assertEqual(buffer.dropped, 1)
        buffer.drain(self.store)
        self.assertFalse(self.store.healthy_at(self.boundary(3)))
        buffer.recover(self.store, "run", at=at(4), evidence={"feed_snapshot": "resynchronized"})
        self.assertTrue(buffer.healthy)
        self.store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        pages = self.store.connection.execute("PRAGMA page_count").fetchone()[0]
        self.store.connection.execute("PRAGMA max_page_count=" + str(pages))
        big = self.capture(body=os.urandom(512 * 1024), native="large")
        buffer = CaptureBuffer(max_bytes=1024 * 1024)
        buffer.submit(big)
        with self.assertRaises(StorageFailure):
            buffer.drain(self.store)
        self.assertFalse(buffer.healthy)
        self.assertFalse(self.store.storage_healthy)
        self.assertEqual(self.count("captures"), 1)
        self.assertTrue(self.store.verify_integrity())

    def test_F17_process_exit_during_transaction_and_lost_ack_recover(self):
        _, m = self.publish(ready=1); self.deliver(2)
        self.store.close()
        code = """
import os,sys
from quant_system.foundation.store import JournalStore
from quant_system.foundation.models import Capture
s=JournalStore(sys.argv[1])
c=Capture('crash-capture','run','crasher','e',0,'fixture','book',b'crash','2026-11-04T00:00:03.000000Z','2026-11-04T00:00:03.000000Z',0)
s.record_capture(c)
s.connection.execute('BEGIN IMMEDIATE')
s._journal('run','SHOULD_ROLLBACK','2026-11-04T00:00:04.000000Z',{'x':1})
os._exit(77)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 77, result.stderr)
        self.store = JournalStore(self.path)
        self.assertEqual(self.store.raw("crash-capture"), b"crash")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM journal WHERE kind='SHOULD_ROLLBACK'").fetchone()[0], 0)
        replay = self.deliver(5, epoch="consumer-restart", mono=1)
        self.assertEqual(replay[0]["message_id"], m["message_id"])
        self.assertEqual(self.count("deliveries"), 2)
        self.assertTrue(self.store.verify_integrity())

    def test_F18_manifest_virtual_clock_is_repeatable_independent_of_speed(self):
        _, one = self.publish(ready=1); self.deliver(2); self.ack(one, 2)
        _, two = self.publish(ready=3); self.deliver(4)
        boundary = self.boundary(5)
        self.add_required_artifacts(self.store.visible(boundary))
        boundary = self.boundary(5)
        manifest = build_manifest(self.store, boundary, seed=7)
        session = ReplaySession(decode(encode(manifest)))
        before = deepcopy(manifest)
        self.assertEqual(session.trace_digest(speed=1), session.trace_digest(speed=1000))
        trace = session.play(target_run_id="replay")
        self.assertEqual(trace[1]["elapsed_ns"], 2_000_000_000)
        self.assertEqual(trace[0]["virtual_utc"], at(2))
        self.assertEqual(manifest, before)
        self.assertEqual(trace[0]["message"]["run_id"], "replay")
        self.assertNotEqual(trace[0]["message"]["message_id"], one["message_id"])
        self.assertEqual(manifest["manifest_id"], build_manifest(self.store, boundary, seed=7)["manifest_id"])
        damaged = deepcopy(manifest); damaged["payload"]["seed"] = 9
        with self.assertRaises(IntegrityFailure):
            ReplaySession(damaged)

    def test_F19_replay_uses_stored_ai_result_without_network(self):
        c = self.capture(ready=1)
        self.store.record_capture(c)
        m = self.message(c, kind="EventPacket")
        self.add_required_artifacts([m])
        self.store.publish(c.capture_id, m, ["consumer"])
        self.deliver(2)
        manifest = build_manifest(self.store, self.boundary(3))
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("time.sleep", side_effect=AssertionError("wall clock forbidden")):
            result = ReplaySession(manifest).play(target_run_id="offline")
        actual = result[0]["message"]["payload"]["affected_targets"][0]
        expected = m["payload"]["affected_targets"][0]
        for key in ("relevance_noul", "direction_probabilities", "importance", "instrument_id", "underlying_id"):
            self.assertEqual(actual[key], expected[key])
        view = ReplaySession(manifest).reference_view(target_run_id="offline")
        self.assertIn(actual["mapping_ref"], view)

    def test_F20_runtime_profile_and_path_guards(self):
        with self.assertRaises(EnvironmentFailure):
            require_runtime((3, 51, 2))
        with self.assertRaises(EnvironmentFailure):
            local_path("//server/share/data.db")
        profile = self.store.profile()
        self.assertEqual(profile["journal_mode"], "wal")
        self.assertEqual(profile["synchronous"], 2)
        self.assertEqual(self.store.run("run")["storage_profile"]["synchronous"], 2)
        report = environment_report(probe_kiwoom=True)
        self.assertFalse(report["kiwoom"]["account_access_tested"])
        if os.name != "nt":
            self.assertEqual(report["kiwoom"]["status"], "UNAVAILABLE_ON_THIS_PLATFORM")

    def test_F21_online_backup_restore_and_digest_detection(self):
        c, m = self.publish(); self.deliver()
        destination = Path(self.tmp.name) / "backup.db"
        metadata = self.store.backup(destination)
        self.assertEqual(metadata["sha256"], digest(destination.read_bytes()))
        with JournalStore(destination, read_only=True) as restored:
            self.assertEqual(restored.raw(c.capture_id), c.body)
            self.assertEqual(restored.message(m["message_id"]), m)
            self.assertTrue(restored.verify_integrity())
        with self.assertRaises(ValidationError):
            self.store.backup(destination)
        # Tampering by an operator who disables guards is detectable, not
        # claimed impossible.
        self.store.connection.execute("DROP TRIGGER immutable_blobs_update")
        self.store.connection.execute("UPDATE blobs SET payload=x'00'")
        with self.assertRaises(IntegrityFailure):
            self.store.verify_integrity()

    def test_F22_all_seven_contracts_roundtrip_through_store_and_replay(self):
        messages = []
        for original in FIXTURES:
            m = deepcopy(original); m["run_id"] = "run"
            messages.append(m)
        self.add_required_artifacts(messages)
        pending = list(messages)
        while pending:
            progressed = False
            for m in pending[:]:
                if all(self.store.connection.execute("SELECT 1 FROM refs WHERE ref_id=?", (ref,)).fetchone() for ref in m["lineage"]):
                    c = self.capture(ready=0, producer=m["message_type"], native=None)
                    c = replace(c, received_at=m["received_at"], ready_at=m["received_at"])
                    self.store.record_capture(c)
                    self.store.publish(c.capture_id, m, ["consumer"])
                    messages_by_id = {x["message_id"]: x for x in messages}
                    self.assertEqual(self.store.message(m["message_id"]), messages_by_id[m["message_id"]])
                    pending.remove(m); progressed = True
            self.assertTrue(progressed, "Unresolvable fixture lineage")
        self.deliver(100)
        manifest = build_manifest(self.store, self.boundary(101))
        trace = ReplaySession(manifest).play(target_run_id="roundtrip")
        self.assertEqual({x["message"]["message_type"] for x in trace}, {x["message_type"] for x in FIXTURES})
        for row in trace:
            validate_message(row["message"])
            self.assertEqual(row["message"]["data_origin"], "SYNTHETIC")
        pos = next(x["message"] for x in trace if x["message"]["message_type"] == "PositionState")
        acct = next(x["message"] for x in trace if x["message"]["message_type"] == "AccountState")
        self.assertEqual(pos["payload"]["account_state_ref"], acct["message_id"])
        self.assertEqual(pos["payload"]["quantity_unit"], "CONTRACT")
        self.assertEqual(pos["payload"]["contract_multiplier"], "1000")

    def test_append_only_guards_and_capture_retransmission(self):
        c = self.capture(); seq = self.store.record_capture(c)
        self.assertEqual(seq, self.store.record_capture(c))
        with self.assertRaises(DataConflict):
            self.store.record_capture(replace(c, body=b"different"))
        for table in ("journal", "captures", "blobs", "runs"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute("DELETE FROM " + table)

    def test_single_writer_lease_readers_are_allowed(self):
        with self.assertRaises(WriterBusy):
            JournalStore(self.path)
        with JournalStore(self.path, read_only=True) as reader:
            self.assertEqual(reader.store_id, self.store.store_id)
        if os.name != "nt":
            alias = Path(self.tmp.name) / "alias.db"
            alias.symlink_to(self.path)
            with self.assertRaises(WriterBusy):
                JournalStore(alias)

    def test_scope_reuse_does_not_merge_different_native_id_domains(self):
        self.publish(native="same", scope="day-1")
        self.publish(native="same", scope="day-2")
        self.assertEqual(self.count("messages"), 2)

    def test_invalid_contract_does_not_leak_or_round_fractional_money(self):
        for original in FIXTURES:
            validate_message(original)
        bad = deepcopy(next(x for x in FIXTURES if x["message_type"] == "PositionState"))
        bad["payload"]["net_qty"] = "1.5"
        with self.assertRaises(ValidationError):
            validate_message(bad)
        cash = deepcopy(bad)
        cash["payload"].update(quantity_unit="BASE_ASSET", net_qty="0.001998")
        validate_message(cash)
        cash["payload"]["net_qty"] = 0.001998
        with self.assertRaises(ValidationError):
            validate_message(cash)
        with self.assertRaises(ValidationError):
            decode('{"x":1,"x":2}')
        policy = deepcopy(next(x for x in FIXTURES if x["message_type"] == "TradingPolicy"))
        policy["payload"]["target_position_qty"] = "2." + "0" * 40 + "1"
        with self.assertRaises(ValidationError):
            validate_message(policy)

    def test_owner_epochs_require_explicit_transition(self):
        _, m = self.publish(version=Version("rule", 1, at(0)))
        self.deliver(2); self.ack(m)
        with self.assertRaises(ValidationError):
            self.publish(ready=3, version=Version("rule", 1, at(3), owner_epoch="new"))
        _, new = self.publish(ready=4, version=Version("rule", 1, at(4), owner_epoch="new", owner_transition=True))
        self.deliver(5)
        self.assertEqual(self.store.latest(self.boundary(6), "rule")["message_id"], new["message_id"])

    def test_cross_run_lineage_and_synthetic_origin_are_rejected(self):
        c = self.capture(); self.store.record_capture(c)
        self.store.register_run("other", mode="SYNTHETIC_REPLAY", config={})
        self.store.put_artifact("other-ref", "other", {}, ready_at=at(0))
        m = self.message(c); m["lineage"].append("other-ref")
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, m)
        m = self.message(c); m["data_origin"] = "OBSERVED"
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, m)

    def test_virtual_clock_rejects_reversed_offset_within_epoch(self):
        clock = VirtualClock(); clock.advance("epoch", 1, 20)
        with self.assertRaises(IntegrityFailure):
            clock.advance("epoch", 2, 10)
        self.assertIsNone(clock.advance("next-epoch", 3, 1))

    def test_past_health_is_not_changed_by_a_future_storage_failure(self):
        before = self.boundary(1)
        self.store.storage_healthy = False
        self.assertTrue(self.store.healthy_at(before))
        with self.assertRaises(StorageFailure):
            self.deliver(2)

    def test_effect_cannot_use_future_ready_or_undelivered_input(self):
        _, m = self.publish(ready=5)
        self.deliver(8)
        with self.assertRaises(ValidationError):
            self.store.apply_once("run", "consumer", m["message_id"], "effect", {}, {}, at=at(7))

    def test_quality_change_is_not_silently_deduplicated(self):
        self.publish(native="id")
        with self.assertRaises(DataConflict):
            self.publish(native="id", flags=["GAP"])

    def test_consumer_clock_cannot_reverse_within_epoch(self):
        self.publish(); self.deliver(2, mono=100)
        with self.assertRaises(ValidationError):
            self.deliver(3, mono=10)

    def test_capture_clock_cannot_reverse_within_epoch(self):
        first = self.capture(); self.store.record_capture(first)
        second = self.capture()
        with self.assertRaises(ValidationError):
            self.store.record_capture(replace(second, monotonic_ns=0))

    def test_unrelated_existing_database_is_rejected_unchanged(self):
        path = Path(self.tmp.name) / "other.db"
        with sqlite3.connect(path) as other:
            other.execute("CREATE TABLE untouched(value TEXT)")
            other.execute("INSERT INTO untouched VALUES('preserve')")
        before = path.read_bytes()
        with self.assertRaises(ValidationError):
            JournalStore(path)
        self.assertEqual(path.read_bytes(), before)

    def test_normalization_failure_blocks_use_without_rewriting_old_health(self):
        self.publish(version=Version("book", 1, at(0)))
        self.deliver(2)
        before = self.boundary(2)
        c = self.capture(ready=3); self.store.record_capture(c)
        m = self.message(c)
        m["payload"]["book"]["bids"][0]["quantity"] = "-1"
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, m)
        self.assertTrue(self.store.usable(before, "book"))
        self.assertFalse(self.store.usable(self.boundary(4), "book"))

    def test_mixed_run_must_be_declared_explicitly(self):
        self.store.register_run("observed", mode="SHADOW", config={}, data_origin="OBSERVED")
        c = replace(self.capture(), run_id="observed")
        self.store.record_capture(c)
        m = self.message(c); m["run_id"] = "observed"
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, m)
        self.store.register_run("mixed", mode="SHADOW", config={}, data_origin="MIXED")
        self.store.put_artifact("mixed-spec", "mixed", {}, ready_at=at(0))
        c = replace(self.capture(), run_id="mixed")
        self.store.record_capture(c)
        m = self.message(c); m["run_id"] = "mixed"; m["payload"]["instrument_spec_ref"] = "mixed-spec"
        self.store.publish(c.capture_id, m)

    def test_policy_rejects_invalid_smoothing_and_hold_parameters(self):
        original = next(x for x in FIXTURES if x["message_type"] == "TradingPolicy")
        cases = [("hold_exit", "long_hold_obi", 2), ("hold_exit", "max_hold_ms", -1),
                 ("smoothing", "tau_ms", 0), ("entry", "obi_long", 0.1)]
        for group, key, value in cases:
            with self.subTest(group=group, key=key):
                m = deepcopy(original); m["payload"][group][key] = value
                with self.assertRaises(ValidationError):
                    validate_message(m)
        for alpha in (0, 1.1, float("nan")):
            m = deepcopy(original); m["payload"]["smoothing"] = {"method": "EVENT_EMA", "alpha": alpha}
            with self.assertRaises(ValidationError):
                validate_message(m)

    def test_unknown_margin_or_position_cannot_be_used(self):
        for kind, change in (("AccountState", lambda p: p["margin"].update(status="UNKNOWN")),
                             ("AccountState", lambda p: p["margin"].update(available_margin="-0." + "0" * 400 + "1")),
                             ("PositionState", lambda p: p.update(position_status="UNKNOWN"))):
            with self.subTest(kind=kind):
                c = self.capture(native=None); self.store.record_capture(c)
                m = self.message(c, kind=kind, payload_change=change)
                self.add_required_artifacts([m])
                self.store.publish(c.capture_id, m, ["consumer"], version=Version(c.capture_id, 2, at(0)))
                self.deliver(3)
                self.assertFalse(self.store.usable(self.boundary(4), c.capture_id))

    def test_payload_reference_cannot_be_missing_future_or_cross_run(self):
        c = self.capture(); self.store.record_capture(c)
        m = self.message(c); m["payload"]["instrument_spec_ref"] = "later-spec"
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, m)
        self.store.put_artifact("later-spec", "run", {}, ready_at=at(5))
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, m)
        self.store.register_run("another", mode="SYNTHETIC_REPLAY", config={})
        self.store.put_artifact("another-spec", "another", {}, ready_at=at(0))
        m["payload"]["instrument_spec_ref"] = "another-spec"
        with self.assertRaises(ValidationError):
            self.store.publish(c.capture_id, m)
        self.assertEqual(self.count("messages"), 0)

    def test_replay_preserves_health_at_each_delivery(self):
        _, one = self.publish(ready=1); self.deliver(2); self.ack(one, 2)
        self.store.set_health("run", False, "FEED_GAP", at=at(3))
        self.publish(ready=4); self.deliver(5)
        manifest = build_manifest(self.store, self.boundary(6))
        self.assertFalse(manifest["payload"]["healthy"])
        self.assertEqual([x["healthy"] for x in ReplaySession(manifest).play(target_run_id="history")], [True, False])

    def test_cli_demo_replay_integrity_and_no_overwrite(self):
        output = Path(self.tmp.name) / "demo"
        result = subprocess.run([sys.executable, "-m", "quant_system", "demo", "--output-dir", str(output)],
                                check=True, capture_output=True, text=True)
        summary = decode(result.stdout)
        self.assertEqual((summary["captures"], summary["messages"], summary["deliveries"], summary["business_effects"]), (2, 2, 3, 2))
        self.assertEqual((summary["past_book"], summary["latest_book"]), ("book-1", "book-2"))
        self.assertTrue(summary["speed_independent"])
        for name in ("history.db", "backup.db"):
            result = subprocess.run([sys.executable, "-m", "quant_system", "integrity", str(output / name)],
                                    check=True, capture_output=True, text=True)
            self.assertTrue(decode(result.stdout)["integrity"])
        result = subprocess.run([sys.executable, "-m", "quant_system", "replay", str(output / "manifest.json"), "--run-id", "demo-replay"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(decode(result.stdout)["trace_sha256"], summary["trace_sha256"])
        before = (output / "summary.json").read_bytes()
        result = subprocess.run([sys.executable, "-m", "quant_system", "demo", "--output-dir", str(output)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, (output / "summary.json").read_bytes())

    def test_checkpoint_requires_idle_owning_writer(self):
        self.publish()
        result = self.store.checkpoint()
        self.assertEqual(result["busy"], 0)
        self.assertEqual(result["log_frames"], result["checkpointed_frames"])
        with self.store.transaction(), self.assertRaises(ValidationError):
            self.store.checkpoint()
        with JournalStore(self.path, read_only=True) as reader, self.assertRaises(ValidationError):
            reader.checkpoint()

    def test_decision_inputs_are_explicit_subset_of_visible_messages(self):
        _, m = self.publish()
        with self.assertRaises(ValidationError):
            self.store.boundary("run", "consumer", at=at(2), decision_id="too-soon", input_refs=[m["message_id"]])
        self.deliver(2)
        boundary = self.store.boundary("run", "consumer", at=at(3), decision_id="chosen", input_refs=[m["message_id"]])
        self.assertEqual(build_manifest(self.store, boundary)["payload"]["input_refs"], [m["message_id"]])
        ordinary = self.boundary(3)
        self.assertEqual(build_manifest(self.store, ordinary)["payload"]["input_refs"], [])

    def test_version_metadata_cannot_override_policy_validity(self):
        c = self.capture(native=None); self.store.record_capture(c)
        m = self.message(c, kind="TradingPolicy")
        m["payload"].update(valid_from=at(10), expires_at=at(20))
        self.add_required_artifacts([m])
        owner = m["payload"]["provider_epoch"]
        bad_versions = [Version("policy", 1, at(0), owner_epoch=owner),
                        Version("policy", 2, at(10), owner_epoch=owner),
                        Version("policy", 1, at(10), at(30), owner_epoch=owner),
                        Version("policy", 1, at(10))]
        for version in bad_versions:
            with self.subTest(version=version), self.assertRaises(ValidationError):
                self.store.publish(c.capture_id, m, ["consumer"], version=version)
        self.store.publish(c.capture_id, m, ["consumer"], version=Version("policy", 1, at(10), owner_epoch=owner))
        self.deliver(2)
        self.assertIsNone(self.store.latest(self.boundary(5), "policy"))
        self.assertEqual(self.store.latest(self.boundary(11), "policy")["message_id"], m["message_id"])
        self.assertIsNone(self.store.latest(self.boundary(21), "policy"))


if __name__ == "__main__":
    unittest.main()
