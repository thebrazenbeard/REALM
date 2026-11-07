"""Behavioral contracts for an original, proposal-only REALM NPC learner."""

import math
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from npc_policy import OriginalNPCPolicy


def observation(*, room="atrium", goal_kind="explore", target="", commands=None,
                exits=None, items=None, inventory=None, world_id="world-a"):
    return {
        "world_id": world_id,
        "room": room,
        "exits": exits if exits is not None else ["east", "north"],
        "items": items if items is not None else [],
        "inventory": inventory if inventory is not None else [],
        "goal": {"kind": goal_kind, "target": target},
        "allowed_commands": commands if commands is not None else ["go east", "go north"],
    }


class OriginalNPCPolicyTests(unittest.TestCase):
    def test_proposal_is_only_a_host_enumerated_command(self):
        policy = OriginalNPCPolicy(seed=11)
        current = observation(commands=["go north", "look", "say Greetings"])
        proposal = policy.propose(current)
        self.assertEqual({"command"}, set(proposal))
        self.assertIn(proposal["command"], current["allowed_commands"])
        self.assertNotIn("actor", proposal)
        self.assertNotIn("action_id", proposal)
        self.assertNotIn("world_id", proposal)

    def test_reward_changes_action_while_frozen_baseline_does_not(self):
        learner = OriginalNPCPolicy(seed=9)
        frozen = OriginalNPCPolicy(seed=9)
        before = observation(goal_kind="explore", commands=["go east", "go north"])
        after = observation(room="atrium")
        self.assertEqual(frozen.propose(before), learner.propose(before))
        learner.learn(before, "go east", after, reward=-1.0, receipt_id="test-1")
        learner.learn(before, "go north", after, reward=1.0, receipt_id="test-2")
        self.assertEqual({"command": "go north"}, learner.propose(before))
        self.assertEqual({"command": "go east"}, frozen.propose(before))

    def test_learned_world_edges_reach_a_new_goal(self):
        learner = OriginalNPCPolicy(seed=9)
        before = observation(goal_kind="reach", target="corridor")
        corridor = observation(room="corridor", goal_kind="reach", target="corridor")
        workshop = observation(room="workshop", goal_kind="reach", target="corridor")
        learner.learn(before, "go east", workshop, reward=0, receipt_id="edge-e")
        learner.learn(before, "go north", corridor, reward=0, receipt_id="edge-n")
        self.assertEqual({"command": "go north"}, learner.propose(before))

    def test_item_action_requires_explicit_host_candidate(self):
        learner = OriginalNPCPolicy()
        no_take = observation(goal_kind="take", target="token", items=["token"], commands=["look"])
        self.assertEqual({"command": "look"}, learner.propose(no_take))
        take = observation(goal_kind="take", target="token", items=["token"], commands=["take token", "look"])
        self.assertEqual({"command": "take token"}, learner.propose(take))

    def test_visible_exits_restrict_movement_even_if_candidate_list_is_broad(self):
        learner = OriginalNPCPolicy()
        state = observation(exits=["north"], commands=["go east", "go north"])
        self.assertEqual({"command": "go north"}, learner.propose(state))
        with self.assertRaisesRegex(ValueError, "legal"):
            learner.propose(observation(exits=[], commands=["go east"]))

    def test_completed_goals_do_not_cause_spurious_movement(self):
        learner = OriginalNPCPolicy()
        arrived = observation(room="workshop", goal_kind="reach",
                              target="workshop", commands=["go north", "look"])
        carried = observation(goal_kind="take", target="token",
                              inventory=["token"], commands=["go north", "look"])
        self.assertEqual({"command": "look"}, learner.propose(arrived))
        self.assertEqual({"command": "look"}, learner.propose(carried))
        with self.assertRaisesRegex(ValueError, "complete"):
            learner.propose(observation(room="workshop", goal_kind="reach",
                                        target="workshop", commands=["go north"]))

    def test_duplicate_receipt_does_not_train_twice(self):
        learner = OriginalNPCPolicy()
        before = observation()
        after = observation(room="corridor")
        self.assertTrue(learner.learn(before, "go north", after, reward=1, receipt_id="unique-1"))
        state = learner.export_state()
        self.assertFalse(learner.learn(before, "go north", after, reward=1, receipt_id="unique-1"))
        self.assertEqual(state, learner.export_state())

    def test_cross_world_feedback_cannot_poison_world_model(self):
        learner = OriginalNPCPolicy()
        with self.assertRaisesRegex(ValueError, "world"):
            learner.learn(observation(world_id="world-a"), "go north",
                          observation(world_id="world-b"), reward=1, receipt_id="cross")
        self.assertEqual({}, learner.export_state()["edges"])

    def test_export_distinguishes_tuple_fields_containing_pipe(self):
        learner = OriginalNPCPolicy()
        first = observation(world_id="a|b", room="c")
        first_after = observation(world_id="a|b", room="d")
        second = observation(world_id="a", room="b|c")
        second_after = observation(world_id="a", room="d")
        self.assertTrue(learner.learn(first, "go north", first_after,
                                     reward=1.0, receipt_id="first"))
        self.assertTrue(learner.learn(second, "go north", second_after,
                                     reward=-1.0, receipt_id="second"))
        state = learner.export_state()
        self.assertEqual(2, len(state["values"]))
        self.assertEqual(2, len(state["edges"]))
        # Diagnostic keys have a reversible, collision-free field encoding.
        keys = {tuple(json.loads(key)) for key in state["values"]}
        self.assertIn(("a|b", "c", "explore", "", "go north"), keys)
        self.assertIn(("a", "b|c", "explore", "", "go north"), keys)

    def test_bad_observations_commands_and_reward_fail_closed(self):
        learner = OriginalNPCPolicy()
        bad = observation(commands=["delete all"])
        with self.assertRaises(ValueError):
            learner.propose(bad)
        with self.assertRaises(ValueError):
            learner.propose(observation(commands=[]))
        before = observation()
        with self.assertRaises(ValueError):
            learner.learn(before, "take token", before, reward=1, receipt_id="unauthorized")
        with self.assertRaises(ValueError):
            learner.learn(before, "go north", before, reward=math.nan, receipt_id="nonfinite")
        with self.assertRaises(ValueError):
            learner.learn(before, "go north", before, reward=1, receipt_id="")


if __name__ == "__main__":
    unittest.main()
