"""Host-signed craft/build must not acknowledge ungranted economic effects.

Only a privileged installer of SQLite triggers can reproduce these local
postcondition attacks; this does not establish a remote-client exploit.
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


NOW = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)


class TypedCraftBuildCustodyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        Gateway(self.path, world_id="craft_synthetic")
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.crafting = Crafting(self.path)
        self.crafting.place_node("atrium", "silver", stock=30, batch_yield=6)
        self.crafting.register_recipe("silver pin", kind="craft",
                                      ingredients={"silver": 2},
                                      output="pin", output_count=3)
        self.crafting.register_recipe("silver arch", kind="build",
                                      ingredients={"silver": 4},
                                      output="arch")
        self.crafting.perform("Ada", "gather silver 2", action_id="seed")
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def sql(self, statement):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(statement)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            tables = ("players", "items", "events", "action_receipts",
                      "gathering_nodes", "material_pouches", "crafted_goods",
                      "built_structures", "craft_recipes", "sqlite_sequence")
            return tuple(tuple(db.execute("SELECT * FROM main." + table)) for table in tables)

    def run_signed(self, action_id, command):
        self.clock.schedule(action_id, due_at=NOW, kind="host_action",
                            payload={"actor": "Ada", "command": command})

        def callback(db, event):
            return perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)

        return tuple(item.event_id for item in self.clock.tick(NOW, callback))

    def assert_rejected_and_retry(self, action_id, command, trigger_sql):
        self.sql(trigger_sql)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "[Ee]ffect|[Ee]conomic|[Hh]istory|[Ee]vent|[Ss]tate|allocation"):
            self.run_signed(action_id, command)
        self.assertEqual(before, self.snapshot())
        self.assertIsNone(self.clock.lookup(action_id).processed_at)
        self.sql("DROP TRIGGER forge;")
        self.assertEqual((action_id,), self.run_signed(action_id, command))
        self.assertEqual((), self.run_signed(action_id, command))

    def test_craft_event_trigger_cannot_move_unrelated_player(self):
        self.assert_rejected_and_retry(
            "craft", "craft silver pin",
            "CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='craft' "
            "BEGIN UPDATE players SET room_id='corridor' WHERE id='Ben'; END;")

    def test_craft_event_timestamp_cannot_be_forged(self):
        self.assert_rejected_and_retry(
            "craft", "craft silver pin",
            "CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='craft' "
            "BEGIN UPDATE events SET occurred_at='1900-01-01T00:00:00+00:00' "
            "WHERE id=NEW.id; END;")

    def test_build_structure_timestamp_cannot_be_forged(self):
        self.assert_rejected_and_retry(
            "build", "build silver arch",
            "CREATE TRIGGER forge AFTER INSERT ON built_structures "
            "BEGIN UPDATE built_structures SET constructed_at='1900-01-01T00:00:00+00:00' "
            "WHERE id=NEW.id; END;")

    def test_build_receipt_trigger_cannot_grant_extra_goods(self):
        self.assert_rejected_and_retry(
            "build", "build silver arch",
            "CREATE TRIGGER forge AFTER INSERT ON action_receipts WHEN NEW.action_id='build' "
            "BEGIN INSERT INTO crafted_goods VALUES('Ben','counterfeit',77); END;")

    def test_craft_event_cannot_mutate_other_players_pouch(self):
        self.assert_rejected_and_retry(
            "craft", "craft silver pin",
            "CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='craft' "
            "BEGIN INSERT INTO material_pouches VALUES('Ben','silver',99); END;")

    def test_craft_event_cannot_launder_forged_speech(self):
        self.assert_rejected_and_retry(
            "craft", "craft silver pin",
            "CREATE TRIGGER forge AFTER INSERT ON events WHEN NEW.kind='craft' "
            "BEGIN INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
            "VALUES('2026-11-09','Ben','atrium','say','{}'); END;")

    def test_craft_event_cannot_poison_allocator(self):
        self.assert_rejected_and_retry(
            "craft", "craft silver pin",
            "CREATE TRIGGER forge AFTER INSERT ON action_receipts WHEN NEW.action_id='craft' "
            "BEGIN UPDATE sqlite_sequence SET seq=9223372036854775807 WHERE name='events'; END;")

    def test_build_structure_cannot_grant_unrelated_structure(self):
        self.assert_rejected_and_retry(
            "build", "build silver arch",
            "CREATE TRIGGER forge AFTER INSERT ON built_structures "
            "BEGIN INSERT INTO built_structures(room_id,recipe,owner,constructed_at) "
            "VALUES('atrium','silver arch','Ben','2026-11-09'); END;")

    def test_invalid_or_duplicate_recipe_ack_without_economic_effect(self):
        self.assertEqual(("invalid",), self.run_signed("invalid", "craft unknown"))
        self.assertEqual(("build",), self.run_signed("build", "build silver arch"))
        prior = self.snapshot()
        self.assertEqual(("duplicate",), self.run_signed("duplicate", "build silver arch"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM built_structures WHERE owner='Ada'"
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='build'"
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM action_receipts WHERE action_id='duplicate'"
            ).fetchone()[0])
        self.assertNotEqual(prior, self.snapshot())  # new receipt, no new game effects

    def test_separately_selected_audit_speech_between_actions_allowed(self):
        self.clock.schedule("a_craft", due_at=NOW, kind="host_action",
                            payload={"actor": "Ada", "command": "craft silver pin"})
        self.clock.schedule("b_audit", due_at=NOW, kind="audit", payload={})
        self.clock.schedule("c_build", due_at=NOW, kind="host_action",
                            payload={"actor": "Ada", "command": "build silver arch"})

        def callback(db, event):
            if event.kind == "audit":
                db.execute("INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                           "VALUES(?,?,?,?,?)", (NOW.isoformat(), "Ben", "atrium",
                                                "say", '{"text":"explicit audit"}'))
                return
            return perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)

        self.assertEqual(("a_craft", "b_audit", "c_build"),
                         tuple(row.event_id for row in self.clock.tick(NOW, callback)))
        self.assertEqual((), tuple(row.event_id for row in self.clock.tick(NOW, callback)))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(["craft", "say", "build"], [r[0] for r in db.execute(
                "SELECT kind FROM events WHERE kind IN ('craft','say','build') ORDER BY id")])

    def test_valid_craft_then_build_uses_stock_and_receipts_once(self):
        self.assertEqual(("craft",), self.run_signed("craft", "craft silver pin"))
        self.assertEqual(("build",), self.run_signed("build", "build silver arch"))
        self.assertEqual((), self.run_signed("craft", "craft silver pin"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(3, db.execute(
                "SELECT quantity FROM crafted_goods WHERE actor='Ada' AND good='pin'"
            ).fetchone()[0])
            self.assertEqual(6, db.execute(
                "SELECT quantity FROM material_pouches WHERE actor='Ada' AND material='silver'"
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM built_structures WHERE owner='Ada'"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
