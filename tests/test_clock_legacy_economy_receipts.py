"""Legacy UTC economy grants require the selected host's new action receipt."""
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


NOW = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)


class LegacyEconomyReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        Gateway(self.path, world_id="world_legacy_economy")
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.crafting = Crafting(self.path)
        self.crafting.place_node("atrium", "iron", stock=5, batch_yield=1)
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def queue(self, event_id="due", *, actor="Ada", command="gather iron"):
        self.clock.schedule(event_id, due_at=NOW, kind="host_action",
                            payload={"actor": actor, "command": command})

    def state(self):
        with closing(sqlite3.connect(self.path)) as db:
            return tuple(tuple(db.execute(sql).fetchall()) for sql in (
                "SELECT room_id,material,remaining FROM gathering_nodes ORDER BY 1,2",
                "SELECT actor,material,quantity FROM material_pouches ORDER BY 1,2",
                "SELECT actor,kind,payload_json FROM events ORDER BY id",
                "SELECT actor,action_id,command FROM action_receipts ORDER BY 1,2",
            ))

    def unsigned_gather(self, db, event):
        # Direct internal domain emitter, deliberately missing the typed
        # host adapter's validation and @clock: receipt.
        return self.crafting._gather(db, "Ada", "atrium", "iron")

    def assert_rejected_without_effect(self, callback):
        before = self.state()
        with self.assertRaisesRegex(ValueError, "host.*receipt|receipt.*host"):
            self.clock.tick(NOW, callback)
        self.assertEqual(before, self.state())
        self.assertIsNone(self.clock.lookup("due").processed_at)

    def test_other_players_existing_receipt_cannot_authorize_economy(self):
        self.queue()
        self.engine.act("Ben", "help", action_id="due")
        self.assert_rejected_without_effect(self.unsigned_gather)

    def test_preexisting_host_receipt_cannot_authorize_new_economy(self):
        self.queue()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO action_receipts VALUES(?,?,?,?)",
                       ("@clock:Ada", "due", "gather iron", "stale"))
        self.assert_rejected_without_effect(self.unsigned_gather)

    def test_new_receipt_for_different_host_actor_cannot_authorize(self):
        self.queue()
        def callback(db, event):
            self.unsigned_gather(db, event)
            db.execute("INSERT INTO action_receipts VALUES(?,?,?,?)",
                       ("@clock:Ben", "due", "gather iron", "wrong actor"))
        self.assert_rejected_without_effect(callback)

    def test_new_receipt_for_different_command_cannot_authorize(self):
        self.queue()
        def callback(db, event):
            self.unsigned_gather(db, event)
            db.execute("INSERT INTO action_receipts VALUES(?,?,?,?)",
                       ("@clock:Ada", "due", "gather copper", "wrong command"))
        self.assert_rejected_without_effect(callback)

    def test_receipt_for_queued_actor_cannot_sign_different_gather_actor(self):
        self.queue()
        def callback(db, event):
            self.crafting._gather(db, "Ben", "atrium", "iron")
            db.execute("INSERT INTO action_receipts VALUES(?,?,?,?)",
                       ("@clock:Ada", "due", "gather iron", "wrong beneficiary"))
        self.assert_rejected_without_effect(callback)

    def test_receipt_for_queued_actor_cannot_sign_different_pouch_actor(self):
        self.queue()
        def callback(db, event):
            db.execute("INSERT INTO material_pouches VALUES (?,?,?)",
                       ("Ben", "iron", 2))
            db.execute("INSERT INTO action_receipts VALUES(?,?,?,?)",
                       ("@clock:Ada", "due", "gather iron", "wrong beneficiary"))
        self.assert_rejected_without_effect(callback)

    def test_new_gather_event_without_inventory_deltas_needs_receipt(self):
        self.queue()
        self.engine.act("Ben", "help", action_id="due")
        def callback(db, event):
            db.execute(
                "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                "VALUES(?,?,?,?,?)",
                (NOW.isoformat(), "Ada", "atrium", "gather",
                 '{"material":"iron","amount":0}'),
            )
        self.assert_rejected_without_effect(callback)

    def test_typed_adapter_succeeds_despite_other_players_collision(self):
        self.queue()
        self.engine.act("Ben", "help", action_id="due")
        def callback(db, event):
            return perform_in_transaction(
                db, engine=self.engine, crafting=self.crafting,
                actor="Ada", command="gather iron", action_id=event.event_id)
        self.assertEqual(["due"], [e.event_id for e in self.clock.tick(NOW, callback)])
        self.assertIsNotNone(self.clock.lookup("due").processed_at)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM action_receipts "
                "WHERE actor='@clock:Ada' AND action_id='due'"
            ).fetchone()[0])

    def test_no_economic_delta_needs_no_receipt(self):
        self.queue()
        self.assertEqual(["due"], [e.event_id for e in
                                    self.clock.tick(NOW, lambda db, event: None)])


if __name__ == "__main__":
    unittest.main()
