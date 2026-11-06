"""Model-free, host-authorized resident proposal seam regression tests."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.residents import ResidentController


class ResidentProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "residents.sqlite3"
        self.engine = Engine(self.db)
        self.engine.join("Moss")
        self.engine.join("Ember")
        self.moss = ResidentController(self.engine, "Moss")

    def _events(self, actor: str, kind: str) -> int:
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(
                "SELECT COUNT(*) FROM events WHERE actor=? AND kind=?", (actor, kind),
            ).fetchone()[0]

    def test_host_fixed_actor_and_separate_identity(self):
        result = self.moss.commit({"command": "say I remember the arch."}, action_id="turn-1")
        self.assertIn("You say", result)
        self.assertEqual(1, self._events("Moss", "say"))
        self.assertEqual(0, self._events("Ember", "say"))

        ember = ResidentController(self.engine, "Ember")
        ember.commit({"command": "say I remember the arch."}, action_id="turn-1")
        self.assertEqual(1, self._events("Ember", "say"))

    def test_rejected_actor_and_receipt_injection_do_not_mutate_world(self):
        invalid = (
            {"command": "say hi", "actor": "Ember"},
            {"command": "say hi", "action_id": "forged"},
            {"command": "take token"},
            {"command": "drop token"},
            {"command": "go\nnorth"},
            {"command": "say \x1b]52;c;evil"},
            {"command": "say " + "x" * 281},
            {"command": "say"},
            {"command": "join Other"},
        )
        for proposal in invalid:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                self.moss.commit(proposal, action_id="invalid")
        self.assertEqual(0, self._events("Moss", "say"))

    def test_receipt_deduplication_and_replay_conflict(self):
        first = self.moss.commit({"command": "say Echo"}, action_id="resident-1")
        repeated = self.moss.commit({"command": "say Echo"}, action_id="resident-1")
        self.assertEqual(first, repeated)
        self.assertEqual(1, self._events("Moss", "say"))
        with self.assertRaisesRegex(ValueError, "Action ID"):
            self.moss.commit({"command": "say Different"}, action_id="resident-1")
        self.assertEqual(1, self._events("Moss", "say"))

    def test_host_provisions_resident_and_engine_rules_still_apply(self):
        unknown = ResidentController(self.engine, "NotJoined")
        with self.assertRaisesRegex(ValueError, "Join"):
            unknown.commit({"command": "say Not authorized"}, action_id="unknown-1")

        self.assertIn("Corridor", self.moss.commit({"command": "north"}, action_id="move-1"))
        self.assertIn("No exit", self.moss.commit({"command": "go up"}, action_id="move-2"))
        # A different Engine after restart sees the same committed location.
        restarted = Engine(self.db)
        self.assertIn("Corridor", restarted.act("Moss", "look"))

    def test_host_may_explicitly_authorize_items_but_model_cannot_expand_policy(self):
        item_controller = ResidentController(
            self.engine, "Moss", allowed_verbs=frozenset({"take", "drop"}),
        )
        self.assertIn("Brass token", item_controller.commit({"command": "take token"}, action_id="take-1"))
        self.assertIn("Brass token", item_controller.commit({"command": "drop token"}, action_id="drop-1"))
        with self.assertRaisesRegex(ValueError, "not authorized"):
            item_controller.commit({"command": "say Unauthorized"}, action_id="say-2")


if __name__ == "__main__":
    unittest.main()
