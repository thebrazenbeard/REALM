"""Source-only tests for in-transaction UTC follow-up event scheduling."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.clock_dispatch import schedule_in_transaction
from realm.gateway import Gateway
from realm.world_clock import DueEvent, WorldClock


UTC = timezone.utc
BASE = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)


class ClockDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "world.sqlite3"
        Gateway(self.path, world_id="first_world")
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def test_followup_event_is_atomic_with_source_tick_and_processes_next_tick(self):
        self.clock.schedule("start", due_at=BASE, kind="host", payload={})
        def handler(db, event):
            if event.kind == "host":
                made = schedule_in_transaction(
                    self.clock, db, "followup",
                    due_at=BASE + timedelta(seconds=7),
                    kind="host_followup", payload={"actor": "npc1"})
                self.assertIsInstance(made, DueEvent)
                self.assertEqual("followup", made.event_id)
            else:
                db.execute("INSERT INTO world_meta(key,value) VALUES ('test_result','arrived')")
        first = self.clock.tick(BASE, handler)
        self.assertEqual(["start"], [e.event_id for e in first])
        self.assertIsNone(self.clock.lookup("followup").processed_at)
        self.assertEqual([], self.clock.tick(BASE + timedelta(seconds=6), handler))
        self.assertEqual(["followup"], [e.event_id for e in self.clock.tick(
            BASE + timedelta(seconds=7), handler)])
        with closing(self._db()) as db:
            self.assertEqual("arrived", db.execute(
                "SELECT value FROM world_meta WHERE key='test_result'").fetchone()[0])

    def test_callback_failure_rolls_back_new_event_and_original_receipt(self):
        self.clock.schedule("start", due_at=BASE, kind="host", payload={})
        def fails(db, _event):
            schedule_in_transaction(self.clock, db, "rolled_back",
                                    due_at=BASE + timedelta(seconds=1),
                                    kind="host", payload={"x": 1})
            raise RuntimeError("synthetic failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            self.clock.tick(BASE, fails)
        self.assertIsNone(self.clock.lookup("rolled_back"))
        self.assertIsNone(self.clock.lookup("start").processed_at)
        self.assertEqual(["start"], [e.event_id for e in self.clock.tick(
            BASE, lambda db, e: None)])

    def test_same_id_is_idempotent_and_conflicting_payload_rejected(self):
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            a = schedule_in_transaction(self.clock, db, "once",
                                        due_at=BASE, kind="npc", payload={"x": 1})
            b = schedule_in_transaction(self.clock, db, "once",
                                        due_at=BASE, kind="npc", payload={"x": 1})
            self.assertEqual(a, b)
            with self.assertRaisesRegex(ValueError, "different"):
                schedule_in_transaction(self.clock, db, "once",
                                        due_at=BASE, kind="npc", payload={"x": 2})
            db.commit()
        self.assertEqual({"x": 1}, self.clock.lookup("once").payload)

    def test_wrong_world_connection_is_rejected_before_mutation(self):
        other = Path(self.tmp.name) / "other.sqlite3"
        Gateway(other, world_id="second_world")
        WorldClock(other).enable()
        with closing(sqlite3.connect(other)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "different world"):
                schedule_in_transaction(self.clock, db, "wrong",
                                        due_at=BASE, kind="npc", payload={})
            db.commit()
        self.assertIsNone(self.clock.lookup("wrong"))
        self.assertIsNone(WorldClock(other).lookup("wrong"))

    def test_requires_existing_transaction_and_exact_connection_properties(self):
        with closing(self._db()) as db:
            with self.assertRaisesRegex(RuntimeError, "transaction"):
                schedule_in_transaction(self.clock, db, "no_tx",
                                        due_at=BASE, kind="npc", payload={})
            db.row_factory = None
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "row_factory"):
                schedule_in_transaction(self.clock, db, "bad_row",
                                        due_at=BASE, kind="npc", payload={})
            db.rollback()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "foreign keys"):
                schedule_in_transaction(self.clock, db, "bad_fk",
                                        due_at=BASE, kind="npc", payload={})
            db.rollback()
        self.assertIsNone(self.clock.lookup("no_tx"))
        self.assertIsNone(self.clock.lookup("bad_row"))
        self.assertIsNone(self.clock.lookup("bad_fk"))

    def test_validation_equivalence_and_no_side_effect(self):
        invalid = [
            ("../escape", BASE, "npc", {}),
            ("valid", BASE.replace(tzinfo=None), "npc", {}),
            ("valid", BASE, "../kind", {}),
            ("valid", BASE, "npc", {"bad": float("nan")}),
            ("valid", BASE, "npc", {"bad": "\ud800"}),
            ("valid", BASE, "npc", {"x": "a" * 5000}),
        ]
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            for event_id, due, kind, payload in invalid:
                with self.subTest(event_id=event_id, kind=kind, payload=repr(payload)[:30]):
                    with self.assertRaises(ValueError):
                        schedule_in_transaction(self.clock, db, event_id,
                                                due_at=due, kind=kind, payload=payload)
            db.commit()
        with closing(self._db()) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM clock_events").fetchone()[0])

    def test_fake_clock_events_without_clock_state_cannot_schedule(self):
        foreign = Path(self.tmp.name) / "not_enabled.sqlite3"
        Gateway(foreign, world_id="not_enabled")
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("""CREATE TABLE clock_events (
                event_id TEXT PRIMARY KEY, due_utc TEXT NOT NULL,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                processed_utc TEXT)""")
            db.commit()
        clock = WorldClock(foreign)  # enable() deliberately never called
        with closing(sqlite3.connect(foreign)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "enabled"):
                schedule_in_transaction(clock, db, "forged",
                                        due_at=BASE, kind="npc", payload={})
            db.commit()
            count = db.execute("SELECT COUNT(*) FROM clock_events").fetchone()[0]
            self.assertEqual(0, count)

    def test_failed_event_insert_rolls_back_its_trigger_side_effects_even_if_caught(self):
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TEMP TRIGGER fail_followup BEFORE INSERT ON clock_events
                BEGIN
                    INSERT INTO world_meta(key,value) VALUES('side_effect','MUTATED');
                    SELECT RAISE(FAIL, 'synthetic event failure');
                END""")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic event failure"):
                schedule_in_transaction(self.clock, db, "trapped",
                                        due_at=BASE, kind="npc", payload={})
            self.assertTrue(db.in_transaction)
            db.execute("INSERT INTO world_meta(key,value) VALUES('caller_survived','yes')")
            db.commit()  # caller deliberately swallows the error
        with closing(self._db()) as check:
            self.assertIsNone(check.execute(
                "SELECT value FROM world_meta WHERE key='side_effect'").fetchone())
            self.assertEqual("yes", check.execute(
                "SELECT value FROM world_meta WHERE key='caller_survived'").fetchone()[0])
            self.assertEqual(0, check.execute(
                "SELECT COUNT(*) FROM clock_events WHERE event_id='trapped'").fetchone()[0])

    def test_temp_clock_events_shadow_cannot_steal_persistent_arrival(self):
        # A same-connection TEMP schema with identical columns must not receive
        # events that the independent WorldClock consumer will never see.
        with closing(self._db()) as db:
            db.execute("""CREATE TEMP TABLE clock_events (
                event_id TEXT PRIMARY KEY, due_utc TEXT NOT NULL,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                processed_utc TEXT)""")
            db.execute("BEGIN IMMEDIATE")
            schedule_in_transaction(self.clock, db, "persistent-arrival",
                                    due_at=BASE, kind="ship_arrival", payload={"ship": "vesta"})
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM main.clock_events WHERE event_id='persistent-arrival'"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM temp.clock_events"
            ).fetchone()[0])
            db.commit()
        self.assertIsNotNone(self.clock.lookup("persistent-arrival"))

    def test_temp_world_meta_and_clock_state_cannot_mask_main_bound_world(self):
        with closing(self._db()) as db:
            db.execute("CREATE TEMP TABLE world_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TEMP TABLE clock_state (
                id INTEGER PRIMARY KEY CHECK(id = 1), last_utc TEXT NOT NULL)""")
            db.execute("BEGIN IMMEDIATE")
            schedule_in_transaction(self.clock, db, "trusted-main",
                                    due_at=BASE, kind="test", payload={})
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM main.clock_events WHERE event_id='trusted-main'"
            ).fetchone()[0])
            db.commit()
        self.assertIsNotNone(self.clock.lookup("trusted-main"))

    def test_same_connection_followup_survives_clock_recreation(self):
        self.clock.schedule("origin", due_at=BASE, kind="npc", payload={})
        def host(db, _event):
            schedule_in_transaction(self.clock, db, "future", due_at=BASE+timedelta(hours=1),
                                    kind="weather", payload={"rain": True})
        self.clock.tick(BASE, host)
        restarted = WorldClock(self.path)
        self.assertEqual({"rain": True}, restarted.lookup("future").payload)
        self.assertEqual("future", restarted.tick(
            BASE+timedelta(hours=1), lambda db,e: None)[0].event_id)


if __name__ == "__main__":
    unittest.main()
