"""Synthetic, proposal-only atmosphere-to-injury boundary tests."""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.engine import Engine
from realm.atmosphere import AtmosphereModel
from realm.atmosphere_ledger import AtmosphereLedger
from realm.mortality import MortalityPolicy, HealthState
from realm.exposure_injury import ExposureProfile, evaluate_exposure_in_transaction


class ExposureInjuryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "world.sqlite3"
        Engine(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO world_meta(key,value) VALUES('world_id','worldOne')")
        model = AtmosphereModel(
            compartments={
                "bridge": {"volume_l": 1000, "pressure_pa": 100000,
                           "oxygen_ppm": 210000, "temperature_mk": 293150},
                "hold": {"volume_l": 1000, "pressure_pa": 20000,
                         "oxygen_ppm": 100000, "temperature_mk": 293150}},
            hatches={"door": {"a": "bridge", "b": "hold",
                              "operators": ["pilot"], "open": False,
                              "operational": True}},
            occupants={"crew": {"compartment": "hold", "sealed_suit": False},
                       "suited": {"compartment": "hold", "sealed_suit": True}})
        self.ledger = AtmosphereLedger(self.path)
        self.ledger.enable(world_id="worldOne", model=model)
        self.now = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
        self.profile = ExposureProfile(
            profile_id="synthetic_only",
            min_pressure_pa=50000, min_oxygen_pa=15000,
            pressure_injury=("pressure_exposure", "critical"),
            oxygen_injury=("oxygen_exposure", "critical"))
        self.profiles = {"crew": self.profile, "suited": self.profile}
        self.policy = MortalityPolicy(
            mode="hardcore", policy_id="synthetic_mortality",
            windows_seconds={("pressure_exposure", "critical"): 3600,
                             ("oxygen_exposure", "critical"): 300})

    def evaluate(self, db, *, version=0, profiles=None, world_id="worldOne", event_id="exposureA"):
        return evaluate_exposure_in_transaction(
            self.ledger, db, world_id=world_id, event_id=event_id,
            at_utc=self.now, expected_version=version,
            profiles=self.profiles if profiles is None else profiles,
            mortality_policy=self.policy)

    def test_wrong_world_unbound_and_missing_profile_fail_closed(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "binding mismatch"):
                self.evaluate(db, world_id="otherWorld")
            with self.assertRaisesRegex(ValueError, "Every occupant"):
                self.evaluate(db, profiles={"crew": self.profile})
            db.rollback()
        with closing(sqlite3.connect(Path(self.temp.name) / "other.sqlite3")) as alien:
            alien.row_factory = sqlite3.Row
            alien.execute("PRAGMA foreign_keys=ON")
            alien.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "bound world DB"):
                self.evaluate(alien)
            alien.rollback()

    def test_stale_version_and_host_transaction_enforced(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with self.assertRaisesRegex(RuntimeError, "host transaction"):
                self.evaluate(db)
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "Stale atmosphere snapshot"):
                self.evaluate(db, version=1)
            db.rollback()

    def test_temp_shadow_is_ignored_replay_and_restart_are_deterministic(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TEMP TABLE atm_state(id INTEGER, world_id TEXT, version INTEGER, state_json TEXT)")
            db.execute("INSERT INTO temp.atm_state VALUES(1,'evil',999,'{}')")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            first = self.evaluate(db)
            second = self.evaluate(db)
            restarted = evaluate_exposure_in_transaction(
                AtmosphereLedger(self.path), db, world_id="worldOne",
                event_id="exposureA", at_utc=self.now,
                expected_version=0, profiles=self.profiles, mortality_policy=self.policy)
            self.assertEqual(first, second)
            self.assertEqual(first, restarted)
            self.assertEqual(64, len(first.source_state_sha256))
            self.assertEqual(self.policy.policy_hash, first.mortality_policy_hash)
            self.assertTrue(all(i.state is not HealthState.DEAD_FINAL for i in first.proposals))
            db.rollback()

    def test_casual_hardcore_zero_window_has_no_final_death(self):
        windows = {("pressure_exposure", "critical"): 0,
                   ("oxygen_exposure", "critical"): 0}
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            states = []
            for mode in ("casual", "hardcore"):
                self.policy = MortalityPolicy(mode=mode, policy_id="synthetic_zero",
                                               windows_seconds=windows)
                result = self.evaluate(db)
                states.append({i.state for i in result.proposals})
                self.assertFalse(result.authorized_for_injury_write)
            self.assertEqual([{HealthState.RESPAWN_PENDING},
                              {HealthState.DEATH_CANDIDATE}], states)
            db.rollback()

    def test_breach_tick_staged_in_same_transaction_changes_exposure_gradually(self):
        mild = ExposureProfile(
            "mild_synthetic", 10000, 4000,
            ("pressure_exposure", "critical"), ("oxygen_exposure", "critical"))
        profiles = {"crew": mild, "suited": mild}
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            initial = self.evaluate(db, profiles=profiles)
            self.assertEqual(["oxygen_exposure"], [p.cause for p in initial.proposals])
            registered = self.ledger.apply_in_transaction(
                db, world_id="worldOne", action_id="breachA",
                operation="breach", params={"chamber_id": "hold",
                                            "loss_pa_l_per_tick": 12000000},
                expected_version=0)
            self.assertEqual(1, registered["version"])
            after_breach = self.evaluate(db, version=1, profiles=profiles)
            self.assertEqual(initial.proposals, after_breach.proposals)
            tick = self.ledger.apply_in_transaction(
                db, world_id="worldOne", action_id="tickA", operation="tick",
                params={}, expected_version=1)
            self.assertEqual(2, tick["version"])
            final = self.evaluate(db, version=2, profiles=profiles)
            self.assertEqual({"pressure_exposure", "oxygen_exposure"},
                             {p.cause for p in final.proposals})
            self.assertNotEqual(initial.source_state_sha256, final.source_state_sha256)
            self.assertFalse(final.authorized_for_injury_write)
            db.rollback()
        self.assertEqual(0, self.ledger.status(world_id="worldOne")["version"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM atm_receipts").fetchone()[0])

    def test_exposure_profile_hash_tracks_rule_changes_and_rejects_bad_inputs(self):
        changed = ExposureProfile(
            "synthetic_only", 60000, 15000,
            ("pressure_exposure", "critical"), ("oxygen_exposure", "critical"))
        self.assertNotEqual(self.profile.profile_hash, changed.profile_hash)
        with self.assertRaises(ValueError):
            ExposureProfile("bad profile", 50000, 15000,
                            ("pressure_exposure", "critical"), ("oxygen_exposure", "critical"))
        with self.assertRaises(ValueError):
            ExposureProfile("bad_pressure", True, 15000,
                            ("pressure_exposure", "critical"), ("oxygen_exposure", "critical"))
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            baseline = self.evaluate(db)
            altered = self.evaluate(db, profiles={"crew": changed, "suited": self.profile})
            self.assertNotEqual(baseline.exposure_policy_hash, altered.exposure_policy_hash)
            db.rollback()

    def test_utc_event_validation_policy_holes_and_no_hidden_receipts(self):
        from datetime import timedelta
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for invalid_time in (datetime(2026, 11, 7, 20, 0),
                                 datetime(2026, 11, 7, 20, 0,
                                          tzinfo=timezone(timedelta(hours=1)))):
                with self.assertRaisesRegex(ValueError, "UTC-aware"):
                    evaluate_exposure_in_transaction(
                        self.ledger, db, world_id="worldOne",
                        event_id="eventX", at_utc=invalid_time,
                        expected_version=0, profiles=self.profiles,
                        mortality_policy=self.policy)
            with self.assertRaisesRegex(ValueError, "Invalid exposure event ID"):
                self.evaluate(db, event_id="bad event")
            sparse_policy = MortalityPolicy(
                mode="hardcore", policy_id="missing_pressure",
                windows_seconds={("oxygen_exposure", "critical"): 100})
            with self.assertRaisesRegex(ValueError, "unsupported injury profile"):
                evaluate_exposure_in_transaction(
                    self.ledger, db, world_id="worldOne", event_id="eventX",
                    at_utc=self.now, expected_version=0,
                    profiles=self.profiles, mortality_policy=sparse_policy)
            self.evaluate(db)
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM main.atm_receipts").fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT version FROM main.atm_state WHERE id=1").fetchone()[0])
            db.rollback()

    def test_unprotected_two_proposals_suited_zero_no_mutation(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            result = self.evaluate(db)
            self.assertEqual("HOST_REVIEW_REQUIRED", result.status)
            self.assertEqual(0, result.atmosphere_version)
            self.assertEqual({"pressure_exposure", "oxygen_exposure"},
                             {p.cause for p in result.proposals})
            self.assertEqual({"crew"}, {p.character_id for p in result.proposals})
            self.assertTrue(all(p.state is HealthState.CRITICAL for p in result.proposals))
            self.assertFalse(result.authorized_for_injury_write)
            db.rollback()
        self.assertEqual(0, self.ledger.status(world_id="worldOne")["version"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM atm_receipts").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
