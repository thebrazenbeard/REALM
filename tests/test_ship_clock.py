"""REALM ship arrivals share the UTC clock's SQLite write transaction.

No player endpoint, daemon, automatic scheduling, or world lore is introduced.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway
from realm.geography import Geography
from realm.world_clock import WorldClock

UTC = timezone.utc
START = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
ARRIVAL = START + timedelta(seconds=50)


class ShipClockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        Gateway(self.path, world_id="ship_clock_test")
        self.geo = Geography(self.path)
        self.geo.add_place("universe", "Universe", "universe")
        self.geo.add_place("system", "System", "system", "universe")
        self.geo.add_place("earth", "Earth", "planet", "system")
        self.geo.add_place("moon", "Moon", "planet", "system")
        self.geo.link("earth", "moon", "ship", seconds=50)
        self.geo.register_ship("voyager", "Voyager", "Patrick", "earth")
        self.geo.depart("voyager", "moon", actor="Patrick",
                        now=int(START.timestamp()), action_id="depart-1")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.clock.schedule("ship_arrival_voyager", due_at=ARRIVAL,
                            kind="ship_arrival", payload={"ship_id": "voyager"})

    def _arrive(self, db, event):
        self.assertEqual("ship_arrival", event.kind)
        self.geo.arrive_in_transaction(
            db, event.payload["ship_id"],
            now=int(event.due_at.timestamp()), action_id=event.event_id,
        )
    def _counts(self):
        with closing(sqlite3.connect(self.path)) as db:
            arrived = db.execute(
                "SELECT COUNT(*) FROM geo_ship_events WHERE kind='arrive'"
            ).fetchone()[0]
            receipts = db.execute(
                "SELECT COUNT(*) FROM geo_ship_receipts WHERE actor='system'"
            ).fetchone()[0]
            return arrived, receipts

    def test_arrival_uses_clock_transaction_and_replay_is_single_effect(self):
        self.assertEqual([], self.clock.tick(ARRIVAL - timedelta(seconds=1), self._arrive))
        self.assertEqual("in_transit", self.geo.ship_status("voyager")["state"])
        self.assertEqual(["ship_arrival_voyager"],
                         [e.event_id for e in self.clock.tick(ARRIVAL, self._arrive)])
        self.assertEqual("moon", self.geo.ship_status("voyager")["planet"])
        self.assertEqual((1, 1), self._counts())
        self.assertEqual([], WorldClock(self.path).tick(ARRIVAL, self._arrive))
        self.assertEqual((1, 1), self._counts())

    def test_callback_failure_rolls_back_arrival_and_receipt(self):
        def fail_after_arrival(db, event):
            self._arrive(db, event)
            raise RuntimeError("simulated crash")
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.clock.tick(ARRIVAL, fail_after_arrival)
        self.assertEqual("in_transit", self.geo.ship_status("voyager")["state"])
        self.assertIsNone(self.clock.lookup("ship_arrival_voyager").processed_at)
        self.assertEqual((0, 0), self._counts())
        self.clock.tick(ARRIVAL, self._arrive)
        self.assertEqual((1, 1), self._counts())

    def test_clock_early_arrival_rejects_without_acknowledging_event(self):
        self.clock.schedule("too_early", due_at=START + timedelta(seconds=20),
                            kind="ship_arrival", payload={"ship_id": "voyager"})
        with self.assertRaisesRegex(ValueError, "not reached"):
            self.clock.tick(START + timedelta(seconds=20), self._arrive)
        self.assertIsNone(self.clock.lookup("too_early").processed_at)
        self.assertEqual((0, 0), self._counts())

    def test_shared_arrival_rejects_unrelated_world_connection(self):
        other = self.path.parent / "other.sqlite3"
        Gateway(other, world_id="another_world")
        another = Geography(other)
        another.add_place("other_universe", "Other Universe", "universe")
        another.add_place("other_system", "Other System", "system", "other_universe")
        another.add_place("other_planet", "Other Planet", "planet", "other_system")
        another.register_ship("voyager", "Other Voyager", "Somebody", "other_planet")
        with closing(sqlite3.connect(other)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "different world"):
                self.geo.arrive_in_transaction(
                    db, "voyager", now=int(ARRIVAL.timestamp()), action_id="wrong_world")
            db.rollback()
        self.assertEqual((0, 0), self._counts())

    def test_shared_arrival_requires_existing_transaction_and_valid_utc_epoch(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with self.assertRaisesRegex(RuntimeError, "transaction"):
                self.geo.arrive_in_transaction(
                    db, "voyager", now=int(ARRIVAL.timestamp()), action_id="outside")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "nonnegative integer"):
                self.geo.arrive_in_transaction(db, "voyager", now=True, action_id="bad_time")
            db.rollback()
        self.assertEqual((0, 0), self._counts())

    def test_shared_arrival_rejects_missing_receipt_key_and_fk_disabled(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "foreign keys"):
                self.geo.arrive_in_transaction(
                    db, "voyager", now=int(ARRIVAL.timestamp()), action_id="disabled_fk")
            db.rollback()
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "stable action_id"):
                self.geo.arrive_in_transaction(
                    db, "voyager", now=int(ARRIVAL.timestamp()), action_id=None)
            db.rollback()
        self.assertEqual((0, 0), self._counts())

    def test_shared_arrival_rejects_tuple_rows_before_mutation(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "row_factory"):
                self.geo.arrive_in_transaction(
                    db, "voyager", now=int(ARRIVAL.timestamp()), action_id="tuple_rows")
            db.rollback()
        self.assertEqual((0, 0), self._counts())

    def test_oversized_epoch_has_no_partial_effect_even_if_caller_commits(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises((ValueError, OverflowError)):
                self.geo.arrive_in_transaction(
                    db, "voyager", now=10**80, action_id="oversized_epoch")
            # A caller catching an error is allowed to commit its transaction:
            # the adapter must not have touched the ship or event ledger.
            self.assertEqual("moon", db.execute(
                "SELECT destination FROM geo_ships WHERE id='voyager'"
            ).fetchone()["destination"])
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM geo_ship_events WHERE kind='arrive'"
            ).fetchone()[0])
            db.commit()
        self.assertEqual("in_transit", self.geo.ship_status("voyager")["state"])
        self.assertEqual((0, 0), self._counts())

    def test_caught_sql_trigger_error_keeps_outer_transaction_usable(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(
                "CREATE TEMP TRIGGER reject_arrival BEFORE INSERT ON main.geo_ship_events "
                "WHEN NEW.kind='arrive' BEGIN SELECT RAISE(FAIL, 'forced arrival event failure'); END"
            )
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced arrival"):
                self.geo.arrive_in_transaction(
                    db, "voyager", now=int(ARRIVAL.timestamp()), action_id="trigger_failure")
            self.assertTrue(db.in_transaction)
            self.assertEqual("moon", db.execute(
                "SELECT destination FROM geo_ships WHERE id='voyager'"
            ).fetchone()["destination"])
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM geo_ship_events WHERE kind='arrive'"
            ).fetchone()[0])
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such savepoint"):
                db.execute("RELEASE SAVEPOINT realm_ship_arrival")
            db.execute(
                "INSERT INTO geo_ship_events(occurred_at,ship_id,actor,kind,payload_json) "
                "VALUES(?,?,?,?,?)", (1, "voyager", "system", "host_marker", "{}")
            )
            db.commit()
        self.assertEqual("in_transit", self.geo.ship_status("voyager")["state"])
        self.assertEqual((0, 0), self._counts())

    def test_successful_shared_arrival_is_rolled_back_with_outer_transaction(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            result = self.geo.arrive_in_transaction(
                db, "voyager", now=int(ARRIVAL.timestamp()), action_id="outer_rollback")
            self.assertEqual("docked", result["state"])
            self.assertTrue(db.in_transaction)
            db.rollback()
        self.assertEqual("in_transit", self.geo.ship_status("voyager")["state"])
        self.assertEqual((0, 0), self._counts())


if __name__ == "__main__":
    unittest.main()
