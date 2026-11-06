"""Disposable process-concurrency and malformed-input regression probes.

These tests NEVER touch a running REALM save. Each subprocess opens only the
test's temporary SQLite database; no Lappy or external provider involvement.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from realm.engine import Engine
from realm.worldpack import WorldpackError, import_worldpack


CHILD = r'''
import json,sys
from realm.engine import Engine
from realm.worldpack import import_worldpack
db, mode, actor, *args = sys.argv[1:]
try:
    if mode == "import":
        result = import_worldpack(db, args[0]); value = result["unchanged"]
    else:
        engine = Engine(db)
        value = engine.join(actor) if mode == "join" else engine.act(actor,args[0],action_id=args[1])
    print(json.dumps({"ok": True,"value":value}))
except Exception as exc:
    print(json.dumps({"ok": False,"error":type(exc).__name__,"message":str(exc)}))
    sys.exit(2)
'''


class CoreProcessResilienceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.db_path = self.folder / "stress.sqlite3"

    def _spawn(self, jobs):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        children = [subprocess.Popen(
            [sys.executable, "-c", CHILD, str(self.db_path), *job],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(ROOT), env=env,
        ) for job in jobs]
        answers = []
        for process in children:
            try:
                output, error = process.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
                self.fail("A child core engine operation hung longer than 25s")
            self.assertEqual("", error, error)
            self.assertTrue(output.strip(), "Child returned no JSON receipt")
            answers.append(json.loads(output.strip()))
        return answers

    def _count(self, table, where=""):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute("SELECT COUNT(*) FROM " + table + " " + where).fetchone()[0]

    def test_concurrent_initialization_and_unique_joins_across_processes(self):
        results = self._spawn([("join", f"Actor{i}") for i in range(12)])
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual(12, self._count("players"))
        self.assertEqual(12, self._count("events", "WHERE kind='join'"))

    def test_same_name_concurrent_join_is_one_durable_principal(self):
        results = self._spawn([("join", "Single") for _ in range(12)])
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual(1, self._count("players"))
        self.assertEqual(1, self._count("events", "WHERE kind='join'"))

    def test_many_processes_compete_for_one_item_without_duplication(self):
        engine = Engine(self.db_path)
        for index in range(10):
            engine.join(f"Taker{index}")
        results = self._spawn([
            ("act", f"Taker{index}", "take token", f"compete_{index}")
            for index in range(10)
        ])
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual(1, sum(row["value"].startswith("You take") for row in results))
        self.assertEqual(1, self._count("events", "WHERE kind='take'"))
        self.assertEqual(10, self._count("action_receipts"))
        with closing(sqlite3.connect(self.db_path)) as db:
            holder = db.execute("SELECT room_id,held_by FROM items WHERE id='token'").fetchone()
        self.assertIsNone(holder[0])
        self.assertIn(holder[1], {f"Taker{i}" for i in range(10)})

    def test_replay_same_action_id_across_processes_creates_one_speech(self):
        Engine(self.db_path).join("Speaker")
        results = self._spawn([
            ("act", "Speaker", "say The bell was heard.", "one_replay")
            for _ in range(12)
        ])
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual({"You say: The bell was heard."}, {row["value"] for row in results})
        self.assertEqual(1, self._count("events", "WHERE kind='say'"))
        self.assertEqual(1, self._count("action_receipts"))

    def test_worldpack_import_race_produces_exactly_one_initial_import(self):
        pack = str(ROOT / "examples" / "shore-world.json")
        results = self._spawn([("import", "Unused", pack) for _ in range(8)])
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual(1, sum(row["value"] is False for row in results))
        self.assertEqual(7, sum(row["value"] is True for row in results))
        self.assertEqual(0, self._count("events"))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual("shore-example", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])

    def test_lock_contention_waits_and_commits_after_writer_releases(self):
        engine = Engine(self.db_path)
        engine.join("Patient")
        with closing(engine._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            # One child must block behind our writer lock, never report a
            # false successful action while the transaction is still held.
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            child = subprocess.Popen(
                [sys.executable, "-c", CHILD, str(self.db_path),
                 "act", "Patient", "say After the lock.", "wait_for_writer"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(ROOT), env=env,
            )
            import time
            time.sleep(0.5)
            self.assertIsNone(child.poll(), "Child unexpectedly bypassed writer lock")
            db.rollback()
            output, error = child.communicate(timeout=20)
        self.assertEqual("", error)
        self.assertEqual(0, child.returncode)
        self.assertTrue(json.loads(output)["ok"])
        self.assertEqual(1, self._count("events", "WHERE kind='say'"))

    def test_invalid_utf8_worldpack_fails_closed_before_database_creation(self):
        bad = self.folder / "broken.json"
        bad.write_bytes(b"{\"schema\":\"realm.worldpack.v1\",\"name\":\"\xff\"}")
        with self.assertRaisesRegex(WorldpackError, "UTF-8 JSON"):
            import_worldpack(self.db_path, bad)
        self.assertFalse(self.db_path.exists())

    def test_exception_after_item_update_rolls_back_item_event_and_receipt(self):
        engine = Engine(self.db_path)
        engine.join("Rollback")
        with patch.object(engine, "_event", side_effect=RuntimeError("injected event failure")):
            with self.assertRaisesRegex(RuntimeError, "injected event failure"):
                engine.act("Rollback", "take token", action_id="failed_take")
        with closing(sqlite3.connect(self.db_path)) as db:
            item = db.execute("SELECT room_id,held_by FROM items WHERE id='token'").fetchone()
            take_events = db.execute("SELECT COUNT(*) FROM events WHERE kind='take'").fetchone()[0]
            receipts = db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0]
        self.assertEqual(("atrium", None), item)
        self.assertEqual(0, take_events)
        self.assertEqual(0, receipts)
        self.assertEqual("You take Brass token.", engine.act(
            "Rollback", "take token", action_id="failed_take",
        ))


if __name__ == "__main__":
    unittest.main()
