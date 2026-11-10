"""Resident scene custody checks must run inside the Engine writer transaction."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.residents import ResidentController


class ResidentWriterGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "world.sqlite3"
        self.engine = Engine(self.db_path)
        self.engine.join("Scout")
        self.controller = ResidentController(
            self.engine, "Scout", allowed_verbs=frozenset({"say"}),
        )

    def _counts(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            return (
                db.execute("SELECT COUNT(*) FROM events WHERE actor='Scout' AND kind='say'").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM action_receipts WHERE actor='Scout'").fetchone()[0],
            )

    def test_guard_denial_aborts_action_and_receipt(self):
        seen = []

        def stale_scene(db):
            seen.append(db.in_transaction)
            self.assertEqual("Scout", db.execute(
                "SELECT id FROM players WHERE id='Scout'"
            ).fetchone()[0])
            raise PermissionError("NPC scene expired")

        with self.assertRaisesRegex(PermissionError, "scene expired"):
            self.controller.commit(
                {"command": "say no stale scene"}, action_id="scene-1",
                writer_guard=stale_scene,
            )
        self.assertEqual([True], seen)
        self.assertEqual((0, 0), self._counts())

    def test_guard_runs_on_success_and_receipt_replay(self):
        observed = []

        def allow(db):
            observed.append(db.in_transaction)

        self.controller.commit(
            {"command": "say safe"}, action_id="scene-2", writer_guard=allow,
        )
        self.assertEqual((1, 1), self._counts())
        self.controller.commit(
            {"command": "say safe"}, action_id="scene-2", writer_guard=allow,
        )
        self.assertEqual([True, True], observed)
        self.assertEqual((1, 1), self._counts())

        def deny(_db):
            raise PermissionError("scene changed on retry")

        with self.assertRaisesRegex(PermissionError, "scene changed"):
            self.controller.commit(
                {"command": "say safe"}, action_id="scene-2", writer_guard=deny,
            )
        self.assertEqual((1, 1), self._counts())

    def test_non_callable_guard_is_rejected_before_mutation(self):
        with self.assertRaises(TypeError):
            self.controller.commit(
                {"command": "say safe"}, action_id="scene-3", writer_guard="not callable",
            )
        self.assertEqual((0, 0), self._counts())


if __name__ == "__main__":
    unittest.main()
