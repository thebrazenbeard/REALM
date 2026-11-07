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

    def test_swallowed_mid_roster_actor_fault_does_not_acknowledge_event(self):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER fail_second_ignored_actor
                BEFORE INSERT ON main.action_receipts
                WHEN (SELECT COUNT(*) FROM main.action_receipts) >= 1
                BEGIN SELECT RAISE(ABORT, 'synthetic mid-roster fault'); END
            """)
        def swallowed_mid_roster_fault(db, event):
            try:
                return perform_host_roster_gather_in_transaction(
                    db, event, engine=self.engine, crafting=self.crafting,
                    policy=policy, host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
                )
            except sqlite3.DatabaseError:
                return None
        with self.assertRaisesRegex(ValueError, "did not return exact"):
            self.clock.tick(NOW, swallowed_mid_roster_fault)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("DROP TRIGGER fail_second_ignored_actor")
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


    def test_all_wait_foreign_world_forgery_never_acks_roster(self):
        """A callback's structurally valid empty answer is not a signed run."""
        from dataclasses import replace
        from realm.npc_roster_utc_policy import NpcRosterGatherResult
        plan = self.proposal()
        self.grant(plan)
        spoof = replace(
            plan, world_id="foreign_spoof",
            assignments=tuple(replace(a, site=None, expected_units=0,
                                      expected_utility=0) for a in plan.assignments),
            total_expected_units=0, total_expected_utility=0,
        )
        outcome = NpcRosterGatherResult(
            event_id="roster_test_1", proposal=spoof, executed=0,
            waiting=len(spoof.assignments), responses=(),
        )
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, lambda db, event: outcome)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_fake_actor_receipts_without_signed_host_execution_never_ack(self):
        """Matching actor receipts alone cannot substitute for host execution."""
        from realm.npc_roster_utc_policy import NpcRosterGatherResult
        plan = self.proposal()
        self.grant(plan)
        actions = tuple(a for a in plan.assignments if a.site is not None)
        fabricated = NpcRosterGatherResult(
            "roster_test_1", plan, len(actions), len(plan.assignments) - len(actions),
            tuple("synthetic" for _ in actions),
        )
        def inject_fake_receipts(db, event):
            for assignment in actions:
                action_id = "npcR_" + hashlib.sha256(
                    f"{event.event_id}|{assignment.actor}".encode()
                ).hexdigest()[:50]
                db.execute(
                    "INSERT INTO main.action_receipts(actor,action_id,command,response) "
                    "VALUES(?,?,?,?)",
                    (assignment.actor, action_id, "gather " + assignment.site.material,
                     "synthetic"),
                )
            return fabricated
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, inject_fake_receipts)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_legitimate_execution_with_forged_world_result_never_ack(self):
        from dataclasses import replace
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan)
        def tamper_result(db, event):
            real = perform_host_roster_gather_in_transaction(
                db, event, engine=self.engine, crafting=self.crafting,
                policy=policy, host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
            )
            return replace(real, proposal=replace(real.proposal, world_id="foreign"))
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, tamper_result)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_unrelated_generic_clock_handler_still_acknowledges(self):
        self.clock.schedule("generic_1", due_at=NOW, kind="ordinary", payload={})
        processed = self.clock.tick(NOW, lambda db, event: None)
        self.assertEqual(len(processed), 1)
        self.assertIsNotNone(self.clock.lookup("generic_1").processed_at)
        self.assertEqual(self.clock.tick(NOW, lambda db, event: None), [])


    def test_postverify_clock_update_trigger_cannot_delete_roster_attestation(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER main.delete_attest_after_clock_ack
                AFTER UPDATE OF processed_utc ON clock_events
                WHEN NEW.event_id='roster_test_1'
                BEGIN
                    DELETE FROM npc_roster_utc_attestations
                    WHERE event_id=NEW.event_id;
                END
            """)
        with self.assertRaises(ValueError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_postverify_clock_update_trigger_cannot_tamper_roster_digest(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER main.tamper_attest_digest_after_clock_ack
                AFTER UPDATE OF processed_utc ON clock_events
                WHEN NEW.event_id='roster_test_1'
                BEGIN
                    UPDATE npc_roster_utc_attestations
                    SET outcome_sha256='0000000000000000000000000000000000000000000000000000000000000000'
                    WHERE event_id=NEW.event_id;
                END
            """)
        with self.assertRaises(ValueError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_postverify_clock_update_trigger_cannot_tamper_valid_mac(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER main.tamper_attest_mac_after_clock_ack
                AFTER UPDATE OF processed_utc ON clock_events
                WHEN NEW.event_id='roster_test_1'
                BEGIN
                    UPDATE npc_roster_utc_attestations
                    SET execution_mac_hex='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
                    WHERE event_id=NEW.event_id;
                END
            """)
        with self.assertRaises(ValueError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)


    def test_postverify_clock_state_trigger_cannot_delete_roster_attestation(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER main.delete_attest_after_clock_state
                AFTER INSERT ON clock_state
                BEGIN DELETE FROM npc_roster_utc_attestations; END
            """)
        with self.assertRaises(ValueError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def test_postverify_later_generic_batch_event_cannot_delete_roster_attestation(self):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        plan = self.proposal()
        policy, mac = self.grant(plan, event_id="a_roster")
        self.clock.schedule("z_generic", due_at=NOW, kind="ordinary", payload={})
        def handler(db, event):
            if event.kind == "npc_roster_gather":
                return perform_host_roster_gather_in_transaction(
                    db, event, engine=self.engine, crafting=self.crafting,
                    policy=policy, host_key=HOST_KEY, grant_mac_hex=mac, now_utc=NOW,
                )
            db.execute("DELETE FROM main.npc_roster_utc_attestations WHERE event_id='a_roster'")
            return None
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, handler)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("a_roster").processed_at)
        self.assertIsNone(self.clock.lookup("z_generic").processed_at)

    def test_postverify_clock_trigger_cannot_change_expected_processed_utc(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER main.tamper_processed_utc_after_clock_ack
                AFTER UPDATE OF processed_utc ON clock_events
                WHEN NEW.event_id='roster_test_1'
                BEGIN
                    UPDATE clock_events SET processed_utc='2099-01-01T00:00:00.000000+00:00'
                    WHERE event_id=NEW.event_id;
                END
            """)
        # Both the generic clock integrity guard and the roster-specific
        # postcondition can reject this trigger; either must roll back effects.
        with self.assertRaises((ValueError, RuntimeError)):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)

    def _postverify_material_trigger(self, body):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute(
                "CREATE TRIGGER main.tamper_roster_materials "
                "AFTER UPDATE OF processed_utc ON clock_events "
                "WHEN NEW.event_id='roster_test_1' BEGIN " + body + " END"
            )
        with self.assertRaises(ValueError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)
        with closing(sqlite3.connect(self.dbpath)) as db:
            self.assertEqual(db.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM material_pouches"
            ).fetchone()[0], 0)
        # The rejected tick must be safe to retry once the hostile fixture
        # is removed, without duplicate receipts or partial stock changes.
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("DROP TRIGGER main.tamper_roster_materials")
        self.assertEqual(self.run_grant(policy, mac).executed, 2)
        self.assertEqual(self.counts(), (2, 2, 0))
        self.assertEqual(self.clock.tick(
            NOW, lambda db, event: self.fail("completed roster replayed")
        ), [])

    def test_postverify_material_pouches_cannot_be_deleted(self):
        self._postverify_material_trigger("DELETE FROM material_pouches;")

    def test_postverify_gathering_stock_cannot_be_replenished(self):
        self._postverify_material_trigger(
            "UPDATE gathering_nodes SET remaining=3;"
        )

    def test_postverify_gather_events_cannot_be_deleted(self):
        self._postverify_material_trigger(
            "DELETE FROM events WHERE kind='gather';"
        )

    def test_postverify_material_pouch_cannot_be_inflated(self):
        self._postverify_material_trigger(
            "UPDATE material_pouches SET quantity=quantity+1;"
        )

    def test_postverify_gather_event_cannot_be_rewritten(self):
        self._postverify_material_trigger(
            "UPDATE events SET payload_json='{}' WHERE kind='gather';"
        )

    def test_postverify_extra_gather_event_cannot_be_inserted(self):
        self._postverify_material_trigger(
            """INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
               VALUES('2026-11-07T21:00:00+00:00','Ada','atrium','gather',
                      '{"material":"gold","amount":1}');"""
        )

    def test_postverify_foreign_room_gather_cannot_be_inserted(self):
        self._postverify_material_trigger(
            """INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
               VALUES('2026-11-07T21:00:00+00:00','Cora','workshop','gather',
                      '{"material":"gold","amount":1}');"""
        )

    def test_postverify_ungranted_material_pouch_cannot_be_inserted(self):
        self._postverify_material_trigger(
            "INSERT INTO material_pouches(actor,material,quantity) "
            "VALUES('Cora','gold',99);"
        )

    def test_postverify_ungranted_gather_node_cannot_be_inserted(self):
        self._postverify_material_trigger(
            "INSERT INTO gathering_nodes(room_id,material,remaining,batch_yield) "
            "VALUES('atrium','gold',99,1);"
        )

    def test_postverify_unrelated_say_event_is_permitted(self):
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("""
                CREATE TRIGGER main.benign_say_after_clock_ack
                AFTER UPDATE OF processed_utc ON clock_events
                WHEN NEW.event_id='roster_test_1'
                BEGIN
                    INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
                    VALUES('2026-11-07T21:00:00+00:00','Cora','atrium','say',
                           '{"text":"hi"}');
                END
            """)
        self.assertEqual(self.run_grant(policy, mac).executed, 2)
        self.assertEqual(self.counts(), (2, 2, 0))
        with closing(sqlite3.connect(self.dbpath)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'"
            ).fetchone()[0], 1)

    def _postverify_full_ledger_trigger(self, body, *, old_gather=False,
                                        high_id=False):
        """Installed MAIN trigger must not turn a false ACK into a commit."""
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            if old_gather:
                db.execute(
                    "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    ("2026-11-07T20:00:00+00:00", "Ada", "atrium", "gather",
                     '{"material":"iron","amount":1}'),
                )
            if high_id:
                db.execute(
                    "INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json) "
                    "VALUES(100,'2026-11-07T20:00:00+00:00','Ada','atrium','say','{}')"
                )
            before = db.execute("SELECT * FROM events ORDER BY id").fetchall()
            db.execute(
                "CREATE TRIGGER main.ledger_attack AFTER UPDATE OF processed_utc "
                "ON clock_events WHEN NEW.event_id='roster_test_1' "
                "BEGIN " + body + " END"
            )
        plan = self.proposal()
        policy, mac = self.grant(plan)
        with self.assertRaises(ValueError):
            self.run_grant(policy, mac)
        self.assertEqual(self.counts(), (1 if old_gather else 0, 0, 6))
        self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)
        with closing(sqlite3.connect(self.dbpath)) as db:
            self.assertEqual(db.execute(
                "SELECT * FROM events ORDER BY id"
            ).fetchall(), before)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("DROP TRIGGER main.ledger_attack")
        self.assertEqual(self.run_grant(policy, mac).executed, 2)
        self.assertEqual(self.counts(), (3 if old_gather else 2, 2, 0))
        self.assertEqual(self.clock.tick(
            NOW, lambda db, event: self.fail("roster replayed")
        ), [])

    def test_full_ledger_denies_historical_gather_payload_rewrite(self):
        self._postverify_full_ledger_trigger(
            "UPDATE events SET payload_json='{}' WHERE kind='gather' "
            "AND id=(SELECT MIN(id) FROM events WHERE kind='gather');",
            old_gather=True,
        )

    def test_full_ledger_denies_historical_gather_delete(self):
        self._postverify_full_ledger_trigger(
            "DELETE FROM events WHERE id=(SELECT MIN(id) FROM events WHERE kind='gather');",
            old_gather=True,
        )

    def test_full_ledger_denies_historical_gather_kind_demotion(self):
        self._postverify_full_ledger_trigger(
            "UPDATE events SET kind='say' WHERE id=(SELECT MIN(id) FROM events WHERE kind='gather');",
            old_gather=True,
        )

    def test_full_ledger_denies_join_kind_promotion(self):
        self._postverify_full_ledger_trigger(
            "UPDATE events SET kind='gather' WHERE id=(SELECT MIN(id) FROM events WHERE kind='join');"
        )

    def test_full_ledger_denies_negative_id_gather_forgery(self):
        self._postverify_full_ledger_trigger(
            "INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json) "
            "VALUES(-11,'2026-11-07T21:00:00+00:00','Cora','workshop','gather',"
            "'{\"material\":\"gold\",\"amount\":99}');"
        )

    def test_full_ledger_denies_zero_id_gather_forgery(self):
        self._postverify_full_ledger_trigger(
            "INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json) "
            "VALUES(0,'2026-11-07T21:00:00+00:00','Cora','workshop','gather',"
            "'{\"material\":\"gold\",\"amount\":99}');"
        )

    def test_full_ledger_denies_positive_id_below_watermark(self):
        self._postverify_full_ledger_trigger(
            "INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json) "
            "VALUES(50,'2026-11-07T21:00:00+00:00','Cora','workshop','gather',"
            "'{\"material\":\"gold\",\"amount\":99}');",
            old_gather=True, high_id=True,
        )

    def test_full_ledger_denies_historical_timestamp_rewrite(self):
        self._postverify_full_ledger_trigger(
            "UPDATE events SET occurred_at='2099-01-01T00:00:00+00:00' "
            "WHERE id=(SELECT MIN(id) FROM events WHERE kind='gather');",
            old_gather=True,
        )

    def test_full_ledger_denies_signed_gather_timestamp_rewrite(self):
        self._postverify_full_ledger_trigger(
            "UPDATE events SET occurred_at='2099-01-01T00:00:00+00:00' "
            "WHERE kind='gather';"
        )

    def _postverify_other_batch_event(self, hostile_body, *, old_gather=False,
                                      before_roster=False, expect_rejection=True):
        from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
        if old_gather:
            with closing(sqlite3.connect(self.dbpath)) as db, db:
                db.execute(
                    "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                    "VALUES('2026-11-07T20:00:00+00:00','Ada','atrium','gather',"
                    "'{\"material\":\"iron\",\"amount\":1}')"
                )
        plan = self.proposal()
        policy, mac = self.grant(plan)
        other_id = "aaa_before_roster" if before_roster else "zzz_later_say"
        self.clock.schedule(other_id, due_at=NOW, kind="say", payload={"test": "other"})
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            before = db.execute("SELECT * FROM events ORDER BY id").fetchall()
            db.execute(
                "CREATE TRIGGER main.later_batch_attack AFTER UPDATE OF processed_utc "
                "ON clock_events WHEN NEW.event_id='" + other_id + "' "
                "BEGIN " + hostile_body + " END"
            )

        def tick():
            def handler(db, event):
                if event.kind == "say":
                    db.execute(
                        "INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                        "VALUES('2026-11-07T21:00:00+00:00','Cora','atrium','say','{}')"
                    )
                    return None
                return perform_host_roster_gather_in_transaction(
                    db, event, engine=self.engine, crafting=self.crafting,
                    policy=policy, host_key=HOST_KEY, grant_mac_hex=mac,
                    now_utc=NOW,
                )
            return self.clock.tick(NOW, handler)

        if expect_rejection:
            with self.assertRaises(ValueError):
                tick()
            self.assertIsNone(self.clock.lookup("roster_test_1").processed_at)
            self.assertIsNone(self.clock.lookup(other_id).processed_at)
            self.assertEqual(self.counts(), (1 if old_gather else 0, 0, 6))
            with closing(sqlite3.connect(self.dbpath)) as db:
                self.assertEqual(db.execute(
                    "SELECT * FROM events ORDER BY id"
                ).fetchall(), before)
        else:
            self.assertEqual(len(tick()), 2)
            self.assertEqual(self.counts(), (2, 2, 0))
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute("DROP TRIGGER main.later_batch_attack")
        if expect_rejection:
            self.assertEqual(len(tick()), 2)
            self.assertEqual(self.counts(), (3 if old_gather else 2, 2, 0))
        self.assertEqual(tick(), [])

    def test_full_ledger_denies_late_batch_historical_gather_delete(self):
        self._postverify_other_batch_event(
            "DELETE FROM events WHERE id=(SELECT MIN(id) FROM events WHERE kind='gather');",
            old_gather=True,
        )

    def test_full_ledger_denies_late_batch_negative_id_forgery(self):
        self._postverify_other_batch_event(
            "INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json) "
            "VALUES(-12,'2026-11-07T21:00:00+00:00','Cora','workshop','gather',"
            "'{\"material\":\"gold\",\"amount\":23}');"
        )

    def test_full_ledger_denies_early_batch_historical_gather_delete(self):
        self._postverify_other_batch_event(
            "DELETE FROM events WHERE id=(SELECT MIN(id) FROM events WHERE kind='gather');",
            old_gather=True, before_roster=True,
        )

    def test_full_ledger_denies_early_batch_negative_id_forgery(self):
        self._postverify_other_batch_event(
            "INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json) "
            "VALUES(-13,'2026-11-07T21:00:00+00:00','Cora','workshop','gather',"
            "'{\"material\":\"gold\",\"amount\":23}');",
            before_roster=True,
        )

    def test_full_ledger_denies_early_batch_ungranted_node(self):
        self._postverify_other_batch_event(
            "INSERT INTO gathering_nodes(room_id,material,remaining,batch_yield) "
            "VALUES('atrium','gold',99,1);",
            before_roster=True,
        )

    def test_full_ledger_denies_early_batch_ungranted_pouch(self):
        self._postverify_other_batch_event(
            "INSERT INTO material_pouches(actor,material,quantity) "
            "VALUES('Cora','gold',99);",
            before_roster=True,
        )

    def test_full_ledger_allows_early_batch_benign_say(self):
        self._postverify_other_batch_event(
            "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
            "VALUES('2026-11-07T21:00:00+00:00','Cora','atrium','say','{}');",
            before_roster=True, expect_rejection=False,
        )


if __name__ == "__main__":
    unittest.main()
