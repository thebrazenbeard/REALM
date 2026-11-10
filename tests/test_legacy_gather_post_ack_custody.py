"""Disallow ungranted gather rows after a trusted legacy handler returns.

Disposable SQLite MAIN triggers here represent privileged preinstalled host
hooks, not remote player powers or cryptographic attestation.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import test_npc_historical_cross_batch_custody as historical_fixture
from realm.clock_gameplay import perform_in_transaction


class LegacyGatherPostAckTests(unittest.TestCase):
    def setUp(self):
        fx = historical_fixture.HistoricalNpcCustodyTests(
            "test_legitimate_audit_only_appends_say_and_preserves_old_gathers"
        )
        fx.setUp()
        self.addCleanup(fx.fx.doCleanups)
        self.fx = fx
        self.world = fx.fx
        self.now = fx.later
        self.assertTrue(self.world.crafting.place_node(
            "atrium", "silver", stock=6, batch_yield=3
        ))
        self.world.clock.schedule("a_host", due_at=self.now,
                                  kind="host_action",
                                  payload={"actor": "Ada", "command": "gather silver"})

    def _sql(self, statements):
        with closing(sqlite3.connect(self.world.dbpath)) as db:
            db.executescript(statements)

    def _state(self):
        with closing(sqlite3.connect(self.world.dbpath)) as db:
            return (
                db.execute("SELECT * FROM events ORDER BY id").fetchall(),
                db.execute("SELECT * FROM material_pouches ORDER BY actor,material").fetchall(),
                db.execute("SELECT * FROM gathering_nodes ORDER BY room_id,material").fetchall(),
                db.execute("SELECT * FROM action_receipts ORDER BY actor,action_id").fetchall(),
                db.execute("SELECT * FROM clock_events ORDER BY event_id").fetchall(),
                db.execute("SELECT * FROM clock_state").fetchall(),
            )

    def _dispatch(self, db, event):
        if event.kind == "host_action":
            return perform_in_transaction(
                db, engine=self.world.engine, crafting=self.world.crafting,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id,
            )
        if event.kind == "audit":
            return db.execute(
                "INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                "VALUES(?,?,?,?,?)", (event.due_at.isoformat(), "Ada", "atrium",
                                     "say", '{"text":"allowed"}'),
            )
        raise ValueError("Unknown synthetic event")

    def _deny_then_retry(self, trigger_name, *, expected_events=("a_host",)):
        before = self._state()
        with self.assertRaisesRegex(ValueError, "Roster"):
            self.world.clock.tick(self.now, self._dispatch)
        self.assertEqual(before, self._state())
        self.assertIsNone(self.world.clock.lookup("a_host").processed_at)
        self._sql(f"DROP TRIGGER main.{trigger_name};")
        self.assertEqual(list(expected_events),
                         [e.event_id for e in self.world.clock.tick(self.now, self._dispatch)])
        self.assertEqual([], self.world.clock.tick(self.now, self._dispatch))
        self.assertEqual(3, self.world.counts()[0])

    def test_last_watermark_cannot_add_forged_gather(self):
        self._sql("""CREATE TRIGGER main.forge_after_watermark
            AFTER UPDATE OF last_utc ON clock_state BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T00:00:00+00:00','Forgery','atrium','gather',
                     '{"material":"silver","amount":999}');
            END;""")
        self._deny_then_retry("forge_after_watermark")

    def test_last_receipt_cannot_insert_negative_id_gather(self):
        self._sql("""CREATE TRIGGER main.forge_low_id_after_receipt
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='a_host' BEGIN
              INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json)
              VALUES(-11,'2026-11-09T00:00:00+00:00','Forgery','atrium','gather',
                     '{"material":"silver","amount":99}');
            END;""")
        self._deny_then_retry("forge_low_id_after_receipt")

    def test_last_receipt_cannot_insert_foreign_room_gather(self):
        self._sql("""CREATE TRIGGER main.forge_room_after_receipt
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='a_host' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T00:00:00+00:00','Forgery','ungranted_room','gather',
                     '{"material":"silver","amount":99}');
            END;""")
        self._deny_then_retry("forge_room_after_receipt")

    def test_later_audit_cannot_follow_forged_prior_receipt_gather(self):
        self.world.clock.schedule("z_audit", due_at=self.now,
                                  kind="audit", payload={})
        self._sql("""CREATE TRIGGER main.forge_between_events
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='a_host' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T00:00:00+00:00','Forgery','atrium','gather',
                     '{"material":"silver","amount":55}');
            END;""")
        self._deny_then_retry("forge_between_events", expected_events=("a_host", "z_audit"))

    def test_late_rewrite_of_valid_legacy_gather_payload_is_denied(self):
        self._sql("""CREATE TRIGGER main.rewrite_after_receipt
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='a_host' BEGIN
              UPDATE events SET payload_json='{"material":"silver","amount":999}'
              WHERE kind='gather' AND payload_json LIKE '%silver%';
            END;""")
        self._deny_then_retry("rewrite_after_receipt")

    def test_legitimate_gather_and_unrelated_audit_say_replay_once(self):
        self.world.clock.schedule("z_audit", due_at=self.now,
                                  kind="audit", payload={})
        self.assertEqual(["a_host", "z_audit"],
                         [e.event_id for e in self.world.clock.tick(self.now, self._dispatch)])
        self.assertEqual([], self.world.clock.tick(self.now, self._dispatch))
        self.assertEqual(3, self.world.counts()[0])
        with closing(sqlite3.connect(self.world.dbpath)) as db:
            self.assertEqual((1,), db.execute(
                "SELECT COUNT(*) FROM action_receipts WHERE action_id='a_host'"
            ).fetchone())
            self.assertEqual((3,), db.execute(
                "SELECT remaining FROM gathering_nodes WHERE material='silver'"
            ).fetchone())
            self.assertEqual((1,), db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
