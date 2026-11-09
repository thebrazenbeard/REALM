"""Host-selected movement/items must attest their entire in-world effects.

Adversarial cases require *installed privileged SQLite triggers*. They do not
demonstrate that an unprivileged remote player can install such triggers.
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
from realm.engine import Engine
from realm.gateway import Gateway
from realm.world_clock import WorldClock


UTC = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


class TypedHostActionCustodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        Gateway(self.path, world_id="typed_effects_synthetic")
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def sql(self, script):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(script)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return tuple(tuple(db.execute("SELECT * FROM main." + name))
                         for name in ("players", "items", "events",
                                      "action_receipts", "sqlite_sequence"))

    def tick(self):
        def callback(db, event):
            if event.kind == "audit":
                db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                           "VALUES(?,?,?,?,?)",
                           (UTC.isoformat(), "Ben", "atrium", "say", '{"text":"audit"}'))
                return None
            return perform_in_transaction(
                db, engine=self.engine, crafting=None,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)
        return tuple(e.event_id for e in self.clock.tick(UTC, callback))

    def schedule(self, action_id, command, actor="Ada"):
        self.clock.schedule(action_id, due_at=UTC, kind="host_action",
                            payload={"actor": actor, "command": command})

    def reject_rollback_retry(self, command, script):
        self.schedule("signed", command)
        self.sql(script)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "effect|state|history|event|receipt|allocation"):
            self.tick()
        self.assertEqual(before, self.snapshot())
        self.assertIsNone(self.clock.lookup("signed").processed_at)
        self.sql("DROP TRIGGER main.forge; ")
        self.assertEqual(("signed",), self.tick())
        self.assertEqual((), self.tick())
        self.assertIsNotNone(self.clock.lookup("signed").processed_at)

    def test_go_event_trigger_cannot_move_unrelated_player(self):
        self.reject_rollback_retry("go north", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='move'
            BEGIN UPDATE players SET room_id='workshop' WHERE id='Ben'; END;
        """)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([("Ada", "corridor"), ("Ben", "atrium")],
                             db.execute("SELECT id,room_id FROM players ORDER BY id").fetchall())
            self.assertEqual([("leave",), ("move",)],
                             db.execute("SELECT kind FROM events WHERE kind IN ('leave','move') "
                                        "ORDER BY id").fetchall())

    def test_take_event_trigger_cannot_move_unrelated_player(self):
        self.reject_rollback_retry("take token", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='take'
            BEGIN UPDATE players SET room_id='workshop' WHERE id='Ben'; END;
        """)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual((None, "Ada"), db.execute(
                "SELECT room_id,held_by FROM items WHERE id='token'").fetchone())

    def test_drop_event_trigger_cannot_steal_unrelated_item(self):
        self.engine.act("Ada", "take token")
        self.reject_rollback_retry("drop token", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='drop'
            BEGIN UPDATE items SET room_id=NULL,held_by='Ben' WHERE id='wrench'; END;
        """)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(("workshop", None), db.execute(
                "SELECT room_id,held_by FROM items WHERE id='wrench'").fetchone())

    def test_receipt_trigger_cannot_insert_extra_event(self):
        self.reject_rollback_retry("take token", """
            CREATE TRIGGER forge AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09','Ghost','nonexistent','take','{"item":"wrench"}');
            END;
        """)

    def test_event_trigger_cannot_replace_movement_history(self):
        self.reject_rollback_retry("north", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='move'
            BEGIN DELETE FROM events WHERE id=NEW.id;
              INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json)
              VALUES(9223372036854775807,NEW.occurred_at,NEW.actor,
                     NEW.room_id,NEW.kind,NEW.payload_json);
            END;
        """)

    def test_take_event_trigger_cannot_replace_genuine_event_at_maxint(self):
        self.reject_rollback_retry("take token", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='take'
            BEGIN DELETE FROM events WHERE id=NEW.id;
              INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json)
              VALUES(9223372036854775807,NEW.occurred_at,NEW.actor,
                     NEW.room_id,NEW.kind,NEW.payload_json);
            END;
        """)

    def test_drop_event_trigger_cannot_replace_genuine_event_at_maxint(self):
        self.engine.act("Ada", "take token")
        self.reject_rollback_retry("drop token", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='drop'
            BEGIN DELETE FROM events WHERE id=NEW.id;
              INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json)
              VALUES(9223372036854775807,NEW.occurred_at,NEW.actor,
                     NEW.room_id,NEW.kind,NEW.payload_json);
            END;
        """)

    def test_move_leave_event_trigger_cannot_insert_unrelated_speech(self):
        self.reject_rollback_retry("go north", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='leave'
            BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09','Ben','atrium','say','{"text":"forged"}');
            END;
        """)

    def test_receipt_trigger_cannot_add_event_for_invalid_command(self):
        self.reject_rollback_retry("go nowhere", """
            CREATE TRIGGER forge AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09','Ada','atrium','move','{"direction":"north"}');
            END;
        """)

    def test_event_trigger_cannot_steal_another_actors_inventory(self):
        self.reject_rollback_retry("take token", """
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='take'
            BEGIN
              UPDATE items SET held_by='Ben',room_id=NULL WHERE id='wrench';
            END;
        """)

    def test_receipt_trigger_cannot_mutate_unrelated_world_table(self):
        self.sql("CREATE TABLE host_flags (k TEXT PRIMARY KEY,v INT); "
                 "INSERT INTO host_flags VALUES('a',0);")
        self.reject_rollback_retry("go north", """
            CREATE TRIGGER forge AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed' BEGIN
              UPDATE host_flags SET v=1 WHERE k='a';
            END;
        """)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT v FROM host_flags").fetchone()[0])

    def test_valid_move_take_drop_and_invalid_actions_are_once_only(self):
        for action_id, command in (("a_take", "take token"),
                                   ("b_drop", "drop token"),
                                   ("c_move", "n"),
                                   ("d_invalid", "go nowhere"),
                                   ("e_missing", "take missing")):
            self.schedule(action_id, command)
        self.assertEqual(("a_take", "b_drop", "c_move", "d_invalid", "e_missing"), self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual("corridor", db.execute("SELECT room_id FROM players "
                                                     "WHERE id='Ada'").fetchone()[0])
            self.assertEqual(("atrium", None), db.execute(
                "SELECT room_id,held_by FROM items WHERE id='token'").fetchone())
            self.assertEqual(5, db.execute("SELECT count(*) FROM action_receipts").fetchone()[0])
            self.assertEqual([("take",), ("drop",), ("leave",), ("move",)],
                             db.execute("SELECT kind FROM events WHERE "
                                        "kind NOT IN ('join') ORDER BY id").fetchall())

    def test_separate_selected_audit_say_is_allowed_between_actions(self):
        self.schedule("a_take", "take token")
        self.clock.schedule("b_audit", due_at=UTC, kind="audit", payload={})
        self.schedule("c_drop", "drop token")
        self.assertEqual(("a_take", "b_audit", "c_drop"), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([("Ada", "take"), ("Ben", "say"), ("Ada", "drop")],
                             db.execute("SELECT actor,kind FROM events WHERE "
                                        "kind NOT IN ('join') ORDER BY id").fetchall())

    def test_later_malicious_drop_rolls_back_entire_multi_event_tick(self):
        self.schedule("a_take", "take token")
        self.clock.schedule("b_audit", due_at=UTC, kind="audit", payload={})
        self.schedule("c_drop", "drop token")
        self.sql("""
            CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='drop'
            BEGIN UPDATE players SET room_id='workshop' WHERE id='Ben'; END;
        """)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(before, self.snapshot())
        for event_id in ("a_take", "b_audit", "c_drop"):
            self.assertIsNone(self.clock.lookup(event_id).processed_at)
        self.sql("DROP TRIGGER main.forge;")
        self.assertEqual(("a_take", "b_audit", "c_drop"), self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(("atrium", None), db.execute(
                "SELECT room_id,held_by FROM items WHERE id='token'").fetchone())
            self.assertEqual([("take",), ("say",), ("drop",)], db.execute(
                "SELECT kind FROM events WHERE kind NOT IN ('join') "
                "ORDER BY id").fetchall())


if __name__ == "__main__":
    unittest.main()
