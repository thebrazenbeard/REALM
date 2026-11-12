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

    def test_player_receipt_cannot_preclaim_standalone_resident_action(self):
        # A saved player action is not proof that the host authorized an NPC
        # turn. Previously this silently replayed the stale Corridor result.
        self.engine.act("Moss", "north", action_id="player-preclaimed-host-id")
        self.engine.act("Moss", "south", action_id="retreat-after-player-preclaim")
        with self.assertRaisesRegex(ValueError, "legacy/player receipt"):
            self.moss.commit({"command": "north"}, action_id="player-preclaimed-host-id")
        self.assertIn("The Atrium", self.engine.act("Moss", "where"))
        self.assertEqual(2, self._events("Moss", "move"))

    def test_direct_resident_receipt_id_matches_scheduled_host_ascii_grammar(self):
        for bad in ("café-1", "with space", "-starts-dash", "bad\nline", "a" * 129):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "safe ASCII"):
                self.moss.commit({"command": "say No invalid host ID"}, action_id=bad)
        self.assertEqual(0, self._events("Moss", "say"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0])

    def test_host_receipt_is_not_player_claimable_and_replays_once(self):
        self.assertIn("You say", self.moss.commit({"command": "say Host speech"}, action_id="shared-host"))
        with closing(sqlite3.connect(self.db)) as db:
            rows = db.execute(
                "SELECT actor,command FROM action_receipts WHERE action_id=?", ("shared-host",)
            ).fetchall()
        self.assertEqual([("@resident:Moss", "say Host speech")], rows)
        # A player may use the *same text ID* only under their own distinct
        # receipt principal. That must not replace the trusted host receipt.
        self.engine.act("Moss", "say Player speech", action_id="shared-host")
        self.assertIn("Host speech", self.moss.commit(
            {"command": "say Host speech"}, action_id="shared-host"
        ))
        self.assertEqual(2, self._events("Moss", "say"))
        with self.assertRaisesRegex(ValueError, "Action ID"):
            self.moss.commit({"command": "say Different"}, action_id="shared-host")

    def test_fresh_read_only_host_replay_observes_world_currently(self):
        viewer = ResidentController(self.engine, "Moss", allowed_verbs=frozenset({"look"}))
        self.assertIn("Brass token", viewer.commit({"command": "look"}, action_id="host-look"))
        self.engine.act("Ember", "take token")
        self.assertNotIn("Brass token", viewer.commit({"command": "look"}, action_id="host-look"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("", db.execute(
                "SELECT response FROM action_receipts WHERE actor=? AND action_id=?",
                ("@resident:Moss", "host-look"),
            ).fetchone()[0])

    def test_writer_guard_is_applied_before_host_mutation_or_retry(self):
        def revoked(_db):
            raise ValueError("Host authority revoked")

        with self.assertRaisesRegex(ValueError, "Host authority revoked"):
            self.moss.commit({"command": "say Blocked"}, action_id="host-guard", writer_guard=revoked)
        self.assertEqual(0, self._events("Moss", "say"))
        self.moss.commit({"command": "say Authorized"}, action_id="host-guard")
        with self.assertRaisesRegex(ValueError, "Host authority revoked"):
            self.moss.commit({"command": "say Authorized"}, action_id="host-guard", writer_guard=revoked)
        self.assertEqual(1, self._events("Moss", "say"))

    def test_resident_receipts_are_case_canonical_and_world_isolated(self):
        alias = ResidentController(self.engine, "mOSS", allowed_verbs=frozenset({"say"}))
        alias.commit({"command": "say Case canonical"}, action_id="alias-host")
        self.assertIn("Case canonical", self.moss.commit(
            {"command": "say Case canonical"}, action_id="alias-host"
        ))
        self.assertEqual(1, self._events("Moss", "say"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM action_receipts WHERE actor=? AND action_id=?",
                ("@resident:Moss", "alias-host"),
            ).fetchone()[0])

    def test_direct_and_turn_host_share_resident_idempotency_custody(self):
        from realm.npc_local_model import LocalChatConfig, LocalChatProposer
        from realm.npc_turn_host import ResidentTurn, ResidentTurnHost

        first = self.moss.commit({"command": "say One trusted action"}, action_id="shared-custody")
        # Port is deliberately unserved: a host receipt must bypass inference.
        proposer = LocalChatProposer(LocalChatConfig(
            endpoint="http://127.0.0.1:1/v1/chat/completions", model="offline-fixture",
        ))
        host = ResidentTurnHost(self.engine, proposer)
        second, = host.run_tick([ResidentTurn(self.moss, "Private host view", "shared-custody")])
        self.assertTrue(second.replayed)
        self.assertEqual(first, second.response)
        self.assertEqual(1, self._events("Moss", "say"))
        # Policy is checked on replay. A tighter host cannot inherit an older
        # receipt that the current controller would forbid.
        restricted = ResidentController(self.engine, "Moss", allowed_verbs=frozenset({"look"}))
        with self.assertRaisesRegex(ValueError, "not authorized"):
            host.run_tick([ResidentTurn(restricted, "Private host view", "shared-custody")])
        self.assertEqual(1, self._events("Moss", "say"))

    def test_host_receipt_insert_failure_rolls_back_speech_event(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                CREATE TRIGGER abort_host_receipt BEFORE INSERT ON action_receipts
                WHEN NEW.actor LIKE '@resident:%'
                BEGIN SELECT RAISE(ABORT, 'host receipt blocked'); END;
            """)
        with self.assertRaisesRegex(sqlite3.DatabaseError, "host receipt blocked"):
            self.moss.commit({"command": "say Cannot escape rollback"}, action_id="host-rollback")
        self.assertEqual(0, self._events("Moss", "say"))

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
