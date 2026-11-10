"""Host-action speech must not launder extra SQLite-trigger events."""
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


NOW = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)


class HostActionSayCustodyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "world.sqlite3"
        Gateway(self.path, world_id="synthetic_speech")
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.clock.schedule("signed_say", due_at=NOW, kind="host_action",
                            payload={"actor": "Ada", "command": "say hello"})

    def install(self, statement):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(statement)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return tuple(tuple(db.execute("SELECT * FROM main." + table))
                         for table in ("events", "action_receipts", "sqlite_sequence"))

    def tick(self):
        def callback(db, event):
            if event.kind == "audit":
                db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                           "VALUES(?,?,?,?,?)", (NOW.isoformat(), "Ben", "atrium",
                                                "say", '{"text":"audit message"}'))
                return None
            return perform_in_transaction(
                db, engine=self.engine, crafting=None,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)
        return tuple(e.event_id for e in self.clock.tick(NOW, callback))

    def assert_rejected_then_retry_once(self, script):
        self.install(script)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "[Ss]ay|speech|event|effect|history"):
            self.tick()
        self.assertEqual(before, self.snapshot())
        self.assertIsNone(self.clock.lookup("signed_say").processed_at)
        self.install("DROP TRIGGER main.forge_speech;")
        self.assertEqual(("signed_say",), self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT actor,room_id,payload_json FROM main.events "
                              "WHERE kind='say'").fetchall()
            self.assertEqual([("Ada", "atrium", '{"text": "hello"}')], rows)
        self.assertEqual("You say: after retry", self.engine.act("Ada", "say after retry"))

    def test_ghost_speech_from_event_trigger_is_not_signed(self):
        self.assert_rejected_then_retry_once("""
            CREATE TRIGGER forge_speech AFTER INSERT ON events
            WHEN NEW.kind='say' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T16:00:00+00:00','GhostAccount','invalid_room',
                     'say','{"text":"I was never here"}');
            END;
        """)

    def test_maximum_id_trigger_cannot_poison_next_legitimate_speech(self):
        self.assert_rejected_then_retry_once("""
            CREATE TRIGGER forge_speech AFTER INSERT ON events
            WHEN NEW.kind='say' BEGIN
              INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json)
              VALUES(9223372036854775807,'2026-11-09T16:00:00+00:00',
                     'Ada','atrium','say','{"text":"poison"}');
            END;
        """)

    def test_same_speech_replacement_at_maximum_id_is_not_emission_proof(self):
        # A forged row can have exactly the expected actor/room/payload while
        # replacing the original event at an attacker-chosen allocator ID.
        self.assert_rejected_then_retry_once("""
            CREATE TRIGGER forge_speech AFTER INSERT ON events
            WHEN NEW.kind='say' BEGIN
              DELETE FROM events WHERE id=NEW.id;
              INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json)
              VALUES(9223372036854775807,NEW.occurred_at,NEW.actor,
                     NEW.room_id,NEW.kind,NEW.payload_json);
            END;
        """)

    def test_receipt_trigger_cannot_impersonate_unrelated_speaker(self):
        self.assert_rejected_then_retry_once("""
            CREATE TRIGGER forge_speech AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed_say' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T16:00:00+00:00','Ben','atrium',
                     'say','{"text":"ungranted"}');
            END;
        """)

    def test_separate_selected_audit_handler_may_emit_its_own_speech(self):
        self.clock.schedule("z_audit", due_at=NOW, kind="audit", payload={})
        self.assertEqual(("signed_say", "z_audit"), self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            actual = db.execute("SELECT actor,payload_json FROM main.events "
                                "WHERE kind='say' ORDER BY id").fetchall()
        self.assertEqual([("Ada", '{"text": "hello"}'),
                          ("Ben", '{"text":"audit message"}')], actual)

    def test_earlier_selected_audit_speech_is_not_rejected_by_later_typed_say(self):
        self.clock.schedule("a_audit", due_at=NOW, kind="audit", payload={})
        self.assertEqual(("a_audit", "signed_say"), self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT actor,payload_json FROM main.events "
                              "WHERE kind='say' ORDER BY id").fetchall()
        self.assertEqual([("Ben", '{"text":"audit message"}'),
                          ("Ada", '{"text": "hello"}')], rows)

    def test_empty_and_oversized_say_emit_no_events_but_receipts(self):
        self.clock.schedule("z_empty", due_at=NOW, kind="host_action",
                            payload={"actor": "Ada", "command": "say "})
        self.clock.schedule("zz_oversize", due_at=NOW, kind="host_action",
                            payload={"actor": "Ada", "command": "say " + "x" * 281})
        self.assertEqual(("signed_say", "z_empty", "zz_oversize"), self.tick())
        self.assertEqual((), self.tick())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM main.events WHERE kind='say'").fetchone()[0])
            rows = db.execute(
                "SELECT action_id,response FROM main.action_receipts ORDER BY action_id"
            ).fetchall()
        self.assertEqual([("signed_say", "You say: hello"),
                          ("z_empty", "Say what?"),
                          ("zz_oversize", "Messages must be 280 characters or fewer.")], rows)

    def test_core_only_world_speech_does_not_require_optional_crafting_schema(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "core-only.sqlite3"
        engine = Engine(path)
        engine.join("Ada")
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO main.world_meta(key,value) VALUES('world_id','core_only')")
            db.commit()
            self.assertIsNone(db.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE name='gathering_nodes'").fetchone())
        clock = WorldClock(path)
        clock.enable()
        clock.schedule("plain_say", due_at=NOW, kind="host_action",
                       payload={"actor": "Ada", "command": "say from core"})

        def callback(db, event):
            return perform_in_transaction(
                db, engine=engine, crafting=None, actor="Ada",
                command="say from core", action_id=event.event_id)

        self.assertEqual(["plain_say"], [e.event_id for e in clock.tick(NOW, callback)])
        self.assertEqual([], clock.tick(NOW, callback))
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
