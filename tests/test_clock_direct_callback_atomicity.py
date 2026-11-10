"""Direct WorldClock callbacks cannot escape the world-owned transaction."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.clock_runner import run_foreground
from realm.gateway import Gateway
from realm.world_clock import WorldClock


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


class DirectCallbackAtomicityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "world.sqlite3"
        Gateway(self.path, world_id="direct_callback_atomicity")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.clock.schedule("once", due_at=NOW, kind="trusted", payload={})
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE host_effects (event_id TEXT NOT NULL)")

    def _persisted_effects(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [row[0] for row in db.execute("SELECT event_id FROM host_effects")]

    @staticmethod
    def _effect(db, event):
        db.execute("INSERT INTO host_effects(event_id) VALUES (?)", (event.event_id,))

    def _assert_not_acknowledged_and_replay_once(self):
        self.assertEqual([], self._persisted_effects())
        self.assertIsNone(self.clock.lookup("once").processed_at)
        self.assertEqual(["once"], [item.event_id for item in self.clock.tick(NOW, self._effect)])
        self.assertEqual(["once"], self._persisted_effects())
        self.assertEqual([], self.clock.tick(NOW, self._effect))

    def test_direct_commit_then_exception_does_not_duplicate_on_retry(self):
        def callback(db, event):
            self._effect(db, event)
            db.commit()
            raise RuntimeError("injected post-commit crash")

        with self.assertRaisesRegex(RuntimeError, "forbidden transaction"):
            self.clock.tick(NOW, callback)
        self._assert_not_acknowledged_and_replay_once()

    def test_direct_rollback_is_rejected_and_does_not_acknowledge(self):
        def callback(db, event):
            self._effect(db, event)
            db.rollback()

        with self.assertRaisesRegex(RuntimeError, "forbidden transaction"):
            self.clock.tick(NOW, callback)
        self._assert_not_acknowledged_and_replay_once()

    def test_executescript_implicit_commit_is_rejected(self):
        def callback(db, event):
            self._effect(db, event)
            db.executescript("SELECT 1")

        with self.assertRaisesRegex(RuntimeError, "forbidden transaction"):
            self.clock.tick(NOW, callback)
        self._assert_not_acknowledged_and_replay_once()

    def test_swallowed_commit_denial_still_aborts_batch(self):
        def callback(db, event):
            self._effect(db, event)
            try:
                db.commit()
            except sqlite3.DatabaseError:
                pass

        with self.assertRaisesRegex(RuntimeError, "forbidden transaction"):
            self.clock.tick(NOW, callback)
        self._assert_not_acknowledged_and_replay_once()

    def test_direct_attached_foreign_database_is_rejected(self):
        other = self.path.with_name("foreign.sqlite3")
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("CREATE TABLE foreign_effects(value TEXT)")

        def callback(db, event):
            db.execute("ATTACH DATABASE ? AS foreign_world", (str(other),))
            db.execute("INSERT INTO foreign_world.foreign_effects VALUES ('foreign')")

        with self.assertRaisesRegex(RuntimeError, "forbidden.*ATTACH"):
            self.clock.tick(NOW, callback)
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual([], db.execute("SELECT value FROM foreign_effects").fetchall())
        self._assert_not_acknowledged_and_replay_once()

    def test_direct_host_preparation_and_finalizer_are_also_guarded(self):
        class BeginEscape:
            def begin_host_tick(self, db, selected):
                db.commit()

            def __call__(self, db, event):
                raise AssertionError("handler must not run")

        with self.assertRaisesRegex(RuntimeError, "forbidden transaction"):
            self.clock.tick(NOW, BeginEscape())
        self.assertEqual([], self._persisted_effects())

        class FinalizerEscape:
            def __call__(self, db, event):
                db.execute("INSERT INTO host_effects VALUES (?)", (event.event_id,))

            def verify_final_precommit(self, db, processed):
                db.commit()

        with self.assertRaisesRegex(RuntimeError, "forbidden transaction"):
            self.clock.tick(NOW, FinalizerEscape())
        self._assert_not_acknowledged_and_replay_once()

    def test_foreground_registered_callback_still_works(self):
        result = run_foreground(self.clock, {"trusted": self._effect}, pulses=1,
                                now=lambda: NOW, monotonic=lambda: 1.0,
                                sleep=lambda _: None)
        self.assertEqual(("once",), result.event_ids)
        self.assertEqual(["once"], self._persisted_effects())


if __name__ == "__main__":
    unittest.main()
