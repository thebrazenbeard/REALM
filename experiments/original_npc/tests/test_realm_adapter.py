"""Offline contract tests; fake controller mirrors REALM 09fc7cf seam."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realm_adapter import ResidentTurnHost


class FakeResidentController:
    def __init__(self):
        self.room = "atrium"
        self.events = []
        self.receipts = {}
        self.validated = 0

    def validate(self, proposal):
        self.validated += 1
        if set(proposal) != {"command"}:
            raise ValueError("Extra proposal keys")
        if proposal["command"].partition(" ")[0] not in {"look", "north", "west"}:
            raise ValueError("Not authorized")
        return proposal

    def commit(self, proposal, *, action_id):
        self.validate(proposal)
        command = proposal["command"]
        if action_id in self.receipts:
            old_command, old_response = self.receipts[action_id]
            if old_command != command:
                raise ValueError("Action ID conflict")
            return old_response
        if command == "north" and self.room == "atrium":
            self.room = "corridor"
            self.events.append((action_id, "move", "corridor"))
            response = "The Corridor\nExits: south"
        elif command == "look":
            response = f"Current room: {self.room}"
        else:
            response = "No exit leads that way."
        self.receipts[action_id] = (command, response)
        return response


class AtomicFakeResidentController(FakeResidentController):
    def commit_with_receipt(self, proposal, *, action_id):
        existed = action_id in self.receipts
        response = self.commit(proposal, action_id=action_id)
        return SimpleNamespace(response=response, receipt_created=not existed)


class Policy:
    def __init__(self, command):
        self.command = command
        self.seen = []
        self.learning = []

    def propose(self, observation):
        self.seen.append(observation)
        return {"command": self.command}

    def learn(self, before, command, after, *, reward, receipt_id):
        self.learning.append((before, command, after, reward, receipt_id))
        return True


class ResidentHostTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeResidentController()
        self.policy = Policy("north")
        self.snapshot = {"world_id": "test-world", "room": "atrium", "exits": ["north"],
                         "items": [], "inventory": [], "goal": {"kind": "explore", "target": ""},
                         "allowed_commands": ["look", "north", "west"]}
        self.observations = 0

    def observe(self):
        self.observations += 1
        return {**self.snapshot, "room": self.controller.room}

    def test_valid_move_passes_controller_and_replay_does_not_duplicate(self):
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        first = host.step(action_id="host-turn-1")
        self.assertEqual("north", first.command)
        self.assertEqual("corridor", self.controller.room)
        self.assertEqual(1, len(self.controller.events))
        self.assertGreaterEqual(self.controller.validated, 2)
        self.assertEqual("disabled", first.learning_status)
        self.policy.command = "west"  # A retry must NOT choose a new proposal.
        self.assertEqual(first, host.step(action_id="host-turn-1"))
        self.assertEqual(1, len(self.controller.events))

    def test_invalid_move_cannot_change_position_even_if_allowed_as_verb(self):
        self.policy.command = "west"
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        outcome = host.step(action_id="host-west")
        self.assertIn("No exit", outcome.response)
        self.assertEqual("atrium", self.controller.room)
        self.assertEqual([], self.controller.events)
        self.assertEqual("disabled", outcome.learning_status)

    def test_model_cannot_expand_actions_or_inject_actor(self):
        self.policy.command = "take token"
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        with self.assertRaisesRegex(ValueError, "allowed actions"):
            host.step(action_id="host-unsafe")
        self.assertEqual({}, self.controller.receipts)

        self.policy.propose = lambda _: {"command": "look", "actor": "Patrick"}
        with self.assertRaisesRegex(ValueError, "only"):
            host.step(action_id="host-spoof")
        self.assertEqual({}, self.controller.receipts)

    def test_controller_rejects_verb_even_when_observer_wrongly_advertises_it(self):
        self.snapshot["allowed_commands"].append("take token")
        self.policy.command = "take token"
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        with self.assertRaisesRegex(ValueError, "Not authorized"):
            host.step(action_id="host-unsafe-2")
        self.assertEqual([], self.controller.events)

    def test_model_gets_detached_data_only_not_controller_or_db(self):
        self.snapshot["secret_token"] = "THIS_MUST_NOT_BE_VISIBLE"
        self.snapshot["engine"] = self.controller  # Must not try to JSON-encode this.
        self.snapshot["goal"] = {"kind": "explore", "target": "", "private_notes": "SECRET"}
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        host.step(action_id="host-step-3")
        seen = self.policy.seen[0]
        self.assertNotIn("controller", seen)
        self.assertNotIn("engine", seen)
        self.assertNotIn("db", seen)
        self.assertNotIn("secret_token", seen)
        self.assertNotIn("private_notes", seen["goal"])
        self.assertEqual("test-world", seen["world_id"])
        seen["allowed_commands"].append("take token")
        self.assertNotIn("take token", self.snapshot["allowed_commands"])

    def test_legacy_precommit_witness_cannot_enable_learning(self):
        def eval_result(before, command, after, response):
            return 1.0 if before["room"] != after["room"] and "Corridor" in response else -1.0

        host = ResidentTurnHost(
            self.controller, self.observe, self.policy, outcome_evaluator=eval_result,
            receipt_exists=lambda rid: rid in self.controller.receipts,
        )
        result = host.step(action_id="host-learn-1")
        self.assertEqual("receipt_unverified", result.learning_status)
        self.assertEqual(0, len(self.policy.learning))
        self.assertEqual("corridor", self.controller.room)
        host.step(action_id="host-learn-1")
        self.assertEqual(0, len(self.policy.learning))

    def test_legacy_false_negative_witness_does_not_train_on_replayed_commit(self):
        # Models a concurrent host having inserted the exact receipt after a
        # legacy receipt_exists=False probe but before this host commits.
        prior = self.controller.commit({"command": "north"}, action_id="racy-id")
        self.assertIn("Corridor", prior)
        host = ResidentTurnHost(
            self.controller, self.observe, self.policy,
            outcome_evaluator=lambda *_: 1.0,
            receipt_exists=lambda action_id: False,
        )
        replay = host.step(action_id="racy-id")
        self.assertEqual(prior, replay.response)
        self.assertEqual("receipt_unverified", replay.learning_status)
        self.assertEqual([], self.policy.learning)
        self.assertEqual(1, len(self.controller.events))

    def test_atomic_commit_reports_new_receipt_then_replay_without_relearning(self):
        self.controller = AtomicFakeResidentController()
        witness_calls = []
        def legacy_witness(action_id):
            witness_calls.append(action_id)
            raise AssertionError("Atomic path must never run legacy precommit witness")

        host = ResidentTurnHost(
            self.controller, self.observe, self.policy,
            outcome_evaluator=lambda *_: 1.0, receipt_exists=legacy_witness,
        )
        first = host.step(action_id="atomic-1")
        self.assertEqual("learned_with_atomic_receipt", first.learning_status)
        self.assertEqual(1, len(self.policy.learning))
        self.assertEqual([], witness_calls)

        replay_policy = Policy("north")
        restarted = ResidentTurnHost(
            self.controller, self.observe, replay_policy,
            outcome_evaluator=lambda *_: 1.0, receipt_exists=legacy_witness,
        )
        second = restarted.step(action_id="atomic-1")
        self.assertEqual("replayed_receipt", second.learning_status)
        self.assertEqual(first.response, second.response)
        self.assertEqual([], replay_policy.learning)
        self.assertEqual(1, len(self.controller.events))
        self.assertEqual([], witness_calls)

    def test_invalid_atomic_commit_provenance_is_rejected_not_learned(self):
        for invalid in (
            SimpleNamespace(response="ok", receipt_created="true"),
            SimpleNamespace(response=7, receipt_created=True),
            SimpleNamespace(response="ok"),
        ):
            with self.subTest(invalid=invalid):
                class MalformedAtomic(FakeResidentController):
                    def commit_with_receipt(self, proposal, *, action_id):
                        return invalid

                controller = MalformedAtomic()
                policy = Policy("north")
                host = ResidentTurnHost(
                    controller, self.observe, policy,
                    outcome_evaluator=lambda *_: 1.0,
                )
                with self.assertRaisesRegex(ValueError, "invalid provenance"):
                    host.step(action_id="invalid-atomic")
                self.assertEqual([], policy.learning)

    def test_nonfinite_or_failing_reward_cannot_claim_learning(self):
        self.controller = AtomicFakeResidentController()
        host = ResidentTurnHost(self.controller, self.observe, self.policy,
                                outcome_evaluator=lambda *_: float("nan"),
                                receipt_exists=lambda rid: rid in self.controller.receipts)
        result = host.step(action_id="invalid-reward")
        self.assertEqual("invalid_reward", result.learning_status)
        self.assertEqual([], self.policy.learning)
        self.assertEqual(1, len(self.controller.events))

    def test_missing_receipt_witness_allows_commit_but_disables_learning(self):
        host = ResidentTurnHost(
            self.controller, self.observe, self.policy,
            outcome_evaluator=lambda *_: 1.0,
        )
        result = host.step(action_id="unwitnessed")
        self.assertEqual("receipt_unverified", result.learning_status)
        self.assertEqual([], self.policy.learning)
        self.assertEqual(1, len(self.controller.events))

    def test_new_adapter_after_restart_does_not_learn_preexisting_receipt(self):
        witness = lambda rid: rid in self.controller.receipts
        original = ResidentTurnHost(
            self.controller, self.observe, self.policy,
            outcome_evaluator=lambda *_: 1.0,
            receipt_exists=witness,
        )
        first = original.step(action_id="durable-1")
        self.assertEqual("receipt_unverified", first.learning_status)
        self.assertEqual(1, len(self.controller.events))
        self.assertEqual([], self.policy.learning)

        # Emulate a process restart: new adapter and policy, existing durable
        # Engine receipt. The same command returns its stored response.
        replay_policy = Policy("north")
        restarted = ResidentTurnHost(
            self.controller, self.observe, replay_policy,
            outcome_evaluator=lambda *_: 1.0,
            receipt_exists=witness,
        )
        second = restarted.step(action_id="durable-1")
        self.assertEqual(first.response, second.response)
        self.assertEqual("replayed_receipt", second.learning_status)
        self.assertEqual([], replay_policy.learning)
        self.assertEqual(1, len(self.controller.events))

    def test_bad_receipt_witness_rejects_before_commit(self):
        for lookup in (lambda rid: "false", lambda rid: None,
                       lambda rid: (_ for _ in ()).throw(OSError("Unavailable"))):
            with self.subTest(lookup=lookup):
                controller = FakeResidentController()
                host = ResidentTurnHost(
                    controller, self.observe, self.policy,
                    outcome_evaluator=lambda *_: 1.0,
                    receipt_exists=lookup,
                )
                with self.assertRaisesRegex(ValueError, "receipt witness"):
                    host.step(action_id="must-not-commit")
                self.assertEqual({}, controller.receipts)

    def test_host_observer_cannot_switch_world_during_one_resident_lifetime(self):
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        host.step(action_id="first-world-turn")
        self.snapshot["world_id"] = "neighbor-world"
        with self.assertRaisesRegex(ValueError, "world_id"):
            host.step(action_id="second-world-turn")
        self.assertEqual(1, len(self.controller.events))

    def test_pending_retry_rechecks_current_host_authorization(self):
        class UnavailableController(FakeResidentController):
            def __init__(self):
                super().__init__()
                self.unavailable_once = True

            def commit(self, proposal, *, action_id):
                if self.unavailable_once:
                    self.unavailable_once = False
                    raise OSError("Temporary storage unavailable before commit")
                return super().commit(proposal, action_id=action_id)

        self.controller = UnavailableController()
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        with self.assertRaises(OSError):
            host.step(action_id="pending-1")
        # Host revokes movement before retry. Old proposal must NOT execute.
        self.snapshot["allowed_commands"] = ["look", "west"]
        with self.assertRaisesRegex(ValueError, "no longer authorized"):
            host.step(action_id="pending-1")
        self.assertEqual("atrium", self.controller.room)
        self.assertEqual([], self.controller.events)

    def test_pending_retry_cannot_cross_world_even_if_command_remains_allowed(self):
        class UnavailableController(FakeResidentController):
            def commit(self, proposal, *, action_id):
                raise OSError("Unavailable")

        self.controller = UnavailableController()
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        with self.assertRaises(OSError):
            host.step(action_id="pending-2")
        self.snapshot["world_id"] = "foreign-world"
        with self.assertRaisesRegex(ValueError, "world_id"):
            host.step(action_id="pending-2")

    def test_pending_retry_rejects_changed_room_goal_or_visible_state(self):
        class UnavailableController(FakeResidentController):
            def commit(self, proposal, *, action_id):
                raise OSError("Unavailable before mutation")

        for changed_field, new_value in (
            ("room", "corridor"),
            ("goal", {"kind": "reach", "target": "workshop"}),
            ("exits", ["south"]),
            ("items", ["token"]),
            ("inventory", ["wrench"]),
            ("allowed_commands", ["north", "look", "west"]),
        ):
            with self.subTest(changed_field=changed_field):
                self.controller = UnavailableController()
                self.snapshot = {
                    "world_id": "test-world", "room": "atrium", "exits": ["north"],
                    "items": [], "inventory": [], "goal": {"kind": "explore", "target": ""},
                    "allowed_commands": ["look", "north", "west"],
                }
                host = ResidentTurnHost(self.controller, self.observe, self.policy)
                with self.assertRaises(OSError):
                    host.step(action_id="pending-drift")
                if changed_field == "room":
                    self.controller.room = new_value
                else:
                    self.snapshot[changed_field] = new_value
                with self.assertRaisesRegex(ValueError, "observation changed"):
                    host.step(action_id="pending-drift")
                self.assertEqual([], self.controller.events)

    def test_observation_exceeds_model_candidate_capacity_fails_closed(self):
        self.snapshot["allowed_commands"] = [f"say message-{i}" for i in range(33)]
        host = ResidentTurnHost(self.controller, self.observe, self.policy)
        with self.assertRaisesRegex(ValueError, "1-32"):
            host.step(action_id="too-many")
        self.assertEqual({}, self.controller.receipts)


if __name__ == "__main__":
    unittest.main()
