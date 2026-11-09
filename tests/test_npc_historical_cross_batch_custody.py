"""Historical NPC economy custody on later UTC host batches, synthetic only.

All triggers here are pre-installed privileged SQLite hooks in a disposable
world, not remotely controlled players or a claim of cryptographic integrity.
"""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import test_npc_roster_utc_policy as roster_fixture


class HistoricalNpcCustodyTests(unittest.TestCase):
    def setUp(self):
        fx = roster_fixture.RosterUtcTests("test_three_actors_two_sites_wait_atomic_receipts")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        self.fx = fx
        policy, mac = fx.grant(fx.proposal(), event_id="a_roster")
        fx.run_grant(policy, mac)
        self.assertEqual(2, fx.counts()[0])
        self.later = roster_fixture.NOW + timedelta(seconds=1)

    def _sql(self, statements):
        with closing(sqlite3.connect(self.fx.dbpath)) as db:
            db.executescript(statements)

    def _history(self):
        with closing(sqlite3.connect(self.fx.dbpath)) as db:
            return (
                db.execute("SELECT * FROM events ORDER BY id").fetchall(),
                db.execute("SELECT * FROM gathering_nodes ORDER BY room_id,material").fetchall(),
                db.execute("SELECT * FROM material_pouches ORDER BY actor,material").fetchall(),
                db.execute("SELECT * FROM clock_events ORDER BY event_id").fetchall(),
                db.execute("SELECT * FROM clock_state").fetchall(),
            )

    @staticmethod
    def _audit(db, event):
        if event.kind != "audit":
            raise ValueError("Only synthetic audit expected")
        db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                   "VALUES(?,?,?,?,?)", (event.due_at.isoformat(), "Ada", "atrium",
                                        "say", '{"text":"permitted audit"}'))

    def _reject_retry_audit(self, malicious_dml):
        self.fx.clock.schedule("z_audit", due_at=self.later, kind="audit", payload={})
        self._sql("CREATE TRIGGER main.npc_history_tamper "
                  "AFTER UPDATE OF processed_utc ON clock_events "
                  "WHEN NEW.event_id='z_audit' BEGIN " + malicious_dml + " END;")
        before = self._history()
        with self.assertRaisesRegex(ValueError, "Roster"):
            self.fx.clock.tick(self.later, self._audit)
        self.assertEqual(before, self._history())
        self.assertIsNone(self.fx.clock.lookup("z_audit").processed_at)
        self._sql("DROP TRIGGER main.npc_history_tamper;")
        self.assertEqual(["z_audit"], [e.event_id for e in
                         self.fx.clock.tick(self.later, self._audit)])
        self.assertEqual([], self.fx.clock.tick(self.later, self._audit))
        self.assertEqual(2, self.fx.counts()[0])

    def test_later_audit_deleting_old_signed_gather_rolls_back_then_retries(self):
        self._reject_retry_audit("DELETE FROM events WHERE kind='gather';")

    def test_later_audit_rewriting_old_signed_payload_rolls_back(self):
        self._reject_retry_audit(
            "UPDATE events SET payload_json='{\"material\":\"gold\",\"amount\":99}' "
            "WHERE kind='gather';")

    def test_later_audit_forged_negative_id_gather_rolls_back(self):
        self._reject_retry_audit(
            "INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json) "
            "VALUES(-11,'2026-11-09T00:00:00+00:00','Forgery','atrium','gather',"
            "'{\"material\":\"iron\",\"amount\":99}');")

    def test_later_audit_forged_unknown_room_gather_rolls_back(self):
        self._reject_retry_audit(
            "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
            "VALUES('2026-11-09T00:00:00+00:00','Forgery','ungranted_room','gather',"
            "'{\"material\":\"iron\",\"amount\":99}');")

    def test_later_audit_extra_resource_stock_rolls_back(self):
        self._reject_retry_audit(
            "UPDATE gathering_nodes SET remaining=remaining+3 WHERE material='iron';")

    def test_later_audit_extra_pouch_inventory_rolls_back(self):
        self._reject_retry_audit(
            "UPDATE material_pouches SET quantity=quantity+99 WHERE material='iron';")

    def test_later_audit_old_join_rewritten_into_gather_rolls_back(self):
        self._reject_retry_audit("UPDATE events SET kind='gather' WHERE kind='join';")

    def test_idle_utc_watermark_cannot_erase_prior_gather(self):
        before = self._history()
        self._sql("""CREATE TRIGGER main.npc_idle_tamper
            AFTER UPDATE OF last_utc ON clock_state
            BEGIN DELETE FROM events WHERE kind='gather'; END;""")
        with self.assertRaisesRegex(ValueError, "Roster"):
            self.fx.clock.tick(self.later, self._audit)
        self.assertEqual(before, self._history())
        self._sql("DROP TRIGGER main.npc_idle_tamper;")
        self.assertEqual([], self.fx.clock.tick(self.later, self._audit))
        self.assertEqual(2, self.fx.counts()[0])

    def test_late_host_finalizer_cannot_erase_old_gather(self):
        class MisbehavingFinalizer:
            def __call__(self, db, event):
                raise ValueError("No due events expected")

            def verify_final_precommit(self, db, processed):
                self_outer.assertEqual((), processed)
                db.execute("DELETE FROM main.events WHERE kind='gather'")

        self_outer = self
        before = self._history()
        with self.assertRaisesRegex(ValueError, "Roster"):
            self.fx.clock.tick(self.later, MisbehavingFinalizer())
        self.assertEqual(before, self._history())

    def test_legitimate_audit_only_appends_say_and_preserves_old_gathers(self):
        self.fx.clock.schedule("z_audit", due_at=self.later, kind="audit", payload={})
        self.assertEqual(["z_audit"], [e.event_id for e in
                         self.fx.clock.tick(self.later, self._audit)])
        self.assertEqual([], self.fx.clock.tick(self.later, self._audit))
        self.assertEqual(2, self.fx.counts()[0])
        with closing(sqlite3.connect(self.fx.dbpath)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'"
            ).fetchone()[0])

    def test_legacy_host_action_cannot_delete_previously_signed_gathers(self):
        # The legacy handler has its own action/receipt contract. Its current
        # inventory effects are a bounded exception, but prior event history
        # must not disappear under that exception.
        self.fx.clock.schedule("z_host", due_at=self.later,
                               kind="host_action", payload={"actor": "Ada",
                                                            "command": "gather iron"})
        before = self._history()
        with self.assertRaisesRegex(ValueError, "Roster"):
            self.fx.clock.tick(self.later, lambda db, event:
                               db.execute("DELETE FROM events WHERE kind='gather'"))
        self.assertEqual(before, self._history())
        self.assertIsNone(self.fx.clock.lookup("z_host").processed_at)

    def test_legacy_host_action_can_commit_new_receipted_gather(self):
        from realm.clock_gameplay import perform_in_transaction

        self.assertTrue(self.fx.crafting.place_node(
            "atrium", "silver", stock=3, batch_yield=1))
        self.fx.clock.schedule("z_host", due_at=self.later,
                               kind="host_action", payload={"actor": "Ada",
                                                            "command": "gather silver"})

        def validated(db, event):
            return perform_in_transaction(
                db, engine=self.fx.engine, crafting=self.fx.crafting,
                actor="Ada", command="gather silver", action_id=event.event_id,
            )

        self.assertEqual(["z_host"], [e.event_id for e in
                         self.fx.clock.tick(self.later, validated)])
        self.assertEqual([], self.fx.clock.tick(self.later, validated))
        self.assertEqual(3, self.fx.counts()[0])
        with closing(sqlite3.connect(self.fx.dbpath)) as db:
            self.assertEqual((2,), db.execute(
                "SELECT remaining FROM gathering_nodes "
                "WHERE room_id='atrium' AND material='silver'"
            ).fetchone())
            self.assertEqual((1,), db.execute(
                "SELECT COUNT(*) FROM action_receipts "
                "WHERE actor='Ada' AND action_id='z_host'"
            ).fetchone())

    def test_legacy_gather_late_clock_receipt_cannot_grant_extra_pouch(self):
        from realm.clock_gameplay import perform_in_transaction

        self.assertTrue(self.fx.crafting.place_node(
            "atrium", "silver", stock=3, batch_yield=1))
        self.fx.clock.schedule("a_host", due_at=self.later,
                               kind="host_action", payload={"actor": "Ada",
                                                            "command": "gather silver"})
        self._sql("""CREATE TRIGGER main.late_forged_pouch
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='a_host' BEGIN
              UPDATE material_pouches SET quantity=quantity+99 WHERE actor='Ada';
            END;""")

        def permitted(db, event):
            return perform_in_transaction(
                db, engine=self.fx.engine, crafting=self.fx.crafting,
                actor="Ada", command="gather silver", action_id=event.event_id,
            )

        before = self._history()
        with self.assertRaisesRegex(ValueError, "Roster pouch"):
            self.fx.clock.tick(self.later, permitted)
        self.assertEqual(before, self._history())
        self.assertIsNone(self.fx.clock.lookup("a_host").processed_at)
        self._sql("DROP TRIGGER main.late_forged_pouch;")
        self.assertEqual(["a_host"], [e.event_id for e in
                         self.fx.clock.tick(self.later, permitted)])
        self.assertEqual([], self.fx.clock.tick(self.later, permitted))
        self.assertEqual(3, self.fx.counts()[0])

    def test_next_event_does_not_adopt_prior_legacy_receipt_trigger_drift(self):
        from realm.clock_gameplay import perform_in_transaction

        self.assertTrue(self.fx.crafting.place_node(
            "atrium", "silver", stock=3, batch_yield=1))
        self.fx.clock.schedule("a_host", due_at=self.later,
                               kind="host_action", payload={"actor": "Ada",
                                                            "command": "gather silver"})
        self.fx.clock.schedule("z_audit", due_at=self.later, kind="audit", payload={})
        self._sql("""CREATE TRIGGER main.late_forged_stock
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='a_host' BEGIN
              UPDATE gathering_nodes SET remaining=remaining+100;
            END;""")

        def dispatch(db, event):
            if event.kind == "host_action":
                return perform_in_transaction(
                    db, engine=self.fx.engine, crafting=self.fx.crafting,
                    actor="Ada", command="gather silver", action_id=event.event_id,
                )
            return self._audit(db, event)

        before = self._history()
        with self.assertRaisesRegex(ValueError, "Roster inventory drifted"):
            self.fx.clock.tick(self.later, dispatch)
        self.assertEqual(before, self._history())
        self._sql("DROP TRIGGER main.late_forged_stock;")
        self.assertEqual(["a_host", "z_audit"], [e.event_id for e in
                         self.fx.clock.tick(self.later, dispatch)])
        self.assertEqual([], self.fx.clock.tick(self.later, dispatch))


if __name__ == "__main__":
    unittest.main()
