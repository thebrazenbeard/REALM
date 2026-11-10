"""Typed UTC host actions must preserve the timestamp issued by their emitter.

Only a preinstalled privileged SQLite trigger can mount these synthetic edits;
the tests do not claim an unprivileged remote player may install triggers.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.clock_gameplay import perform_in_transaction
from realm.crafting import Crafting
from realm.engine import Engine
from realm.gateway import Gateway
from realm.world_clock import WorldClock


NOW = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)
FORGED = "1900-01-01T00:00:00+00:00"


class HostEventTimestampCustodyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "world.sqlite3"
        Gateway(self.path, world_id="timestamp_synthetic")
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.crafting = Crafting(self.path)
        self.crafting.place_node("atrium", "silver", stock=4, batch_yield=1)
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def sql(self, script):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(script)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return tuple(tuple(db.execute("SELECT * FROM main." + table))
                         for table in ("players", "items", "events", "action_receipts",
                                       "gathering_nodes", "material_pouches", "sqlite_sequence"))

    def selected(self, action_id, command, *, kind="host_action"):
        self.clock.schedule(action_id, due_at=NOW, kind=kind,
                            payload={"actor": "Ada", "command": command})

    def tick(self):
        def callback(db, event):
            if event.kind == "audit":
                db.execute("INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                           "VALUES(?,?,?,?,?)", (NOW.isoformat(), "Ben", "atrium",
                                                "say", '{"text":"trusted audit"}'))
                return None
            return perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)
        return tuple(event.event_id for event in self.clock.tick(NOW, callback))

    def attack_and_retry(self, command, kind, *, setup=None):
        if setup:
            setup()
        self.selected("signed", command)
        self.sql("CREATE TRIGGER forge AFTER INSERT ON events "
                 f"WHEN NEW.kind='{kind}' BEGIN "
                 f"UPDATE events SET occurred_at='{FORGED}' WHERE id=NEW.id; "
                 "END;")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "event|history|timestamp|effect|time"):
            self.tick()
        self.assertEqual(before, self.snapshot())
        self.assertIsNone(self.clock.lookup("signed").processed_at)
        self.sql("DROP TRIGGER main.forge;")
        self.assertEqual(("signed",), self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM events WHERE occurred_at=?", (FORGED,)).fetchone()[0])

    def test_leave_timestamp_forgery_is_rejected(self):
        self.attack_and_retry("go north", "leave")

    def test_move_timestamp_forgery_is_rejected(self):
        self.attack_and_retry("go north", "move")

    def test_take_timestamp_forgery_is_rejected(self):
        self.attack_and_retry("take token", "take")

    def test_drop_timestamp_forgery_is_rejected(self):
        self.attack_and_retry("drop token", "drop",
                              setup=lambda: self.engine.act("Ada", "take token"))

    def test_say_timestamp_forgery_is_rejected(self):
        self.attack_and_retry("say hello", "say")

    def test_gather_timestamp_forgery_is_rejected(self):
        self.attack_and_retry("gather silver", "gather")

    def test_valid_mixed_selected_handlers_retains_real_and_audit_time(self):
        self.selected("a_take", "take token")
        self.clock.schedule("b_audit", due_at=NOW, kind="audit", payload={})
        self.selected("c_drop", "drop token")
        self.selected("d_say", "say hello")
        self.selected("e_gather", "gather silver")
        self.selected("f_move", "go north")
        self.assertEqual(("a_take", "b_audit", "c_drop", "d_say", "e_gather", "f_move"),
                         self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT kind,occurred_at FROM events "
                              "WHERE kind NOT IN ('join') ORDER BY id").fetchall()
            self.assertEqual(["take", "say", "drop", "say", "gather", "leave", "move"],
                             [row[0] for row in rows])
            self.assertTrue(all(row[1] != FORGED for row in rows))

    def test_late_batch_timestamp_poison_rolls_back_prior_valid_actions(self):
        self.selected("a_take", "take token")
        self.clock.schedule("b_audit", due_at=NOW, kind="audit", payload={})
        self.selected("c_drop", "drop token")
        self.sql("CREATE TRIGGER forge AFTER INSERT ON events "
                 "WHEN NEW.kind='drop' BEGIN "
                 f"UPDATE events SET occurred_at='{FORGED}' WHERE id=NEW.id; END;")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "event|history|timestamp|effect|time"):
            self.tick()
        self.assertEqual(before, self.snapshot())
        self.assertTrue(all(self.clock.lookup(event).processed_at is None
                            for event in ("a_take", "b_audit", "c_drop")))
        self.sql("DROP TRIGGER main.forge;")
        self.assertEqual(("a_take", "b_audit", "c_drop"), self.tick())
        self.assertEqual((), self.tick())


if __name__ == "__main__":
    unittest.main()
