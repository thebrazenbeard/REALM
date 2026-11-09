"""Adversarial synthetic-host NPC UTC action tests against real world ledgers."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.crafting import Crafting
from realm.engine import Engine
from realm.gateway import Gateway
from realm.npc_game_theory import ResourceSite
from realm.npc_utc_policy import (
    HostNpcGatherPolicy, make_host_npc_gather_handler,
    perform_host_npc_gather_in_transaction,
)
from realm.world_clock import DueEvent, WorldClock

BASE = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
KEY = b"synthetic-host-policy-test-key-32+bytes-0001"
DOMAIN = b"REALM/NPC-HOST-PERMIT/v1\x00"
SITES = (ResourceSite("atrium", "iron"), ResourceSite("atrium", "copper"))


class NpcUtcHostileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "real_world.sqlite3"
        Gateway(self.path, world_id="scarcity_world")
        self.engine = Engine(self.path)
        for actor in ("Ada", "Ben"):
            self.engine.join(actor)
        self.crafting = Crafting(self.path)
        for material in ("iron", "copper"):
            self.assertTrue(self.crafting.place_node("atrium", material, stock=3, batch_yield=3))
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def policy(self, event_id: str, actor: str, opponent: str, *, seed: int = 7):
        return HostNpcGatherPolicy(
            policy_id="policy_" + actor.lower(), world_id="scarcity_world",
            actor=actor, opponent=opponent, sites=SITES, seed=seed,
            event_id=event_id, not_before_utc=BASE,
            expires_at_utc=BASE + timedelta(minutes=5),
        )

    @staticmethod
    def mac(policy):
        return hmac.new(KEY, DOMAIN + policy.signing_bytes(), hashlib.sha256).hexdigest()

    def register(self, policy, *, payload=None, kind="npc_economic_gather", due=BASE):
        self.clock.schedule(policy.event_id, due_at=due, kind=kind, payload=(
            {"policy_id": policy.policy_id, "world_id": policy.world_id}
            if payload is None else payload))

    def handler(self, *policies, now=BASE):
        grants = {policy.event_id: (policy, self.mac(policy)) for policy in policies}
        return make_host_npc_gather_handler(
            engine=self.engine, crafting=self.crafting, host_key=KEY,
            grants_by_event_id=grants, now=lambda: now,
        )

    def counts(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM main.events WHERE kind='gather'").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM main.action_receipts").fetchone()[0],
                conn.execute("SELECT SUM(remaining) FROM main.gathering_nodes").fetchone()[0],
            )

    def test_two_competing_actors_get_real_scarce_stock_not_just_forecasts(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        ben = self.policy("npc_ben_1", "Ben", "Ada")
        self.register(ada)
        self.register(ben)
        results = []
        handler = self.handler(ada, ben)
        processed = self.clock.tick(BASE, lambda db, event: results.append(handler(db, event)))
        self.assertEqual([e.event_id for e in processed], ["npc_ada_1", "npc_ben_1"])
        self.assertEqual(self.counts(), (2, 2, 0))
        self.assertEqual({r.choice.site.material for r in results}, {"iron", "copper"})
        self.assertEqual({r.choice.actor for r in results}, {"Ada", "Ben"})
        self.assertTrue(all(r.choice.status == "VERIFIED_FOR_HOST_ACTION"
                            and r.choice.available_units == 3
                            and r.choice.forecast_denominator > 0
                            and len(r.choice.evidence_sha256) == 64 for r in results))
        with closing(sqlite3.connect(self.path)) as conn:
            pouches = conn.execute(
                "SELECT actor,quantity FROM main.material_pouches ORDER BY actor").fetchall()
            self.assertEqual(pouches, [("Ada", 3), ("Ben", 3)])
        self.assertEqual(self.clock.tick(BASE, handler), [])
        self.assertEqual(self.counts(), (2, 2, 0))
        self.assertIsNotNone(self.clock.lookup("npc_ada_1").processed_at)
        self.assertIsNotNone(self.clock.lookup("npc_ben_1").processed_at)

    def test_missing_host_grant_held_no_ledger_effect(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        with self.assertRaisesRegex(ValueError, "HELD"):
            self.clock.tick(BASE, self.handler())
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup(ada.event_id).processed_at)

    def test_event_cannot_self_authorize_actor_or_world(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada, payload={"policy_id": ada.policy_id,
                                    "world_id": "scarcity_world", "actor": "Ben"})
        with self.assertRaisesRegex(ValueError, "payload"):
            self.clock.tick(BASE, self.handler(ada))
        self.assertEqual(self.counts(), (0, 0, 6))

    def test_host_grant_binds_kind_world_actor_event_seed_and_utc(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        original_mac = self.mac(ada)
        alterations = (
            replace(ada, actor="Ben", opponent="Ada"),
            replace(ada, world_id="other_world"),
            replace(ada, seed=6),
            replace(ada, event_id="npc_ben_1"),
            replace(ada, expires_at_utc=BASE + timedelta(minutes=4)),
        )
        for changed in alterations:
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "grant"):
                    self.clock.tick(BASE, lambda db, event: perform_host_npc_gather_in_transaction(
                        db, event, engine=self.engine, crafting=self.crafting,
                        policy=changed, host_key=KEY, grant_mac_hex=original_mac, now_utc=BASE))
                self.assertEqual(self.counts(), (0, 0, 6))
        with self.assertRaisesRegex(ValueError, "window"):
            self.clock.tick(BASE + timedelta(minutes=6), self.handler(ada, now=BASE + timedelta(minutes=6)))
        self.assertEqual(self.counts(), (0, 0, 6))

    def test_unknown_event_kind_rejected_without_receipt(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada, kind="some_unknown_model_kind")
        with self.assertRaisesRegex(ValueError, "event"):
            self.clock.tick(BASE, self.handler(ada))
        self.assertEqual(self.counts(), (0, 0, 6))

    def test_host_kill_before_commit_rolls_back_stock_action_and_clock_receipt(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        handler = self.handler(ada)
        def interrupted(db, event):
            handler(db, event)
            raise RuntimeError("simulated host failure after gameplay")
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            self.clock.tick(BASE, interrupted)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup(ada.event_id).processed_at)
        self.assertEqual(len(self.clock.tick(BASE, handler)), 1)
        self.assertEqual(self.counts(), (1, 1, 3))

    def test_temp_table_spoof_fails_closed(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        handler = self.handler(ada)
        def spoofed(db, event):
            db.execute("CREATE TEMP TABLE players(id TEXT)")
            handler(db, event)
        with self.assertRaisesRegex(RuntimeError, "TEMP"):
            self.clock.tick(BASE, spoofed)
        self.assertEqual(self.counts(), (0, 0, 6))

    def test_direct_duplicate_invocation_rejected_after_first_receipt(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        handler = self.handler(ada)
        self.clock.tick(BASE, handler)
        before = self.counts()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            event = DueEvent(ada.event_id, BASE, "npc_economic_gather",
                             {"policy_id": ada.policy_id, "world_id": ada.world_id})
            with self.assertRaisesRegex(ValueError, "already executed"):
                handler(db, event)
            db.rollback()
        self.assertEqual(self.counts(), before)

    def test_direct_unregistered_event_object_is_not_a_real_clock_event(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        handler = self.handler(ada)
        event = DueEvent(ada.event_id, BASE, "npc_economic_gather",
                         {"policy_id": ada.policy_id, "world_id": ada.world_id})
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "registered"):
                handler(db, event)
            db.rollback()
        self.assertEqual(self.counts(), (0, 0, 6))

    def test_signed_unprovisioned_actor_still_cannot_join_itself(self):
        rogue = self.policy("npc_mallory_1", "Mallory", "Ada")
        self.register(rogue)
        with self.assertRaisesRegex(ValueError, "pre-provision"):
            self.clock.tick(BASE, self.handler(rogue))
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup(rogue.event_id).processed_at)

    def test_same_world_writer_lock_prevents_adjudication(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        holder = sqlite3.connect(self.path, timeout=0.05)
        contender = sqlite3.connect(self.path, timeout=0.05)
        try:
            contender.row_factory = sqlite3.Row
            contender.execute("PRAGMA foreign_keys=ON")
            holder.execute("BEGIN IMMEDIATE")
            contender.execute("BEGIN")
            event = DueEvent(ada.event_id, BASE, "npc_economic_gather",
                             {"policy_id": ada.policy_id, "world_id": ada.world_id})
            with self.assertRaises((ValueError, sqlite3.OperationalError)):
                self.handler(ada)(contender, event)
            contender.rollback()
            holder.rollback()
        finally:
            contender.close()
            holder.close()
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup(ada.event_id).processed_at)

    def test_opt_in_foreground_runner_consumes_only_registered_host_event(self):
        from realm.clock_runner import run_foreground
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        report = run_foreground(
            self.clock,
            {"npc_economic_gather": self.handler(ada)},
            pulses=1, now=lambda: BASE, monotonic=lambda: 0.0,
            sleep=lambda _: self.fail("A one-pulse runner must never sleep"),
        )
        self.assertEqual(report.pulses, 1)
        self.assertEqual(report.event_ids, ("npc_ada_1",))
        self.assertEqual(self.counts(), (1, 1, 3))

    def test_competing_parallel_clock_pulses_serialize_without_double_spend(self):
        from concurrent.futures import ThreadPoolExecutor
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        ben = self.policy("npc_ben_1", "Ben", "Ada")
        self.register(ada)
        self.register(ben)
        handler = self.handler(ada, ben)
        with ThreadPoolExecutor(max_workers=2) as pool:
            groups = list(pool.map(lambda _: self.clock.tick(BASE, handler), range(2)))
        committed = [event.event_id for group in groups for event in group]
        self.assertEqual(sorted(committed), ["npc_ada_1", "npc_ben_1"])
        self.assertEqual(self.counts(), (2, 2, 0))
        restarted_clock = WorldClock(self.path)
        self.assertEqual(restarted_clock.tick(BASE, handler), [])
        self.assertEqual(self.counts(), (2, 2, 0))

    def test_wrong_host_key_rejected_without_gameplay(self):
        ada = self.policy("npc_ada_1", "Ada", "Ben")
        self.register(ada)
        wrong_host = make_host_npc_gather_handler(
            engine=self.engine, crafting=self.crafting,
            host_key=b"wrong-host-key-does-not-possess-valid-grant-0001",
            grants_by_event_id={ada.event_id: (ada, self.mac(ada))},
            now=lambda: BASE,
        )
        with self.assertRaisesRegex(ValueError, "grant"):
            self.clock.tick(BASE, wrong_host)
        self.assertEqual(self.counts(), (0, 0, 6))

if __name__ == "__main__":
    unittest.main()
