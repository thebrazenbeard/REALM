"""Exact economic effects for a legacy host-scheduled gather action."""
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


NOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)


class LegacyGatherEffectCustodyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "synthetic.sqlite3"
        Gateway(self.path, world_id="synthetic")
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.crafting = Crafting(self.path)
        self.crafting.place_node("atrium", "silver", stock=3, batch_yield=1)
        self.crafting.place_node("atrium", "iron", stock=6, batch_yield=3)
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def _sql(self, sql):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(sql)

    def _snapshot(self):
        tables = ("gathering_nodes", "material_pouches", "crafted_goods",
                  "built_structures", "action_receipts", "events")
        with closing(sqlite3.connect(self.path)) as db:
            return {table: db.execute(
                "SELECT * FROM main." + table + " ORDER BY 1,2").fetchall()
                    for table in tables}

    def _run(self, action_id="gather_once", command="gather silver"):
        self.clock.schedule(action_id, due_at=NOW, kind="host_action",
                            payload={"actor": "Ada", "command": command})

        def handle(db, event):
            return perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor=event.payload["actor"], command=event.payload["command"],
                action_id=event.event_id)

        return self.clock.tick(NOW, handle)

    def test_receipt_trigger_extra_inventory_cannot_ack(self):
        self._sql("""CREATE TRIGGER forge_after_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='gather_once' BEGIN
              UPDATE material_pouches SET quantity=quantity+99 WHERE actor='Ada';
            END""")
        before = self._snapshot()
        with self.assertRaisesRegex(ValueError, "[Ee]conomic|gather|effect|delta"):
            self._run()
        self.assertEqual(before, self._snapshot())
        self._sql("DROP TRIGGER forge_after_receipt;")
        self.assertEqual(["gather_once"], [e.event_id for e in self._run()])
        self.assertEqual(1, dict(((r[0], r[1]), r[2]) for r in
            self._snapshot()["material_pouches"])[("Ada", "silver")])

    def test_receipt_trigger_cross_actor_inventory_and_extra_node_rejected(self):
        self._sql("""CREATE TRIGGER forge_after_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='gather_once' BEGIN
              INSERT INTO material_pouches(actor,material,quantity)
                VALUES('Ben','silver',50);
              INSERT INTO gathering_nodes(room_id,material,remaining,batch_yield)
                VALUES('atrium','platinum',20,1);
            END""")
        before = self._snapshot()
        # Use the dispatcher directly to isolate its own in-handler guarantee
        # from the separate WorldClock final-postcondition verifier.
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                perform_in_transaction(db, engine=self.engine,
                    crafting=self.crafting, actor="Ada", command="gather silver",
                    action_id="gather_once")
            db.rollback()
        self.assertEqual(before, self._snapshot())

    def test_receipt_trigger_extra_gather_event_and_history_rewrite_rejected(self):
        self.assertEqual(["first"], [e.event_id for e in
            self._run("first", "gather iron")])
        self._sql("""CREATE TRIGGER forge_after_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='gather_once' BEGIN
              UPDATE events SET payload_json='{}' WHERE kind='gather';
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
                VALUES('2026-11-09T13:00:00+00:00','Ada','atrium','gather',
                       '{"material":"silver","amount":99}');
            END""")
        before = self._snapshot()
        with self.assertRaises(ValueError):
            self._run()
        self.assertEqual(before, self._snapshot())

    def test_receipt_trigger_cannot_delete_prior_action_receipt(self):
        self.assertEqual(["first"], [e.event_id for e in
            self._run("first", "gather iron")])
        self._sql("""CREATE TRIGGER forge_after_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='gather_once' BEGIN
              DELETE FROM action_receipts WHERE action_id='first';
            END""")
        before = self._snapshot()
        with self.assertRaises(ValueError):
            self._run()
        self.assertEqual(before, self._snapshot())

    def test_receipt_trigger_cannot_replace_exact_new_gather_event_id(self):
        # A forged replacement can preserve actor/material/amount while
        # deleting the event actually emitted by Crafting._gather.
        self._sql("""CREATE TRIGGER forge_after_receipt AFTER INSERT ON action_receipts
            WHEN NEW.action_id='gather_once' BEGIN
              DELETE FROM events WHERE kind='gather' AND actor='Ada';
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
                VALUES('2026-11-09T13:00:00+00:00','Ada','atrium','gather',
                       '{"amount": 1, "material": "silver"}');
            END""")
        before = self._snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                perform_in_transaction(db, engine=self.engine,
                    crafting=self.crafting, actor="Ada", command="gather silver",
                    action_id="gather_once")
            db.rollback()
        self.assertEqual(before, self._snapshot())

    def test_receipt_trigger_say_rejected_but_multi_batch_gather_still_works(self):
        self._sql("""CREATE TRIGGER benign_say AFTER INSERT ON action_receipts
            WHEN NEW.action_id='gather_once' BEGIN
              INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
                VALUES('2026-11-09T13:00:00+00:00','Ada','atrium','say',
                       '{"message":"ordinary"}');
            END""")
        before = self._snapshot()
        # Receipt-trigger speech has no independent authorization from this
        # typed gathering command, even when actor/room/payload look valid.
        with self.assertRaisesRegex(ValueError, "ungranted gameplay event"):
            self._run("gather_once", "gather silver 2")
        self.assertEqual(before, self._snapshot())
        self._sql("DROP TRIGGER benign_say;")
        self.assertEqual(["gather_once"], [e.event_id for e in
            self._run("gather_once", "gather silver 2")])
        snapshot = self._snapshot()
        self.assertEqual(0, len([row for row in snapshot["events"] if row[4] == "say"]))
        self.assertEqual(2, dict(((r[0], r[1]), r[2]) for r in
            snapshot["material_pouches"])[("Ada", "silver")])
        # Clock replay remains once-only; explicit action replay is also exact.
        self.assertEqual([], self._run("gather_once", "gather silver 2"))
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            response = perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor="Ada", command="gather silver 2", action_id="gather_once")
            self.assertEqual("You gather silver x2.", response)
            db.rollback()
        self.assertEqual(snapshot, self._snapshot())


if __name__ == "__main__":
    unittest.main()
