"""Synthetic world-mode binding tests; no live admission or deployment."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import hashlib
import shutil
import sqlite3
import tempfile
import unittest

from realm.engine import Engine
from realm.mortality import MortalityPolicy
from realm.server_mode import (
    ModeBindingError, bind_server_mode, read_server_mode, check_mode_admission,
)


def policy(mode: str, window: int = 3600) -> MortalityPolicy:
    return MortalityPolicy(
        mode=mode, policy_id="sandbox-v1",
        windows_seconds={("injury", "severe"): window},
    )


class ServerModeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "sandbox.sqlite"
        self.engine = Engine(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as db:
            with db:
                db.execute("INSERT INTO main.world_meta(key,value) VALUES('world_id',?)",
                           ("sandbox",))

    def bind(self, mode="casual", config_id="config-v1", policy_version=1,
             db_path=None, world_id="sandbox", window=3600):
        return bind_server_mode(
            self.db_path if db_path is None else db_path,
            world_id=world_id, policy=policy(mode, window),
            policy_version=policy_version, configuration_id=config_id,
        )

    def test_exact_binding_and_idempotent_replay(self):
        first = self.bind()
        self.assertEqual(first.world_id, "sandbox")
        self.assertEqual(first.mode, "casual")
        self.assertEqual(first.policy_hash, policy("casual").policy_hash)
        again = self.bind()
        self.assertEqual(first, again)
        self.assertEqual(read_server_mode(self.db_path, world_id="sandbox"), first)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM main.realm_server_mode").fetchone()[0], 1)

    def test_mode_policy_and_config_cannot_be_changed(self):
        self.bind()
        for kwargs in (
            {"mode": "hardcore"},
            {"mode": "casual", "window": 7},
            {"mode": "casual", "config_id": "new-config"},
            {"mode": "casual", "policy_version": 2},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ModeBindingError):
                self.bind(**kwargs)
        self.assertEqual(read_server_mode(self.db_path, world_id="sandbox").mode, "casual")

    def test_existing_character_cannot_be_silently_migrated(self):
        self.engine.join("Alice")
        with self.assertRaisesRegex(ModeBindingError, "played"):
            self.bind()
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertFalse(db.execute(
                "SELECT 1 FROM main.sqlite_master WHERE name='realm_server_mode'"
            ).fetchone())

    def test_wrong_world_and_unbound_destination_are_rejected(self):
        with self.assertRaisesRegex(ModeBindingError, "world"):
            self.bind(world_id="other")
        with self.assertRaises(ModeBindingError):
            read_server_mode(self.db_path, world_id="sandbox")
        self.bind()
        with self.assertRaisesRegex(ModeBindingError, "world"):
            read_server_mode(self.db_path, world_id="other")

    def test_two_mode_race_has_exactly_one_winner(self):
        def attempt(mode):
            try:
                self.bind(mode=mode, config_id="race-" + mode)
                return "bound"
            except ModeBindingError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("casual", "hardcore")))
        self.assertEqual(sorted(results), ["blocked", "bound"])
        binding = read_server_mode(self.db_path, world_id="sandbox")
        self.assertIn(binding.mode, ("casual", "hardcore"))

    def test_foreign_schema_or_trigger_fails_closed(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            with db:
                db.execute("CREATE VIEW main.realm_server_mode AS SELECT 1 AS id")
        with self.assertRaisesRegex(ModeBindingError, "schema"):
            self.bind()
        with closing(sqlite3.connect(self.db_path)) as db:
            with db:
                db.execute("DROP VIEW main.realm_server_mode")
        self.bind()
        with closing(sqlite3.connect(self.db_path)) as db:
            with db:
                db.execute("CREATE TRIGGER main.malicious AFTER UPDATE ON realm_server_mode BEGIN SELECT 1; END")
        with self.assertRaisesRegex(ModeBindingError, "schema"):
            read_server_mode(self.db_path, world_id="sandbox")

    def test_real_world_db_path_required_and_connection_not_accepted(self):
        with self.assertRaises(ModeBindingError):
            self.bind(db_path=":memory:")
        with self.assertRaises(ModeBindingError):
            self.bind(db_path=Path(self.temp.name) / "missing.sqlite")
        with closing(sqlite3.connect(self.db_path)) as db:
            with self.assertRaises(ModeBindingError):
                self.bind(db_path=db)

    def test_exact_policy_version_validation(self):
        for invalid in (0, True, -1, 2**63):
            with self.subTest(version=invalid), self.assertRaises(ModeBindingError):
                self.bind(policy_version=invalid)

    def test_casual_mode_requires_host_auth_for_play(self):
        self.bind()
        check = check_mode_admission(
            self.db_path, world_id="sandbox", character_id="Alice",
            policy=policy("casual"), policy_version=1,
        )
        self.assertEqual(check.status, "HOST_AUTH_REQUIRED")
        self.assertFalse(check.authorized_for_play)

    def test_hardcore_mode_requires_external_witness(self):
        self.bind(mode="hardcore")
        check = check_mode_admission(
            self.db_path, world_id="sandbox", character_id="Alice",
            policy=policy("hardcore"), policy_version=1,
        )
        self.assertEqual(check.status, "WITNESS_REQUIRED")
        self.assertFalse(check.authorized_for_play)
        with self.assertRaises(ModeBindingError):
            check_mode_admission(
                self.db_path, world_id="sandbox", character_id="Alice",
                policy=policy("casual"), policy_version=1,
            )

    def test_snapshot_restore_cannot_be_claimed_as_unbroken_authority(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup = Path(self.temp.name) / "before-binding.sqlite"
        shutil.copyfile(self.db_path, backup)
        self.bind(mode="hardcore")
        restored = Path(self.temp.name) / "restored.sqlite"
        shutil.copyfile(backup, restored)
        with self.assertRaises(ModeBindingError):
            read_server_mode(restored, world_id="sandbox")
        self.assertEqual(read_server_mode(self.db_path, world_id="sandbox").mode, "hardcore")

    def test_corrupted_stored_identity_or_digest_fails_closed(self):
        self.bind()
        with closing(sqlite3.connect(self.db_path)) as db:
            with db:
                db.execute("UPDATE main.realm_server_mode SET policy_hash=?", ("Z" * 64,))
        with self.assertRaises(ModeBindingError):
            read_server_mode(self.db_path, world_id="sandbox")

    def test_different_world_databases_are_independent(self):
        other = Path(self.temp.name) / "other.sqlite"
        Engine(other)
        with closing(sqlite3.connect(other)) as db:
            with db:
                db.execute("INSERT INTO main.world_meta(key,value) VALUES('world_id',?)",
                           ("other_world",))
        self.bind(mode="hardcore")
        self.bind(db_path=other, world_id="other_world")
        self.assertEqual(read_server_mode(other, world_id="other_world").mode, "casual")
        self.assertEqual(read_server_mode(self.db_path, world_id="sandbox").mode, "hardcore")


if __name__ == "__main__":
    unittest.main()
