"""Real REALM Engine/ResidentController integration; temporary SQLite only.

When installed under REALM/experiments/original_npc/, discover REALM/src.
Standalone checking may provide REALM_SRC_ROOT pointing to a source snapshot.
The test never uses or modifies the user's running world.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


HERE = Path(__file__).resolve()
EXPERIMENT = HERE.parents[1]
sys.path.insert(0, str(EXPERIMENT))
from npc_policy import OriginalNPCPolicy
from realm_adapter import ResidentTurnHost


def _locate_realm_source():
    override = os.environ.get("REALM_SRC_ROOT")
    candidates = ([Path(override)] if override else []) + [p / "src" for p in HERE.parents]
    for candidate in candidates:
        if (candidate / "realm" / "engine.py").is_file() and (
            candidate / "realm" / "residents.py"
        ).is_file():
            return candidate
    return None


SOURCE = _locate_realm_source()
if SOURCE:
    sys.path.insert(0, str(SOURCE))
    from realm.engine import Engine
    from realm.residents import ResidentController


@unittest.skipUnless(SOURCE is not None, "actual REALM source not present; set REALM_SRC_ROOT")
class ActualEngineNPCIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="realm-npc-engine-test-")
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name) / "disposable-world.sqlite3"
        self.engine = Engine(self.db_path)
        self.engine.join("Moss")
        self.engine.join("Ember")
        self.policy = OriginalNPCPolicy(seed=42)
        self.world_id = "disposable-world"
        self.goal = "workshop"

    def _rows(self, sql, args=()):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql, args).fetchall()

    def _room(self, actor="Moss"):
        return self._rows("SELECT room_id FROM players WHERE id=?", (actor,))[0][0]

    def _observe(self):
        room = self._room()
        exits = [row[0] for row in self._rows(
            "SELECT direction FROM exits WHERE room_id=? ORDER BY direction", (room,)
        )]
        items = [row[0] for row in self._rows(
            "SELECT id FROM items WHERE room_id=? ORDER BY id", (room,)
        )]
        inventory = [row[0] for row in self._rows(
            "SELECT id FROM items WHERE held_by='Moss' ORDER BY id"
        )]
        return {
            "world_id": self.world_id, "room": room, "exits": exits,
            "items": items, "inventory": inventory,
            "goal": {"kind": "reach", "target": self.goal},
            "allowed_commands": ["go north", "go east", "go south", "go west", "look"],
        }

    @staticmethod
    def _reward(before, command, after, response):
        if before["room"] != after["room"]:
            return 1.0
        return -1.0

    def _receipt_exists(self, action_id):
        # A trusted host query, actor-scoped to Moss in this one SQLite world.
        return bool(self._rows(
            "SELECT 1 FROM action_receipts WHERE actor=? AND action_id=?",
            ("Moss", action_id),
        ))

    def _host(self, *, observer=None, policy=None, engine=None):
        controller = ResidentController(engine or self.engine, "Moss")
        return ResidentTurnHost(controller, observer or self._observe,
                                policy or self.policy, outcome_evaluator=self._reward,
                                receipt_exists=self._receipt_exists)

    def test_real_moves_receipts_learning_actor_isolation_and_restart(self):
        host = self._host()
        before_events = len(self._rows("SELECT id FROM events WHERE actor='Moss'"))
        first = host.step(action_id="moss-real-turn-1")
        self.assertEqual("go north", first.command)
        self.assertIn("Corridor", first.response)
        self.assertEqual("corridor", self._room())
        self.assertEqual("atrium", self._room("Ember"))
        self.assertEqual("learned_with_host_witness", first.learning_status)
        self.assertEqual(1, self.policy.export_state()["receipts"])
        moss_events = len(self._rows("SELECT id FROM events WHERE actor='Moss'"))
        self.assertEqual(before_events + 2, moss_events)  # departure + arrival

        repeated = host.step(action_id="moss-real-turn-1")
        self.assertEqual(first, repeated)
        self.assertEqual(moss_events, len(self._rows("SELECT id FROM events WHERE actor='Moss'")))
        self.assertEqual(1, self.policy.export_state()["receipts"])

        second = host.step(action_id="moss-real-turn-2")
        self.assertEqual("go east", second.command)
        self.assertIn("Workshop", second.response)
        self.assertEqual("workshop", self._room())
        self.assertEqual("learned_with_host_witness", second.learning_status)
        self.assertEqual(2, self.policy.export_state()["receipts"])

        # Every actual command is in the actor-scoped Engine receipt table.
        self.assertEqual(
            [("moss-real-turn-1", "go north"), ("moss-real-turn-2", "go east")],
            self._rows("SELECT action_id,command FROM action_receipts WHERE actor='Moss' ORDER BY action_id"),
        )
        self.assertEqual([], self._rows("SELECT action_id FROM action_receipts WHERE actor='Ember'"))
        restarted = Engine(self.db_path)
        self.assertIn("Workshop", restarted.act("Moss", "look"))
        self.assertIn("Atrium", restarted.act("Ember", "look"))
        self.assertEqual(first.response, restarted.act("Moss", "go north", action_id="moss-real-turn-1"))

    def test_disposable_sqlite_world_isolated_from_second_world(self):
        other_path = Path(self.temporary.name) / "other-world.sqlite3"
        other = Engine(other_path)
        other.join("Moss")
        result = self._host().step(action_id="moss-scope-1")
        self.assertIn("Corridor", result.response)
        self.assertIn("Atrium", other.act("Moss", "look"))
        with closing(sqlite3.connect(other_path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0])

    def test_restart_engine_duplicate_receipt_does_not_double_learn(self):
        # A speech action produces a real event but leaves the room and policy
        # inputs unchanged, so the same proposal can recur after restart.
        def speak_only_observer():
            state = self._observe()
            state["allowed_commands"] = ["say The arch remembers."]
            return state

        original = self._host(observer=speak_only_observer)
        first = original.step(action_id="restart-receipt-1")
        self.assertEqual("say The arch remembers.", first.command)
        self.assertEqual("learned_with_host_witness", first.learning_status)
        self.assertEqual(1, self.policy.export_state()["receipts"])
        speech_events = len(self._rows(
            "SELECT id FROM events WHERE actor=? AND kind='say'", ("Moss",),
        ))
        self.assertEqual(1, speech_events)

        # Mimic host restart: actual Engine state and receipt persist; new
        # local learner has no in-memory receipt cache.
        restarted_engine = Engine(self.db_path)
        empty_after_restart = OriginalNPCPolicy(seed=42)
        replay_host = self._host(observer=speak_only_observer,
                                 policy=empty_after_restart, engine=restarted_engine)
        replayed = replay_host.step(action_id="restart-receipt-1")
        self.assertEqual("replayed_receipt", replayed.learning_status)
        self.assertEqual(first.response, replayed.response)
        self.assertEqual(0, empty_after_restart.export_state()["receipts"])
        self.assertEqual(speech_events, len(self._rows(
            "SELECT id FROM events WHERE actor=? AND kind='say'", ("Moss",),
        )))
        self.assertEqual(1, len(self._rows(
            "SELECT action_id FROM action_receipts WHERE actor=? AND action_id=?",
            ("Moss", "restart-receipt-1"),
        )))


if __name__ == "__main__":
    unittest.main()
