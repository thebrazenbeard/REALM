"""Synchronous NPC scheduler regression, including actual disposable Engine."""

import itertools
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from npc_policy import OriginalNPCPolicy
from realm_adapter import ResidentTurnHost, TurnResult
from turn_scheduler import ResidentTurnScheduler
from realm.engine import Engine
from realm.residents import ResidentController


def observation(*, world_id="world-a", room="atrium", goal=None):
    return {
        "world_id": world_id, "room": room,
        "goal": goal if goal is not None else {"kind": "reach", "target": "workshop"},
        "exits": ["north"], "items": [], "inventory": [],
        "allowed_commands": ["go north", "look"],
    }


class FakeHost:
    def __init__(self, on_step=None):
        self.calls = []
        self.on_step = on_step

    def step(self, *, action_id):
        self.calls.append(action_id)
        if self.on_step:
            self.on_step()
        return TurnResult(action_id, "go north", "No exit leads north", "disabled")


class SchedulerBoundedTests(unittest.TestCase):
    def scheduler(self, host, read, ids, **options):
        return ResidentTurnScheduler(
            host, read, lambda: next(ids), actor_id="Moss", world_id="world-a", **options,
        )

    def test_already_completed_reach_and_take_goals_commit_nothing(self):
        for goal, room, inventory in [
            ({"kind": "reach", "target": "atrium"}, "atrium", []),
            ({"kind": "take", "target": "token"}, "atrium", ["token"]),
        ]:
            with self.subTest(goal=goal):
                state = observation(room=room, goal=goal)
                state["inventory"] = inventory
                host = FakeHost()
                schedule = self.scheduler(host, lambda: state, iter(["should-not-be-used"]))
                result = schedule.run()
                self.assertEqual("goal_complete", result.reason)
                self.assertEqual((), result.turns)
                self.assertEqual([], host.calls)

    def test_stalled_turns_are_bounded_and_do_not_loop_forever(self):
        host = FakeHost()
        ids = iter([f"moss-turn-{n}" for n in range(20)])
        result = self.scheduler(host, observation, ids, max_turns=20, stall_limit=2).run()
        self.assertEqual("stalled", result.reason)
        self.assertEqual(2, len(result.turns))
        self.assertEqual(2, len(host.calls))

    def test_changes_of_goal_stop_before_next_action(self):
        state = observation()
        def change_goal():
            state["goal"] = {"kind": "take", "target": "token"}
        host = FakeHost(change_goal)
        result = self.scheduler(host, lambda: state, iter(["turn-1", "turn-2"])).run()
        self.assertEqual("objective_changed", result.reason)
        self.assertEqual(1, len(host.calls))

    def test_host_world_mismatch_refuses_actions(self):
        host = FakeHost()
        state = observation(world_id="other-world")
        schedule = self.scheduler(host, lambda: state, iter(["turn-1"]))
        with self.assertRaisesRegex(ValueError, "world_id"):
            schedule.run()
        self.assertEqual([], host.calls)

    def test_action_receipts_are_unique_across_batches(self):
        host = FakeHost()
        schedule = self.scheduler(host, observation, iter(["same", "same"]), max_turns=1)
        self.assertEqual("budget_exhausted", schedule.run().reason)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            schedule.run()
        self.assertEqual(["same"], host.calls)

    def test_bad_receipt_from_host_is_rejected(self):
        class CorruptHost:
            def step(self, *, action_id):
                return TurnResult("other-receipt", "look", "look", "disabled")
        scheduler = self.scheduler(CorruptHost(), observation, iter(["actual"]))
        with self.assertRaisesRegex(ValueError, "mismatched"):
            scheduler.run()

    def test_bad_configuration_rejected(self):
        host = FakeHost()
        for opts in [{"max_turns": 0}, {"stall_limit": 11}, {"actor_id": ""}, {"world_id": ""}]:
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                kwargs = {"actor_id": "Moss", "world_id": "world-a", **opts}
                ResidentTurnScheduler(host, observation, lambda: "x", **kwargs)


class ActualRealmSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="realm-resident-schedule-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "demo.sqlite3"
        self.engine = Engine(self.db)
        self.engine.join("Moss")
        self.engine.join("Ember")
        self.policy = OriginalNPCPolicy(seed=8)
        self.goal = {"kind": "reach", "target": "workshop"}

    def query(self, sql, *args):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(sql, args).fetchall()

    def observe(self):
        room = self.query("SELECT room_id FROM players WHERE id='Moss'")[0][0]
        exits = [x[0] for x in self.query("SELECT direction FROM exits WHERE room_id=? ORDER BY direction", room)]
        return {
            "world_id": "world-a", "room": room,
            "goal": self.goal,
            "exits": exits,
            "items": [x[0] for x in self.query("SELECT id FROM items WHERE room_id=? ORDER BY id", room)],
            "inventory": [x[0] for x in self.query("SELECT id FROM items WHERE held_by='Moss' ORDER BY id")],
            "allowed_commands": ["go " + direction for direction in exits] + ["look"],
        }

    def test_autonomous_two_hop_goal_and_no_noop_receipts(self):
        resident = ResidentController(self.engine, "Moss")
        def existed(action_id):
            return bool(self.query("SELECT 1 FROM action_receipts WHERE actor='Moss' AND action_id=?", action_id))
        def reward(before, command, after, response):
            return 1.0 if before["room"] != after["room"] else -1.0
        host = ResidentTurnHost(
            resident, self.observe, self.policy, outcome_evaluator=reward, receipt_exists=existed,
        )
        id_counter = itertools.count(1)
        scheduler = ResidentTurnScheduler(
            host, self.observe, lambda: f"moss-scheduled-{next(id_counter)}",
            actor_id="Moss", world_id="world-a", max_turns=10,
        )
        outcome = scheduler.run()
        self.assertEqual("goal_complete", outcome.reason)
        self.assertEqual(["go north", "go east"], [x.command for x in outcome.turns])
        self.assertEqual(["learned_with_atomic_receipt"] * 2, [x.learning_status for x in outcome.turns])
        self.assertEqual("workshop", self.observe()["room"])
        self.assertEqual(2, len(self.query("SELECT * FROM action_receipts WHERE actor='Moss'")))
        self.assertEqual(2, self.policy.export_state()["receipts"])
        self.assertEqual("atrium", self.query("SELECT room_id FROM players WHERE id='Ember'")[0][0])
        # A new scheduling call sees the completed goal before generating any
        # new receipt or making a read-only 'look' transaction.
        again = scheduler.run()
        self.assertEqual("goal_complete", again.reason)
        self.assertEqual((), again.turns)
        self.assertEqual(2, len(self.query("SELECT * FROM action_receipts WHERE actor='Moss'")))


if __name__ == "__main__":
    unittest.main()
