"""Legacy UTC economy grants require the selected host's new action receipt."""
from __future__ import annotations

import sqlite3
import hashlib
import hmac
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.clock_gameplay import perform_in_transaction
from realm import clock_gameplay
from realm.crafting import Crafting
from realm.engine import Engine
from realm.gateway import Gateway
from realm.npc_game_theory import ResourceSite
from realm.npc_utc_policy import (HostNpcGatherPolicy,
                                  perform_host_npc_gather_in_transaction)
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
        self.crafting.place_node("atrium", "copper", stock=5, batch_yield=1)
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

    def raw_copper(self, db):
        db.execute("UPDATE gathering_nodes SET remaining=remaining-1 "
                   "WHERE room_id='atrium' AND material='copper'")
        db.execute("INSERT INTO material_pouches VALUES (?,?,?) "
                   "ON CONFLICT(actor,material) DO UPDATE "
                   "SET quantity=quantity+excluded.quantity",
                   ("Ada", "copper", 1))

    def typed_iron(self, db, event):
        return perform_in_transaction(
            db, engine=self.engine, crafting=self.crafting,
            actor="Ada", command="gather iron", action_id=event.event_id)

    def assert_unattested_rejected_without_effect(self, callback):
        before = self.state()
        with self.assertRaisesRegex(ValueError, "typed.*proof|economic.*attestation"):
            self.clock.tick(NOW, callback)
        self.assertEqual(before, self.state())
        self.assertIsNone(self.clock.lookup("due").processed_at)

    def test_matching_host_receipt_does_not_grant_wrong_material(self):
        self.queue()
        def callback(db, event):
            self.raw_copper(db)
            db.execute("INSERT INTO action_receipts VALUES(?,?,?,?)",
                       ("@clock:Ada", "due", "gather iron", "fake iron"))
        self.assert_unattested_rejected_without_effect(callback)

    def test_raw_economy_change_before_valid_typed_action_is_rejected(self):
        self.queue()
        def callback(db, event):
            self.raw_copper(db)
            return self.typed_iron(db, event)
        self.assert_unattested_rejected_without_effect(callback)

    def test_raw_economy_change_after_valid_typed_action_is_rejected(self):
        self.queue()
        def callback(db, event):
            result = self.typed_iron(db, event)
            self.raw_copper(db)
            return result
        self.assert_unattested_rejected_without_effect(callback)

    def test_typed_action_with_unrelated_audit_speech_keeps_its_grant(self):
        self.queue()
        def callback(db, event):
            db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                       "VALUES(?,?,?,?,?)",
                       (NOW.isoformat(), "Ada", "atrium", "say", '{"text":"audit"}'))
            self.typed_iron(db, event)
            db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                       "VALUES(?,?,?,?,?)",
                       (NOW.isoformat(), "Ada", "atrium", "say", '{"text":"after"}'))
        self.assertEqual(["due"], [e.event_id for e in self.clock.tick(NOW, callback)])

    def test_typed_economy_proof_reuses_existing_verified_snapshots(self):
        """Do not rescan all economy tables twice inside each signed adapter."""
        self.queue()
        with mock.patch.object(clock_gameplay, "_clock_economy_state",
                               wraps=clock_gameplay._clock_economy_state) as reads:
            self.assertEqual(["due"], [e.event_id for e in
                                       self.clock.tick(NOW, self.typed_iron)])
        # The independent WorldClock pre/post snapshots remain mandatory;
        # this counts only redundant *adapter* rescans.
        self.assertEqual(0, reads.call_count)

    def test_projected_economy_state_equals_sql_snapshot(self):
        """Typed verifier projection must use the clock's exact row domain."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            for kind, material in (("say", "ignored"), ("gather", "iron"),
                                   ("craft", "copper"), ("build", "iron")):
                db.execute(
                    "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    (NOW.isoformat(), "Ada", "atrium", kind,
                     '{"material":"' + material + '"}'),
                )
            self.assertEqual(
                clock_gameplay._clock_economy_state(db),
                clock_gameplay._clock_economy_projection(
                    clock_gameplay._gather_evidence(db)),
            )

    def test_signed_npc_typed_adapter_proof_survives_void_wrapper(self):
        policy = HostNpcGatherPolicy(
            policy_id="signed_ada", world_id="world_legacy_economy",
            actor="Ada", opponent="Ben",
            sites=(ResourceSite("atrium", "iron"), ResourceSite("atrium", "copper")),
            seed=7, event_id="npc_due", not_before_utc=NOW,
            expires_at_utc=NOW + timedelta(minutes=5),
        )
        key = b"synthetic-private-host-test-key-only-0001"
        mac = hmac.new(key, b"REALM/NPC-HOST-PERMIT/v1\x00" +
                       policy.signing_bytes(), hashlib.sha256).hexdigest()
        self.clock.schedule("npc_due", due_at=NOW, kind="npc_economic_gather",
                            payload={"policy_id": policy.policy_id,
                                     "world_id": policy.world_id})
        responses = []
        # A real registered callback may discard the adapter's return value.
        def callback(db, event):
            responses.append(perform_host_npc_gather_in_transaction(
                db, event, engine=self.engine, crafting=self.crafting,
                policy=policy, host_key=key, grant_mac_hex=mac, now_utc=NOW))
        self.assertEqual(["npc_due"], [e.event_id for e in
                                       self.clock.tick(NOW, callback)])
        self.assertEqual(1, len(responses))

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
