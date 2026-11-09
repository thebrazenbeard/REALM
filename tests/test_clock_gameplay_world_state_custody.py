"""A signed gather must not launder unrelated world-state writes via receipt hooks.

These synthetic attacks require pre-installed privileged SQLite triggers;
they are not evidence that a remote player can install such triggers.
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


UTC = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)


class GatherWorldStateCustodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        Gateway(self.path, world_id="synthetic")
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.crafting = Crafting(self.path)
        self.crafting.place_node("atrium", "silver", stock=3, batch_yield=1)
        self.crafting.register_recipe("silver_bar", kind="craft",
                                      ingredients={"silver": 1})
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def _sql(self, script):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(script)

    def _snapshot(self):
        tables = ("rooms", "exits", "players", "items", "world_meta",
                  "player_credentials", "geo_places", "geo_links",
                  "gathering_nodes", "material_pouches", "crafted_goods",
                  "built_structures", "craft_recipes", "events",
                  "action_receipts", "sqlite_sequence", "host_quest_flags")
        with closing(sqlite3.connect(self.path)) as db:
            return {name: sorted(db.execute("SELECT * FROM main." + name).fetchall(),
                                 key=repr)
                    for name in tables if name != "host_quest_flags" or
                    db.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                               (name,)).fetchone() is not None}

    def _tick(self):
        self.clock.schedule("signed_gather", due_at=UTC, kind="host_action",
                            payload={"actor": "Ada", "command": "gather silver"})

        def callback(db, event):
            return perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)

        return self.clock.tick(UTC, callback)

    def _reject_and_retry(self, trigger_script):
        self._sql(trigger_script)
        before = self._snapshot()
        with self.assertRaises(ValueError):
            self._tick()
        self.assertEqual(before, self._snapshot())
        self.assertIsNone(self.clock.lookup("signed_gather").processed_at)
        self._sql("DROP TRIGGER main.forge_receipt;")
        self.assertEqual(["signed_gather"],
                         [e.event_id for e in self._tick()])
        self.assertEqual([], self._tick())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT quantity FROM material_pouches WHERE actor='Ada' "
                "AND material='silver'").fetchone()[0])

    def test_receipt_trigger_cannot_move_other_player(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed_gather' BEGIN
              UPDATE players SET room_id='workshop' WHERE id='Ben';
            END;
        """)

    def test_receipt_trigger_cannot_steal_other_players_item(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed_gather' BEGIN
              UPDATE items SET room_id=NULL,held_by='Ben' WHERE id='token';
            END;
        """)

    def test_receipt_trigger_cannot_mutate_opt_in_world_table(self):
        self._sql("CREATE TABLE host_quest_flags (key TEXT PRIMARY KEY, value INT);"
                  "INSERT INTO host_quest_flags VALUES ('quest',0);")
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed_gather' BEGIN
              UPDATE host_quest_flags SET value=100 WHERE key='quest';
            END;
        """)

    def test_late_clock_receipt_cannot_move_player(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              UPDATE players SET room_id='workshop' WHERE id='Ben';
            END;
        """)

    def test_late_clock_receipt_cannot_forge_recipe_or_receipt(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              UPDATE craft_recipes SET output_count=99 WHERE id='silver_bar';
              UPDATE action_receipts SET response='forged'
                WHERE action_id='signed_gather';
            END;
        """)

    def test_late_clock_receipt_cannot_rewrite_world_metadata(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              UPDATE world_meta SET value='wrong_world' WHERE key='world_id';
              UPDATE exits SET destination='workshop'
                WHERE room_id='atrium' AND direction='north';
            END;
        """)

    def test_late_clock_receipt_cannot_rewrite_auth_or_route(self):
        self._sql("""
            INSERT INTO player_credentials(player_name,token_digest,created_at)
              VALUES('Ben','synthetic_digest_before','2026-11-09T16:00:00Z');
            INSERT INTO geo_places(id,name,kind,parent_id,x,y)
              VALUES('testa','A','room',NULL,NULL,NULL),
                    ('testb','B','room',NULL,NULL,NULL);
            INSERT INTO geo_links(origin,destination,mode,seconds)
              VALUES('testa','testb','walk',10);
        """)
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              UPDATE player_credentials SET token_digest='synthetic_digest_forged'
                WHERE player_name='Ben';
              UPDATE geo_links SET seconds=99
                WHERE origin='testa' AND destination='testb';
            END;
        """)

    def test_late_receipt_cannot_create_unauthorized_say_event(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T16:00:00+00:00','GhostAccount','invalid_room',
                     'say','{"bad":true}');
            END;
        """)

    def test_late_receipt_cannot_impersonate_valid_speaker(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T16:00:00+00:00','Ben','atrium',
                     'say','{"message":"plausible but ungranted"}');
            END;
        """)

    def test_late_receipt_cannot_enqueue_unauthorized_future_action(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              INSERT INTO clock_events(event_id,due_utc,kind,payload_json,processed_utc)
              VALUES('forged_future','2026-11-09T16:01:00.000000+00:00',
                     'host_action','{}',NULL);
            END;
        """)
        self.assertIsNone(self.clock.lookup("forged_future"))

    def test_late_receipt_cannot_poison_event_id_allocator(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='signed_gather' BEGIN
              UPDATE sqlite_sequence SET seq=9223372036854775807
                WHERE name='events';
            END;
        """)

    def test_in_handler_receipt_cannot_poison_event_id_allocator(self):
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed_gather' BEGIN
              UPDATE sqlite_sequence SET seq=9223372036854775807
                WHERE name='events';
            END;
        """)

    def test_final_utc_watermark_cannot_insert_unauthorized_say(self):
        # clock_state starts empty; first host tick inserts its UTC watermark.
        self._reject_and_retry("""
            CREATE TRIGGER forge_receipt AFTER INSERT ON clock_state BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T16:00:00+00:00','GhostAccount','invalid_room',
                     'say','{"bad":true}');
            END;
        """)

    def test_three_event_batch_cannot_absorb_earlier_receipt_forgery(self):
        self.clock.schedule("a_host", due_at=UTC, kind="host_action",
                            payload={"actor": "Ada", "command": "gather silver"})
        self.clock.schedule("b_audit", due_at=UTC, kind="audit", payload={})
        self.clock.schedule("z_host", due_at=UTC, kind="host_action",
                            payload={"actor": "Ben", "command": "gather silver"})
        self._sql("""CREATE TRIGGER forge_receipt AFTER UPDATE OF processed_utc
            ON clock_events WHEN NEW.event_id='a_host' BEGIN
              UPDATE craft_recipes SET output_count=99 WHERE id='silver_bar';
            END;""")

        def callback(db, event):
            if event.kind == "audit":
                db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                           "VALUES(?,?,?,?,?)", (UTC.isoformat(), "Ada", "atrium",
                                                "say", '{"message":"legitimate"}'))
                return None
            return perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)

        before = self._snapshot()
        with self.assertRaises(ValueError):
            self.clock.tick(UTC, callback)
        self.assertEqual(before, self._snapshot())
        self.assertTrue(all(self.clock.lookup(event_id).processed_at is None
                            for event_id in ("a_host", "b_audit", "z_host")))
        self._sql("DROP TRIGGER main.forge_receipt;")
        self.assertEqual(["a_host", "b_audit", "z_host"],
                         [e.event_id for e in self.clock.tick(UTC, callback)])
        self.assertEqual([], self.clock.tick(UTC, callback))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(1, db.execute("SELECT output_count FROM craft_recipes "
                                            "WHERE id='silver_bar'").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT count(*) FROM events "
                                            "WHERE kind='gather'").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT count(*) FROM events "
                                            "WHERE kind='say'").fetchone()[0])

    def test_distinct_valid_typed_movement_and_say_still_commit(self):
        self.clock.schedule("z_move", due_at=UTC, kind="host_action",
                            payload={"actor": "Ben", "command": "north"})
        self._sql("""CREATE TRIGGER harmless_say AFTER INSERT ON action_receipts
            WHEN NEW.action_id='signed_gather' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T16:00:00+00:00','Ada','atrium','say',
                     '{"message":"allowed"}');
            END;""")
        self.assertEqual(["signed_gather", "z_move"],
                         [e.event_id for e in self._tick()])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual("corridor", db.execute("SELECT room_id FROM players "
                                                     "WHERE id='Ben'").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT count(*) FROM events "
                                            "WHERE kind='say'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
