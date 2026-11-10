"""Read-only local host readiness must not create or modify a world."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.host_preflight import preflight_local_host


class LocalHostPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "world.sqlite3"

    def test_missing_db_is_warning_not_a_write(self):
        before = tuple(self.root.iterdir())
        report = preflight_local_host(self.db)
        self.assertTrue(report.ready, report.findings)
        self.assertIn("db_missing", {finding.code for finding in report.findings})
        self.assertEqual(before, tuple(self.root.iterdir()))

    def test_existing_world_read_does_not_change_bytes_or_mtime(self):
        Engine(self.db)
        before = [(path.name, path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.iterdir()]
        report = preflight_local_host(self.db)
        self.assertTrue(report.ready, report.findings)
        self.assertIn("world_unbound", {finding.code for finding in report.findings})
        self.assertEqual(before, [(path.name, path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.root.iterdir()])

    def test_nonloopback_is_fatal_without_db_creation(self):
        report = preflight_local_host(self.db, http_host="0.0.0.0")
        self.assertFalse(report.ready)
        self.assertIn("http_nonloopback", {finding.code for finding in report.findings})
        self.assertFalse(self.db.exists())

    def test_corrupt_database_rejected_without_repair(self):
        original = b"not a SQLite database"
        self.db.write_bytes(original)
        report = preflight_local_host(self.db)
        self.assertFalse(report.ready)
        self.assertIn("db_invalid", {finding.code for finding in report.findings})
        self.assertEqual(original, self.db.read_bytes())

    def test_wrong_schema_rejected(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE unrelated(id INTEGER)")
        report = preflight_local_host(self.db)
        self.assertFalse(report.ready)
        self.assertIn("db_schema", {finding.code for finding in report.findings})

    def test_world_identity_mismatch_is_not_rebound(self):
        Engine(self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO world_meta(key,value) VALUES('world_id','known_world')")
        before = self.db.read_bytes()
        report = preflight_local_host(self.db, expected_world_id="different_world")
        self.assertFalse(report.ready)
        self.assertIn("world_mismatch", {finding.code for finding in report.findings})
        self.assertEqual(before, self.db.read_bytes())

    def test_wal_sidecar_means_unverified_without_mutation(self):
        Engine(self.db)
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO world_meta(key,value) VALUES('world_id','wal_only')")
        writer.commit()
        self.assertTrue(Path(str(self.db) + "-wal").exists())
        report = preflight_local_host(self.db)
        self.assertFalse(report.ready)
        self.assertIn("db_wal_unverified", {finding.code for finding in report.findings})
        self.assertNotIn("world_unbound", {finding.code for finding in report.findings})

    def test_missing_parent_is_fatal_without_creation(self):
        target = self.root / "missing" / "world.sqlite3"
        report = preflight_local_host(target)
        self.assertFalse(report.ready)
        self.assertIn("db_parent_missing", {finding.code for finding in report.findings})
        self.assertFalse(target.parent.exists())

    def test_broken_symlink_is_not_classified_as_fresh_world(self):
        target = self.root / "broken.sqlite3"
        try:
            target.symlink_to("missing.sqlite3")
        except OSError:
            self.skipTest("Symbolic-link creation not available on this platform")
        report = preflight_local_host(target)
        self.assertFalse(report.ready)
        self.assertIn("db_symlink_broken", {finding.code for finding in report.findings})

    def test_cli_redacts_paths_and_returns_nonzero_on_failure(self):
        completed = subprocess.run(
            [sys.executable, "-m", "realm.host_preflight", "--db", str(self.db),
             "--http-host", "0.0.0.0", "--json"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(2, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ready"])
        self.assertNotIn(str(self.db), completed.stdout)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
