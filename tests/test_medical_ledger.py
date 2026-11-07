"""Opt-in medical ledger regression: no live treatment or character death."""
import concurrent.futures
import sqlite3
from contextlib import closing
from dataclasses import replace
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from realm.mortality import HealthState, MortalityPolicy, injure
from realm.medical_ledger import MedicalLedger

UTC = timezone.utc
T0 = datetime(2026, 1, 2, tzinfo=UTC)
POLICY = MortalityPolicy("hardcore", "test-policy", {("trauma", "critical"): 3600})


class MedicalLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "med.sqlite"
        self.ledger = MedicalLedger(self.path, world_id="world-a", policy=POLICY)
        self.injury = injure(
            world_id="world-a", character_id="patient", cause="trauma",
            severity="critical", occurred_at=T0, policy=POLICY)
        self.ledger.register_injury("inj-1", self.injury)
        self.ledger.stock("kit-1", 1)

    def treat(self, **changes):
        args = dict(world_id="world-a", injury_id="inj-1",
                    actor_id="medic-a", resource_id="kit-1", action_id="heal-1",
                    expected_version=0, at=T0 + timedelta(minutes=2),
                    authorized=True, nearby=True)
        args.update(changes)
        return self.ledger.treat(**args)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            injured = db.execute(
                "SELECT state,version FROM medical_injuries WHERE injury_id='inj-1'"
            ).fetchone()
            kit = db.execute(
                "SELECT quantity FROM medical_supply WHERE resource_id='kit-1'"
            ).fetchone()
            receipts = db.execute("SELECT COUNT(*) FROM medical_receipts").fetchone()[0]
            return injured, kit[0], receipts

    def test_treatment_commits_stabilization_inventory_and_receipt(self):
        receipt = self.treat()
        self.assertEqual((receipt.state, receipt.version),
                         (HealthState.STABILIZED, 1))
        self.assertEqual(self.snapshot(), (("stabilized", 1), 0, 1))

    def test_two_medics_cannot_spend_one_kit_twice(self):
        barrier = threading.Barrier(2)
        def race(who):
            barrier.wait()
            try:
                return self.treat(actor_id=who, action_id="rescue-" + who)
            except ValueError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(race, ("medic-a", "medic-b")))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.snapshot(), (("stabilized", 1), 0, 1))

    def test_two_distinct_patients_compete_atomically_for_one_kit(self):
        other_injury = injure(
            world_id="world-a", character_id="patient-b", cause="trauma",
            severity="critical", occurred_at=T0, policy=POLICY)
        self.ledger.register_injury("inj-2", other_injury)
        barrier = threading.Barrier(2)

        def race(args):
            injury_id, actor_id = args
            barrier.wait()
            try:
                return self.treat(injury_id=injury_id, actor_id=actor_id,
                                  action_id="rescue-" + actor_id)
            except ValueError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(race, (
                ("inj-1", "medic-a"), ("inj-2", "medic-b"))))
        self.assertEqual(sum(result is not None for result in results), 1)
        with closing(sqlite3.connect(self.path)) as db:
            states = [row[0] for row in db.execute(
                "SELECT state FROM medical_injuries ORDER BY injury_id")]
        self.assertCountEqual(states, ["critical", "stabilized"])
        self.assertEqual(self.snapshot()[1:], (0, 1))

    def test_identical_replay_is_stable_after_kit_consumption(self):
        first = self.treat()
        replay = self.treat(nearby=False)
        self.assertEqual(replay, first)
        self.assertEqual(self.snapshot(), (("stabilized", 1), 0, 1))

    def test_changed_action_id_payload_rejected_without_effect(self):
        self.treat()
        with self.assertRaisesRegex(ValueError, "collision|conflict|reuse"):
            self.treat(actor_id="other", nearby=False)
        self.assertEqual(self.snapshot(), (("stabilized", 1), 0, 1))

    def test_replay_does_not_bypass_authentication(self):
        self.treat()
        with self.assertRaisesRegex(ValueError, "authoriz|authentic"):
            self.treat(authorized=False)
        self.assertEqual(self.snapshot(), (("stabilized", 1), 0, 1))

    def test_spoofed_injury_deadline_does_not_extend_rescue_window(self):
        forged = replace(self.injury, deadline_utc=T0 + timedelta(days=10))
        with self.assertRaises(ValueError):
            self.ledger.register_injury("forged-inj", forged)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM medical_injuries WHERE injury_id='forged-inj'"
            ).fetchone()[0], 0)
        self.assertEqual(self.snapshot(), (("critical", 0), 1, 0))

    def test_view_only_foreign_database_is_never_appropriated(self):
        foreign = Path(self.temp.name) / "view.sqlite"
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("CREATE VIEW private_authority AS SELECT 1 AS v")
        with self.assertRaises(ValueError):
            MedicalLedger(foreign, world_id="world-a", policy=POLICY)
        with closing(sqlite3.connect(foreign)) as db:
            objects = [(row[0], row[1]) for row in db.execute(
                "SELECT name,type FROM sqlite_master ORDER BY name")]
        self.assertEqual(objects, [("private_authority", "view")])

    def test_existing_foreign_database_is_never_appropriated(self):
        foreign = Path(self.temp.name) / "foreign.sqlite"
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("CREATE TABLE foreign_world_identity(world_id TEXT)")
            db.execute("INSERT INTO foreign_world_identity VALUES ('not-ours')")
        with self.assertRaises(ValueError):
            MedicalLedger(foreign, world_id="world-a", policy=POLICY)
        with closing(sqlite3.connect(foreign)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"foreign_world_identity"})
            self.assertEqual(db.execute(
                "SELECT world_id FROM foreign_world_identity").fetchone()[0],
                             "not-ours")

    def test_incorrect_world_and_policy_never_mutate(self):
        with self.assertRaises(ValueError):
            self.treat(world_id="world-b")
        with self.assertRaises(ValueError):
            MedicalLedger(self.path, world_id="world-b", policy=POLICY)
        other = MortalityPolicy("hardcore", "test-policy",
                                {("trauma", "critical"): 300})
        with self.assertRaises(ValueError):
            MedicalLedger(self.path, world_id="world-a", policy=other)
        self.assertEqual(self.snapshot(), (("critical", 0), 1, 0))

    def test_stale_version_and_after_deadline_rejected(self):
        with self.assertRaises(ValueError):
            self.treat(expected_version=8)
        with self.assertRaises(ValueError):
            self.treat(at=T0 + timedelta(hours=1))
        self.assertEqual(self.snapshot(), (("critical", 0), 1, 0))

    def test_unwitnessed_auth_and_proximity_rejected(self):
        with self.assertRaises(ValueError):
            self.treat(authorized=False)
        with self.assertRaises(ValueError):
            self.treat(nearby=False)
        self.assertEqual(self.snapshot(), (("critical", 0), 1, 0))

    def test_receipt_insert_failure_rolls_back_state_and_inventory(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER test_fail_receipt BEFORE INSERT ON
                          medical_receipts BEGIN SELECT RAISE(ABORT, 'forced'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.treat()
        self.assertEqual(self.snapshot(), (("critical", 0), 1, 0))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER test_fail_receipt")
        self.assertEqual(self.treat().state, HealthState.STABILIZED)

    def test_in_memory_ledger_cannot_masquerade_as_durable_sidecar(self):
        with self.assertRaises(ValueError):
            MedicalLedger(":memory:", world_id="world-a", policy=POLICY)

    def test_utc_and_identity_validation(self):
        with self.assertRaises(ValueError):
            self.treat(at=datetime(2026, 1, 2, 0, 2))
        with self.assertRaises(ValueError):
            self.treat(action_id="")
        with self.assertRaises(ValueError):
            self.treat(expected_version=True)
        self.assertEqual(self.snapshot(), (("critical", 0), 1, 0))


if __name__ == "__main__":
    unittest.main()
