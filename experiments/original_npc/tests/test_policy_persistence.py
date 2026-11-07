"""Explicit, actor-scoped learned-state custody; disposable files only."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from npc_policy import OriginalNPCPolicy
from state_store import load_policy_state, save_policy_state


def view(room="atrium", world="sandbox"):
    return {
        "world_id": world, "room": room, "exits": ["north", "east"],
        "items": [], "inventory": [], "goal": {"kind": "explore", "target": ""},
        "allowed_commands": ["go north", "go east", "look"],
    }


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "moss.policy.json"
        self.policy = OriginalNPCPolicy(seed=19, world_id="sandbox", actor_id="Moss")
        self.before = view()
        self.after = view("corridor")
        self.policy.learn(self.before, "go north", self.after, reward=3.0, receipt_id="moss-1")
        self.policy.learn(self.before, "go east", self.before, reward=-2.0, receipt_id="moss-2")

    def save(self, policy=None):
        save_policy_state(self.path, policy or self.policy, world_id="sandbox", actor_id="Moss")

    def test_explicit_restart_restores_preferences_routes_and_replay_receipts(self):
        self.assertFalse(self.path.exists())
        self.save()
        restored = load_policy_state(self.path, world_id="sandbox", actor_id="Moss")
        self.assertEqual(self.policy.propose(self.before), restored.propose(self.before))
        self.assertEqual(self.policy.export_state(), restored.export_state())
        self.assertFalse(restored.learn(self.before, "go north", self.after,
                                        reward=3.0, receipt_id="moss-1"))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            restored.learn(self.before, "go north", self.before,
                           reward=-1.0, receipt_id="moss-1")

    def test_world_actor_binding_protects_reads_and_overwrites(self):
        self.save()
        previous = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "binding"):
            load_policy_state(self.path, world_id="neighbor", actor_id="Moss")
        with self.assertRaisesRegex(ValueError, "binding"):
            load_policy_state(self.path, world_id="sandbox", actor_id="Ember")
        with self.assertRaisesRegex(ValueError, "binding"):
            save_policy_state(self.path, OriginalNPCPolicy(), world_id="sandbox", actor_id="Ember")
        self.assertEqual(previous, self.path.read_bytes())

    def test_policy_with_mixed_world_evidence_cannot_be_saved_as_single_resident(self):
        with self.assertRaisesRegex(ValueError, "world"):
            self.policy.learn(view(world="neighbor"), "go north", view("corridor", "neighbor"),
                              reward=1, receipt_id="foreign")
        self.assertEqual(2, self.policy.export_state()["receipts"])
        self.save()
        self.assertTrue(self.path.exists())

    def test_integrity_rejects_changed_or_truncated_bytes(self):
        self.save()
        contents = json.loads(self.path.read_text())
        contents["payload"]["seed"] = 17
        self.path.write_text(json.dumps(contents))
        with self.assertRaisesRegex(ValueError, "digest"):
            load_policy_state(self.path, world_id="sandbox", actor_id="Moss")
        self.path.write_text("{not valid")
        with self.assertRaises(ValueError):
            load_policy_state(self.path, world_id="sandbox", actor_id="Moss")

    def test_rejects_structurally_invalid_but_rehashed_state(self):
        self.save()
        import hashlib
        envelope = json.loads(self.path.read_text())
        envelope["payload"]["receipts"].append(envelope["payload"]["receipts"][0])
        payload_bytes = json.dumps(envelope["payload"], ensure_ascii=True,
                                   sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope["sha256"] = hashlib.sha256(payload_bytes).hexdigest()
        self.path.write_text(json.dumps(envelope))
        with self.assertRaises(ValueError):
            load_policy_state(self.path, world_id="sandbox", actor_id="Moss")

    def test_failed_atomic_replace_preserves_previous_checkpoint(self):
        self.save()
        previous = self.path.read_bytes()
        self.policy.learn(self.after, "go east", self.after, reward=1, receipt_id="moss-3")
        with patch("state_store.os.replace", side_effect=OSError("injected rename failure")):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(previous, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))

    def test_existing_symlink_checkpoint_is_not_followed(self):
        target = self.path.parent / "victim.json"
        target.write_text("preserve")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises((ValueError, OSError)):
            self.save()
        with self.assertRaises((ValueError, OSError)):
            load_policy_state(self.path, world_id="sandbox", actor_id="Moss")
        self.assertEqual("preserve", target.read_text())


if __name__ == "__main__":
    unittest.main()
