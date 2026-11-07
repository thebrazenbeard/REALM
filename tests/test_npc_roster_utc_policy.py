"""Transactional host NPC roster gather must fail closed and roll back as one UTC event."""
from __future__ import annotations

from contextlib import closing
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
from realm.npc_roster_allocation import propose_roster_allocation
from realm.world_clock import WorldClock

NOW = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)
SITES = (ResourceSite("atrium", "iron"), ResourceSite("atrium", "copper"))
HOST_KEY = b"synthetic-only-host-roster-secret-not-a-token-001"


class RosterUtcTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dbpath = Path(temp.name) / "roster.sqlite3"
        Gateway(self.dbpath, world_id="npc_roster")
        self.engine = Engine(self.dbpath)
        for actor in ("Ada", "Ben", "Cora"):
            self.engine.join(actor)
        self.crafting = Crafting(self.dbpath)
        for material in ("iron", "copper"):
            self.crafting.place_node("atrium", material, stock=3, batch_yield=3)
        self.clock = WorldClock(self.dbpath)
        self.clock.enable()

    def proposal(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            latest = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        return propose_roster_allocation(
            self.dbpath, expected_world_id="npc_roster",
            actors=("Ada", "Ben", "Cora"), sites=SITES,
            observed_through_event=latest, seed=17,
        )

    def grant(self, proposal, *, event_id="roster_test_1"):
        from realm.npc_roster_utc_policy import HostNpcRosterPolicy
        policy = HostNpcRosterPolicy(
            policy_id="roster_policy", world_id="npc_roster",
            proposal=proposal, sites=SITES, site_values=None,
            event_id=event_id, not_before_utc=NOW,
            expires_at_utc=NOW + timedelta(minutes=20),
        )
        mac = hmac.new(
            HOST_KEY, b"REALM/NPC-ROSTER-HOST-PERMIT/v1\x00" + policy.signing_bytes(),
            hashlib.sha256,
        ).hexdigest()
        self.clock.schedule(
            event_id, due_at=NOW, kind="npc_roster_gather",
            payload={"policy_id": "roster_policy", "world_id": "npc_roster"},
        )
        return policy, mac

    def run_grant(self, policy, mac):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        results = []
        def handler(db, event):
            result = perform_host_roster_gather_in_transaction(
                db, event, engine=self.engine, crafting=self.crafting,
                policy=policy, host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
            )
            results.append(result)
            return result
        self.clock.tick(NOW, handler)
        return results[0]

    def counts(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            return (
                db.execute("SELECT COUNT(*) FROM events WHERE kind='gather'").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0],
                db.execute("SELECT SUM(remaining) FROM gathering_nodes").fetchone()[0],
            )

    def test_three_actors_two_sites_wait_atomic_receipts(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        result = self.run_grant(policy, mac)
        self.assertEqual(result.executed, 2)
        self.assertEqual(result.waiting, 1)
        self.assertEqual(self.counts(), (2, 2, 0))
        self.assertIsNotNone(self.clock.lookup("roster_test_1").processed_at)
        self.assertEqual(result.proposal.evidence_sha256, plan.evidence_sha256)

    def test_two_actors_both_gather_under_exact_host_grant(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            latest = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        plan = propose_roster_allocation(
            self.dbpath, expected_world_id="npc_roster",
            actors=("Ada", "Ben"), sites=SITES,
            observed_through_event=latest, seed=7,
        )
        policy, mac = self.grant(plan)
        result = self.run_grant(policy, mac)
        self.assertEqual((result.executed, result.waiting), (2, 0))
        self.assertEqual(self.counts(), (2, 2, 0))

    def test_wrong_mac_denied_without_clock_receipt(self):
        plan = self.proposal()
        policy, _ = self.grant(plan)
        with self.assertRaises(ValueError):
            self.run_grant(policy, "0" * 64)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_stale_stock_fingerprint_denied(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("UPDATE main.gathering_nodes SET remaining=remaining-1 WHERE material='iron'")
        with self.assertRaises(ValueError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 5))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_failed_second_action_rolls_back_first_and_retries(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER fail_second_roster_receipt
                BEFORE INSERT ON main.action_receipts
                WHEN (SELECT COUNT(*) FROM main.action_receipts) >= 1
                BEGIN SELECT RAISE(ABORT, 'synthetic after-first fault'); END
            """)
        with self.assertRaises(sqlite3.DatabaseError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("DROP TRIGGER fail_second_roster_receipt")
        result = self.run_grant(policy, mac)
        self.assertEqual((result.executed, result.waiting), (2, 1))
        self.assertEqual(self.counts(), (2, 2, 0))


    def test_eight_actor_capacity_and_idempotent_clock(self):
        for actor in ("Dax", "Eli", "Faye", "Gio", "Hale"):
            self.engine.join(actor)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("UPDATE main.gathering_nodes SET remaining=6 WHERE material='iron'")
            db.execute("UPDATE main.gathering_nodes SET remaining=9 WHERE material='copper'")
        with closing(sqlite3.connect(self.dbpath)) as db:
            latest = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        plan = propose_roster_allocation(
            self.dbpath, expected_world_id="npc_roster",
            actors=("Hale", "Gio", "Faye", "Eli", "Dax", "Cora", "Ben", "Ada"),
            sites=SITES, observed_through_event=latest, seed=31,
        )
        policy, mac = self.grant(plan)
        result = self.run_grant(policy, mac)
        self.assertEqual((result.executed, result.waiting), (5, 3))
        self.assertEqual(self.counts(), (5, 5, 0))
        self.assertEqual(self.clock.tick(
            NOW, lambda db, event: self.fail("completed event replayed")
        ), [])
        self.assertEqual(self.counts(), (5, 5, 0))

    def test_tampered_roster_grant_rejected(self):
        from dataclasses import replace
        plan = self.proposal()
        policy, mac = self.grant(plan)
        forged = replace(policy, proposal=replace(plan, seed=99))
        with self.assertRaises(ValueError):
            self.run_grant(forged, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_foreign_database_connection_denied(self):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with tempfile.TemporaryDirectory() as dirpath:
            other = Path(dirpath) / "foreign.sqlite3"
            Gateway(other, world_id="foreign")
            with closing(sqlite3.connect(other)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                with self.assertRaises((ValueError, RuntimeError)):
                    perform_host_roster_gather_in_transaction(
                        db, self.clock.lookup("roster_test_1"),
                        engine=self.engine, crafting=self.crafting, policy=policy,
                        host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
                    )
                db.rollback()
        self.assertEqual(self.counts(), (0, 0, 6))

    def test_temp_shadowing_denied_without_receipts(self):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TEMP TABLE action_receipts(actor TEXT, action_id TEXT)")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises((ValueError, RuntimeError)):
                perform_host_roster_gather_in_transaction(
                    db, self.clock.lookup("roster_test_1"),
                    engine=self.engine, crafting=self.crafting, policy=policy,
                    host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
                )
            db.rollback()
        self.assertEqual(self.counts(), (0, 0, 6))


    def test_caught_partial_action_failure_leaves_no_roster_writes(self):
        """Even a trusted caller catching an exception must not retain actor #1."""
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""
                CREATE TRIGGER fail_second_roster_again
                BEFORE INSERT ON main.action_receipts
                WHEN (SELECT COUNT(*) FROM main.action_receipts) >= 1
                BEGIN SELECT RAISE(ABORT, 'synthetic partial-fault'); END
            """)
            db.execute("BEGIN IMMEDIATE")
            try:
                perform_host_roster_gather_in_transaction(
                    db, self.clock.lookup("roster_test_1"),
                    engine=self.engine, crafting=self.crafting, policy=policy,
                    host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
                )
            except sqlite3.DatabaseError:
                pass
            else:
                self.fail("Expected synthetic action fault")
            db.commit()  # Deliberately bad host code must not commit partial roster.
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)


    def test_caught_invalid_host_grant_cannot_acknowledge_clock_event(self):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan)
        def swallowed_failure(db, event):
            try:
                perform_host_roster_gather_in_transaction(
                    db, event, engine=self.engine, crafting=self.crafting,
                    policy=policy, host_key=HOST_KEY, grant_mac_hex="0" * 64,
                    now_utc=NOW,
                )
            except ValueError:
                pass
        with self.assertRaisesRegex(ValueError, "did not return exact"):
            self.clock.tick(NOW, swallowed_failure)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)
        result = self.run_grant(policy, mac)
        self.assertEqual((result.executed, result.waiting), (2, 1))

    def test_fabricated_typed_outcome_without_actor_receipts_is_denied(self):
        from realm.npc_roster_utc_policy import NpcRosterGatherResult
        plan = self.proposal()
        self.grant(plan)
        fake = NpcRosterGatherResult(
            event_id="roster_test_1", proposal=plan,
            executed=2, waiting=1, responses=("forged_1", "forged_2"),
        )
        with self.assertRaisesRegex(ValueError, "exact actor action receipt"):
            self.clock.tick(NOW, lambda db, event: fake)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_valid_mixed_wait_roster_acknowledges_exact_action_receipt(self):
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("UPDATE main.gathering_nodes SET remaining=0 WHERE material='copper'")
        plan = self.proposal()
        self.assertEqual(sum(a.site is None for a in plan.assignments), 2)
        policy, mac = self.grant(plan)
        result = self.run_grant(policy, mac)
        self.assertEqual((result.executed, result.waiting, len(result.responses)), (1, 2, 1))
        self.assertEqual(self.counts(), (1, 1, 0))
        self.assertIsNotNone(self.clock.lookup("roster_test_1").processed_at)

    def test_two_competing_signed_rosters_roll_back_complete_tick_batch(self):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy_a, mac_a = self.grant(plan, event_id="batch_a")
        policy_b, mac_b = self.grant(plan, event_id="batch_b")
        grants = {"batch_a": (policy_a, mac_a), "batch_b": (policy_b, mac_b)}
        def handler(db, event):
            policy, mac = grants[event.event_id]
            return perform_host_roster_gather_in_transaction(
                db, event, engine=self.engine, crafting=self.crafting,
                policy=policy, host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
            )
        with self.assertRaisesRegex(ValueError, "differs from host proposal"):
            self.clock.tick(NOW, handler)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("batch_a").processed_at)
        self.assertIsNone(self.clock.lookup("batch_b").processed_at)

    def test_missing_actor_receipt_after_real_action_rolls_back_clock(self):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan)
        def silently_removed_receipt(db, event):
            result = perform_host_roster_gather_in_transaction(
                db, event, engine=self.engine, crafting=self.crafting,
                policy=policy, host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
            )
            db.execute("DELETE FROM main.action_receipts WHERE actor='Ada'")
            db.execute("DELETE FROM main.action_receipts WHERE actor='Ben'")
            return result
        with self.assertRaisesRegex(ValueError, "exact actor action receipt"):
            self.clock.tick(NOW, silently_removed_receipt)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)


if __name__ == "__main__":
    unittest.main()
