"""Source-only medical/exposure/mode coherence tests; absolutely no live treatment."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.atmosphere import AtmosphereModel
from realm.atmosphere_ledger import AtmosphereLedger
from realm.engine import Engine
from realm.exposure_injury import ExposureProfile
from realm.medical_ledger import MedicalLedger
from realm.mortality import HealthState, MortalityPolicy
from realm.server_mode import bind_server_mode
from realm.medical_source_bridge import inspect_medical_source_in_transaction


class MedicalSourceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = Path(self.temp.name) / "world.db"
        self.medical = Path(self.temp.name) / "medical.db"
        Engine(self.world)
        with closing(sqlite3.connect(self.world)) as db, db:
            db.execute("INSERT INTO main.world_meta(key,value) VALUES('world_id','worldone')")
        self.policy = MortalityPolicy("hardcore", "synthetic-v1",
            {("pressure", "critical"): 3600, ("oxygen", "critical"): 300})
        bind_server_mode(self.world, world_id="worldone", policy=self.policy,
                         policy_version=1, configuration_id="synthetic-v1")
        self.atmosphere = AtmosphereLedger(self.world)
        self.atmosphere.enable(world_id="worldone", model=AtmosphereModel(
            compartments={"hold": {"volume_l": 1000, "pressure_pa": 15000,
                 "oxygen_ppm": 210000, "temperature_mk": 293150}},
            hatches={}, occupants={"patient": {"compartment": "hold",
                                               "sealed_suit": False}}))
        MedicalLedger(self.medical, world_id="worldone", policy=self.policy)
        self.at = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.profile = ExposureProfile("synthetic", 50000, 0,
                     ("pressure", "critical"), ("oxygen", "critical"))

    def inspect(self, db, **changes):
        args = dict(world_id="worldone", event_id="exposureA", at_utc=self.at,
                    expected_version=0, profiles={"patient": self.profile},
                    mortality_policy=self.policy, policy_version=1,
                    medical_db_path=self.medical)
        args.update(changes)
        return inspect_medical_source_in_transaction(self.atmosphere, db, **args)

    def test_held_review_preserves_utc_proposal_without_side_effects(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            review = self.inspect(db)
            self.assertEqual(review.status, "HELD_HOST_REVIEW_REQUIRED")
            self.assertFalse(review.authorized_for_injury_write)
            self.assertFalse(review.authorized_for_treatment)
            self.assertEqual(review.mode_binding.mode, "hardcore")
            self.assertEqual(len(review.exposure.proposals), 1)
            injury = review.exposure.proposals[0]
            self.assertEqual(injury.state, HealthState.CRITICAL)
            self.assertEqual(injury.deadline_utc - injury.incident_utc,
                             timedelta(hours=1))
            self.assertEqual(self.inspect(db), review)
            db.rollback()
        with closing(sqlite3.connect(self.medical)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM medical_injuries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM medical_receipts").fetchone()[0], 0)

    def test_wrong_policy_and_sidecar_do_not_mint_medical_authority(self):
        other = MortalityPolicy("hardcore", "synthetic-v1",
            {("pressure", "critical"): 1, ("oxygen", "critical"): 300})
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "policy"):
                self.inspect(db, mortality_policy=other)
            with self.assertRaisesRegex(ValueError, "medical"):
                self.inspect(db, medical_db_path=Path(self.temp.name) / "missing.db")
            with self.assertRaisesRegex(ValueError, "binding"):
                self.inspect(db, policy_version=2)
            db.rollback()
        self.assertFalse((Path(self.temp.name) / "missing.db").exists())

    def test_temp_mode_spoof_cannot_substitute_main_binding(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TEMP TABLE realm_server_mode(mode TEXT, policy_hash TEXT)")
            db.execute("INSERT INTO temp.realm_server_mode VALUES('casual','fake')")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            review = self.inspect(db)
            self.assertEqual(review.mode_binding.mode, "hardcore")
            self.assertEqual(review.exposure.proposals[0].policy_hash,
                             review.mode_binding.policy_hash)
            db.rollback()

    def test_same_named_foreign_injury_table_is_not_a_medical_ledger(self):
        with closing(sqlite3.connect(self.medical)) as db, db:
            db.execute("DROP TABLE medical_injuries")
            db.execute("CREATE TABLE medical_injuries (forged_only INTEGER NOT NULL)")
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "medical sidecar schema"):
                self.inspect(db)
            db.rollback()

    def test_same_columns_without_medical_supply_check_are_rejected(self):
        with closing(sqlite3.connect(self.medical)) as db, db:
            db.execute("DROP TABLE medical_supply")
            db.execute("CREATE TABLE medical_supply ("
                       "resource_id TEXT PRIMARY KEY, quantity INTEGER NOT NULL)")
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "medical sidecar schema"):
                self.inspect(db)
            db.rollback()

    def test_orphan_receipt_is_rejected_before_medical_source_review(self):
        with closing(sqlite3.connect(self.medical)) as db, db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO medical_receipts(action_id,fingerprint,injury_id,state,version) "
                       "VALUES('orphan','synthetic','noSuchInjury','done',1)")
            self.assertEqual(len(db.execute("PRAGMA main.foreign_key_check").fetchall()), 1)
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "referential integrity"):
                self.inspect(db)
            db.rollback()

    def test_wrong_medical_world_is_rejected_without_world_or_kit_effect(self):
        alien = Path(self.temp.name) / "alien.db"
        MedicalLedger(alien, world_id="another", policy=self.policy)
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "medical"):
                self.inspect(db, medical_db_path=alien)
            db.rollback()
        with closing(sqlite3.connect(alien)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM medical_receipts").fetchone()[0], 0)

    def test_stale_atmosphere_version_and_world_rollback(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            event = self.atmosphere.apply_in_transaction(
                db, world_id="worldone", action_id="breachA",
                operation="breach", params={"chamber_id": "hold",
                                           "loss_pa_l_per_tick": 1},
                expected_version=0)
            self.assertEqual(event["version"], 1)
            with self.assertRaisesRegex(ValueError, "Stale atmosphere"):
                self.inspect(db, expected_version=0)
            db.rollback()
        self.assertEqual(self.atmosphere.status(world_id="worldone")["version"], 0)
        with closing(sqlite3.connect(self.medical)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM medical_injuries").fetchone()[0], 0)

    def test_medical_sidecar_view_or_trigger_added_after_creation_is_held(self):
        with closing(sqlite3.connect(self.medical)) as db, db:
            db.execute("CREATE VIEW foreign_rights AS SELECT 1 AS approved")
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "medical sidecar schema"):
                self.inspect(db)
            db.rollback()

    def test_backward_utc_predating_mode_is_rejected(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "predates"):
                self.inspect(db, at_utc=self.at - timedelta(days=5))
            db.rollback()


if __name__ == "__main__":
    unittest.main()
