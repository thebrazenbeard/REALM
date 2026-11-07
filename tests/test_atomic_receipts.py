"""Real SQLite tests for transaction-coupled action receipt freshness."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.residents import ResidentController


class AtomicReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="realm-atomic-receipt-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "world.sqlite3"
        self.engine_a = Engine(self.path)
        self.engine_b = Engine(self.path)
        self.engine_a.join("Moss")

    def _rows(self, query, args=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(query, args).fetchall()

    def test_separate_controllers_same_receipt_report_exactly_one_new_commit(self):
        left = ResidentController(self.engine_a, "Moss")
        right = ResidentController(self.engine_b, "Moss")
        start = threading.Barrier(2)

        def submit(controller):
            start.wait(timeout=3)
            return controller.commit_with_receipt({"command": "north"}, action_id="one-logical-turn")

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(submit, left)
            b = pool.submit(submit, right)
            results = [a.result(timeout=12), b.result(timeout=12)]

        self.assertEqual([False, True], sorted(result.receipt_created for result in results))
        self.assertEqual(results[0].response, results[1].response)
        self.assertIn("Corridor", results[0].response)
        self.assertEqual(1, len(self._rows("SELECT action_id FROM action_receipts")))
        self.assertEqual(2, len(self._rows("SELECT kind FROM events WHERE actor='Moss' AND kind IN ('leave','move')")))
        self.assertEqual("corridor", self._rows("SELECT room_id FROM players WHERE id='Moss'")[0][0])

    def test_replayed_and_conflicting_receipt_are_distinguished(self):
        first = ResidentController(self.engine_a, "Moss").commit_with_receipt(
            {"command": "say Echo"}, action_id="chat-1"
        )
        again = ResidentController(self.engine_b, "Moss").commit_with_receipt(
            {"command": "say Echo"}, action_id="chat-1"
        )
        self.assertTrue(first.receipt_created)
        self.assertFalse(again.receipt_created)
        self.assertEqual(first.response, again.response)
        self.assertEqual(1, len(self._rows("SELECT id FROM events WHERE actor='Moss' AND kind='say'")))
        with self.assertRaisesRegex(ValueError, "Action ID"):
            ResidentController(self.engine_b, "Moss").commit_with_receipt(
                {"command": "say Different"}, action_id="chat-1"
            )

    def test_legacy_act_and_commit_stay_response_strings(self):
        controller = ResidentController(self.engine_a, "Moss")
        legacy = controller.commit({"command": "look"}, action_id="look-1")
        self.assertIsInstance(legacy, str)
        self.assertIsInstance(self.engine_b.act("Moss", "look", action_id="look-1"), str)
        result = controller.commit_with_receipt({"command": "look"}, action_id="look-1")
        self.assertEqual(legacy, result.response)
        self.assertFalse(result.receipt_created)


if __name__ == "__main__":
    unittest.main()
