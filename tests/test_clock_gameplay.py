"""Clock-driven real Engine and Crafting actions share one SQLite transaction."""
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
from realm.engine import Engine
from realm.crafting import Crafting
from realm.clock_dispatch import schedule_in_transaction
from realm.world_clock import WorldClock

BASE = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)


class ClockGameplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "first.sqlite3"
        Gateway(self.path, world_id="world_first")
        self.engine = Engine(self.path)
        self.engine.join("BotA")
        self.crafting = Crafting(self.path)
        self.crafting.place_node("atrium", "iron", stock=6, batch_yield=3)
        self.crafting.register_recipe("blade", kind="craft", ingredients={"iron": 3})
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def _connection(self, path=None):
        db = sqlite3.connect(str(path or self.path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _apply(self, db, actor, command, action_id):
        try:
            from realm.clock_gameplay import perform_in_transaction
        except ModuleNotFoundError:
            self.fail("Clock gameplay lacks the shared-transaction action seam")
        return perform_in_transaction(
            db, engine=self.engine, crafting=self.crafting,
            actor=actor, command=command, action_id=action_id,
        )

    def test_gather_and_craft_from_real_clock_tick_with_atomic_followup(self):
        self.clock.schedule("gather_1", due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "gather iron"})
        responses = []

        def handler(db, event):
            responses.append(self._apply(db, event.payload["actor"],
                                         event.payload["command"], event.event_id))
            if event.event_id == "gather_1":
                schedule_in_transaction(self.clock, db, "craft_1",
                                        due_at=BASE + timedelta(seconds=1),
                                        kind="host_action",
                                        payload={"actor": "BotA", "command": "craft blade"})

        self.assertEqual(["gather_1"],
                         [x.event_id for x in self.clock.tick(BASE, handler)])
        self.assertEqual("You gather iron x3.", responses[0])
        self.assertEqual(["craft_1"], [x.event_id for x in
                         self.clock.tick(BASE + timedelta(seconds=1), handler)])
        self.assertEqual("You craft blade x1.", responses[1])
        self.assertEqual([], self.clock.tick(BASE + timedelta(seconds=2), handler))
        with closing(self._connection()) as db:
            remaining = db.execute(
                "SELECT remaining FROM gathering_nodes WHERE room_id='atrium' AND material='iron'"
            ).fetchone()[0]
            pouch = db.execute(
                "SELECT quantity FROM material_pouches WHERE actor='BotA' AND material='iron'"
            ).fetchone()[0]
            good = db.execute(
                "SELECT quantity FROM crafted_goods WHERE actor='BotA' AND good='blade'"
            ).fetchone()[0]
            receipts = db.execute(
                "SELECT action_id FROM action_receipts WHERE actor='BotA' ORDER BY action_id"
            ).fetchall()
        self.assertEqual((3, 0, 1), (remaining, pouch, good))
        self.assertEqual(["craft_1", "gather_1"], [x[0] for x in receipts])

    def test_engine_movement_and_forced_failure_roll_back_both_ledgers(self):
        self.clock.schedule("move_1", due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "north"})

        def fails(db, event):
            self.assertIn("The Corridor", self._apply(
                db, "BotA", "north", event.event_id))
            self.assertEqual("corridor", db.execute(
                "SELECT room_id FROM players WHERE id='BotA'").fetchone()[0])
            raise RuntimeError("synthetic abort")

        with self.assertRaisesRegex(RuntimeError, "synthetic abort"):
            self.clock.tick(BASE, fails)
        with closing(self._connection()) as db:
            self.assertEqual("atrium", db.execute(
                "SELECT room_id FROM players WHERE id='BotA'").fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM action_receipts WHERE actor='BotA'").fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM events WHERE kind='move'").fetchone()[0])
        self.assertIsNone(self.clock.lookup("move_1").processed_at)
        self.assertEqual(["move_1"], [x.event_id for x in self.clock.tick(
            BASE, lambda db, event: self._apply(
                db, "BotA", "north", event.event_id))])
        with closing(self._connection()) as db:
            self.assertEqual("corridor", db.execute(
                "SELECT room_id FROM players WHERE id='BotA'").fetchone()[0])

    def test_existing_action_receipt_replay_and_conflicting_text(self):
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            a = self._apply(db, "BotA", "gather iron", "same")
            self.assertEqual(a, self._apply(db, "BotA", "gather iron", "same"))
            with self.assertRaisesRegex(ValueError, "different command"):
                self._apply(db, "BotA", "gather iron 2", "same")
            db.commit()
        with closing(self._connection()) as db:
            self.assertEqual(3, db.execute(
                "SELECT remaining FROM gathering_nodes WHERE material='iron'"
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT count(*) FROM events WHERE kind='gather'"
            ).fetchone()[0])

    def test_fail_closed_for_foreign_world_or_missing_transaction(self):
        other = Path(self.path.parent) / "other.sqlite3"
        Gateway(other, world_id="world_second")
        with closing(self._connection()) as db:
            with self.assertRaisesRegex(RuntimeError, "transaction"):
                self._apply(db, "BotA", "north", "without_tx")
        with closing(self._connection(other)) as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "different world"):
                self._apply(db, "BotA", "north", "wrong_world")
            db.rollback()

    def test_fail_closed_for_unsupported_actions_and_connection_configuration(self):
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            for command in ("quit", "resources", "help", "erase world", "gather"):
                if command == "gather":
                    continue  # gather without argument is a valid read-only query.
                with self.subTest(command=command):
                    with self.assertRaisesRegex(ValueError, "Unsupported"):
                        self._apply(db, "BotA", command, command.replace(" ", "_"))
            with self.assertRaisesRegex(ValueError, "action_id"):
                self._apply(db, "BotA", "north", "")
            db.rollback()
        with closing(self._connection()) as db:
            db.row_factory = None
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "row_factory"):
                self._apply(db, "BotA", "north", "bad_row")
            db.rollback()
        with closing(sqlite3.connect(str(self.path))) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "foreign keys"):
                self._apply(db, "BotA", "north", "bad_fk")
            db.rollback()

    def test_failed_receipt_trigger_is_rolled_back_even_if_caught(self):
        self.clock.schedule("trigger", due_at=BASE, kind="host_action", payload={})
        with closing(self._connection()) as db, db:
            db.execute("""CREATE TRIGGER fail_action_receipt BEFORE INSERT ON action_receipts
                BEGIN
                    UPDATE gathering_nodes SET remaining=0 WHERE material='iron';
                    SELECT RAISE(FAIL, 'blocked receipt');
                END""")

        def handler(db, event):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "blocked receipt"):
                self._apply(db, "BotA", "gather iron", event.event_id)

        self.assertEqual(["trigger"], [x.event_id for x in
                         self.clock.tick(BASE, handler)])
        with closing(self._connection()) as db:
            self.assertEqual(6, db.execute(
                "SELECT remaining FROM gathering_nodes WHERE material='iron'"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM action_receipts WHERE actor='BotA'"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM events WHERE kind='gather'"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
