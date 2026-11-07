"""Journal selection must not silently switch a played world's SQLite mode."""

from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm import Engine


def journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        return db.execute("PRAGMA journal_mode").fetchone()[0].upper()


class SQLiteJournalPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "world.sqlite3"

    def test_new_world_defaults_to_wal(self):
        Engine(self.db)
        self.assertEqual("WAL", journal_mode(self.db))

    def test_opt_in_delete_mode_survives_reopen_and_actions(self):
        first = Engine(self.db, journal_mode="DELETE")
        self.assertEqual("DELETE", journal_mode(self.db))
        first.join("Patrick")
        reopened = Engine(self.db)
        self.assertIn("The Atrium", reopened.act("Patrick", "look"))
        self.assertEqual("DELETE", journal_mode(self.db))

    def test_existing_mode_cannot_be_silently_switched(self):
        Engine(self.db, journal_mode="DELETE")
        with self.assertRaisesRegex(ValueError, "migration"):
            Engine(self.db, journal_mode="WAL")
        self.assertEqual("DELETE", journal_mode(self.db))

    def test_existing_wal_cannot_be_silently_downgraded(self):
        Engine(self.db)
        with self.assertRaisesRegex(ValueError, "migration"):
            Engine(self.db, journal_mode="DELETE")
        self.assertEqual("WAL", journal_mode(self.db))

    def test_unrecognized_mode_rejected_without_creating_db(self):
        with self.assertRaises(ValueError):
            Engine(self.db, journal_mode="TRUNCATE")
        self.assertFalse(self.db.exists())

    def test_legacy_delete_file_retained(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE old_marker (value INTEGER NOT NULL)")
        self.assertEqual("DELETE", journal_mode(self.db))
        Engine(self.db)
        self.assertEqual("DELETE", journal_mode(self.db))

    def test_delete_mode_remains_serialized_under_two_writers(self):
        first = Engine(self.db, journal_mode="DELETE")
        first.join("Patrick")
        first.join("Vera")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda actor: Engine(self.db).act(actor, "take token"),
                ("Patrick", "Vera"),
            ))
        self.assertEqual(1, sum("You take" in response for response in results))
        self.assertEqual("DELETE", journal_mode(self.db))

    def test_simultaneous_first_world_open_keeps_one_mode(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: Engine(self.db).join("Patrick"), range(4)))
        self.assertEqual(4, len(results))
        self.assertEqual("WAL", journal_mode(self.db))

    def test_competing_new_world_modes_cannot_both_claim_success(self):
        # Each iteration has a distinct file. One constructor must win and the
        # other must reject; a returned Engine cannot claim the losing mode.
        for trial in range(60):
            path = self.db.parent / f"contested-{trial}.sqlite3"
            barrier = Barrier(2)

            def attempt(mode: str) -> tuple[str, bool]:
                barrier.wait(timeout=5)
                try:
                    Engine(path, journal_mode=mode)
                except ValueError as exc:
                    if "migration" not in str(exc):
                        raise
                    return mode, False
                return mode, True

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, ("WAL", "DELETE")))
            winners = [mode for mode, succeeded in results if succeeded]
            self.assertEqual(1, len(winners), msg=f"Trial {trial}: {results}")
            self.assertEqual(winners[0], journal_mode(path))

    def test_competing_modes_are_fenced_across_processes(self):
        script = (
            "import sys,time,pathlib\n"
            "from realm import Engine\n"
            "path,mode,ready,other = sys.argv[1:]\n"
            "pathlib.Path(ready).touch()\n"
            "deadline = time.monotonic()+5\n"
            "while not pathlib.Path(other).exists() and time.monotonic()<deadline:\n"
            "    time.sleep(.005)\n"
            "try:\n"
            "    Engine(path,journal_mode=mode)\n"
            "except ValueError as exc:\n"
            "    if 'migration' not in str(exc): raise\n"
            "    sys.exit(5)\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        for trial in range(3):
            path = self.db.parent / f"process-contested-{trial}.sqlite3"
            a_ready = self.db.parent / f"a-{trial}.ready"
            b_ready = self.db.parent / f"b-{trial}.ready"
            a = subprocess.Popen(
                [sys.executable, "-c", script, str(path), "WAL", str(a_ready), str(b_ready)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            b = subprocess.Popen(
                [sys.executable, "-c", script, str(path), "DELETE", str(b_ready), str(a_ready)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            a_out, a_err = a.communicate(timeout=10)
            b_out, b_err = b.communicate(timeout=10)
            self.assertEqual([0, 5], sorted([a.returncode, b.returncode]),
                             msg=f"Trial {trial}: {a.returncode} {a_err} {b.returncode} {b_err}")
            winner = "WAL" if a.returncode == 0 else "DELETE"
            self.assertEqual(winner, journal_mode(path))


if __name__ == "__main__":
    unittest.main()
