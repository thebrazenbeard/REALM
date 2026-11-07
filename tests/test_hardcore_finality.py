"""Hostile source-only Hardcore join/action fence, not live death authority."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from realm.hardcore_finality import Admission, HardcoreFinalityGate, WitnessStatement
from realm.mortality import HealthState, MortalityPolicy

POLICY = MortalityPolicy("hardcore", "test-fatality", {("trauma", "critical"): 3600})


class FakeExternalWitness:
    """Independent test fixture; NOT a production witness, signer, or trust root."""

    def __init__(self):
        self.retired = set()
        self.issued = set()
        self.previous = None
        self.replay = False
        self.fail = False
        self.invalid = False
        self.serial = 0

    def attest(self, world_id, character_id, nonce):
        if self.fail:
            raise ConnectionError("witness offline")
        if self.replay:
            return self.previous
        self.serial += 1
        statement = WitnessStatement(
            world_id=world_id, character_id=character_id, mode="hardcore",
            policy_hash=POLICY.policy_hash, nonce=nonce,
            verdict="retired" if (world_id, character_id) in self.retired else "clear",
            sequence=self.serial, proof="test-only-signed")
        self.issued.add(statement)
        self.previous = statement
        return statement

    def verify(self, statement):
        if self.fail:
            raise ConnectionError("witness verifier offline")
        return not self.invalid and statement in self.issued


class HardcoreFinalityTests(unittest.TestCase):
    def setUp(self):
        self.witness = FakeExternalWitness()
        self.gate = HardcoreFinalityGate(
            world_id="world-a", policy=POLICY, witness=self.witness)

    def test_fresh_external_clear_allows_only_live_and_stabilized(self):
        self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision, Admission.ALLOW)
        self.assertEqual(self.gate.check("player", HealthState.STABILIZED).decision, Admission.ALLOW)

    def test_restored_predeath_local_snapshot_cannot_revive_external_retired(self):
        self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision, Admission.ALLOW)
        self.witness.retired.add(("world-a", "player"))
        self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision, Admission.RETIRED)
        self.assertEqual(self.gate.check("player", HealthState.STABILIZED).decision, Admission.RETIRED)

    def test_witness_outage_and_unverifiable_attestation_hold_admission(self):
        self.witness.fail = True
        self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision, Admission.HOLD)
        self.witness.fail = False
        self.witness.invalid = True
        self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision, Admission.HOLD)

    def test_signed_previous_clear_cannot_be_replayed_after_retirement(self):
        self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision, Admission.ALLOW)
        self.witness.retired.add(("world-a", "player"))
        self.witness.replay = True
        self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision, Admission.HOLD)

    def test_candidate_never_becomes_final_without_external_retirement(self):
        self.assertEqual(
            self.gate.check("player", HealthState.DEATH_CANDIDATE).decision, Admission.HOLD)
        self.assertEqual(
            self.gate.check("player", HealthState.PENDING_REVIEW).decision, Admission.HOLD)
        self.assertEqual(
            self.gate.check("player", HealthState.CRITICAL).decision, Admission.HOLD)
        self.assertEqual(
            self.gate.check("player", HealthState.RESPAWN_PENDING).decision, Admission.HOLD)

    def test_local_final_is_never_reopened_by_clear_witness(self):
        self.assertEqual(self.gate.check("player", HealthState.DEAD_FINAL).decision,
                         Admission.RETIRED)

    def test_world_mode_policy_character_and_nonce_mismatch_fail_closed(self):
        original = self.witness.attest
        mutations = (
            {"world_id": "world-b"}, {"character_id": "other"},
            {"mode": "casual"}, {"policy_hash": "0" * 64},
            {"nonce": "stale"}, {"verdict": "allowed"},
            {"sequence": -1}, {"proof": ""},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                def malformed(world, character, nonce, *, changes=changes):
                    return replace(original(world, character, nonce), **changes)
                self.witness.attest = malformed
                self.assertEqual(
                    self.gate.check("player", HealthState.ALIVE).decision, Admission.HOLD)
                self.witness.attest = original

    def test_disposable_world_db_backup_restore_cannot_bypass_external_retired(self):
        # Synthetic SQLite character table only; REALM routes are NOT wired.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "world.sqlite"
            backup_path = Path(tmp) / "before.sqlite"
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE characters(id TEXT PRIMARY KEY, state TEXT)")
                db.execute("INSERT INTO characters VALUES ('player', 'alive')")
            shutil.copyfile(db_path, backup_path)
            self.witness.retired.add(("world-a", "player"))
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("UPDATE characters SET state='dead_final' WHERE id='player'")
            shutil.copyfile(backup_path, db_path)
            with closing(sqlite3.connect(db_path)) as db, db:
                restored = HealthState(db.execute(
                    "SELECT state FROM characters WHERE id='player'").fetchone()[0])
            self.assertIs(restored, HealthState.ALIVE)
            self.assertEqual(self.gate.check("player", restored).decision,
                             Admission.RETIRED)

    def test_invalid_witness_response_object_never_means_clear(self):
        original = self.witness.attest
        try:
            self.witness.attest = lambda world, character, nonce: None
            self.assertEqual(self.gate.check("player", HealthState.ALIVE).decision,
                             Admission.HOLD)
        finally:
            self.witness.attest = original

    def test_hardcore_only_and_invalid_identity_fail_closed(self):
        casual = MortalityPolicy("casual", "test", {("trauma", "critical"): 3600})
        with self.assertRaises(ValueError):
            HardcoreFinalityGate(world_id="world-a", policy=casual,
                                witness=self.witness)
        with self.assertRaises(ValueError):
            self.gate.check("", HealthState.ALIVE)
        with self.assertRaises(ValueError):
            self.gate.check("player", "alive")

    def test_no_witness_is_not_equivalent_to_no_death_record(self):
        with self.assertRaises(ValueError):
            HardcoreFinalityGate(world_id="world-a", policy=POLICY, witness=None)


if __name__ == "__main__":
    unittest.main()
