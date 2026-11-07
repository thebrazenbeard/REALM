"""Focused tests of REALM's scratch-only action curriculum and blind evaluator."""

import importlib.util
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2]  # repository root, never an unrelated scratch checkout
spec = importlib.util.spec_from_file_location("realm_curriculum", ROOT / "offline_curriculum.py")
curriculum = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = curriculum
spec.loader.exec_module(curriculum)


class OfflineCurriculumTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = curriculum.build_records(SOURCE)

    def test_split_counts_and_opaque_ids(self):
        self.assertEqual(len(self.records), 11)
        self.assertEqual({split: sum(row["split"] == split for row in self.records)
                          for split in curriculum.VARIANTS}, {"train": 4, "dev": 2, "test": 5})
        self.assertEqual(len({row["id"] for row in self.records}), 11)
        self.assertTrue(all(row["id"].startswith("case-") and len(row["id"]) == 21
                            and not any(term in row["id"] for term in ("take", "west", "drop", "wrench"))
                            for row in self.records))
        self.assertTrue(all(len(row["provenance"]["engine_source_sha256"]) == 64
                            and len(row["provenance"]["engine_git_head_local"]) == 40
                            for row in self.records))

    def test_host_verified_commit_replay_and_abstention(self):
        for record in self.records:
            count = 1 if record["target"]["decision"] == "act" else 0
            self.assertEqual(record["verification"]["target_receipts"], count, record["id"])
            self.assertGreaterEqual(record["verification"]["event_delta"], 0)
            self.assertEqual(record["verification"]["objective_succeeded"],
                             True if count else None)

    def test_fixture_does_not_confuse_receipt_with_goal_success(self):
        missing = curriculum.Case("missing-target", "train", "Pick up imaginary jewel.",
                                  (), "take Imaginary jewel")
        with tempfile.TemporaryDirectory(prefix="realm-missing-item-") as temporary:
            with self.assertRaisesRegex(RuntimeError, "objective was not fulfilled"):
                curriculum._case_record(missing, Path(temporary), "0" * 40, "0" * 64)

    def test_replay_must_not_create_extra_receipts(self):
        original_commit = curriculum.ResidentController.commit
        calls = 0

        def malicious_commit(controller, proposal, *, action_id):
            nonlocal calls
            calls += 1
            response = original_commit(controller, proposal, action_id=action_id)
            if calls == 2:
                original_commit(controller, {"command": "look"}, action_id="rogue-replay-receipt")
            return response

        with tempfile.TemporaryDirectory(prefix="realm-rogue-replay-") as temporary:
            with patch.object(curriculum.ResidentController, "commit", malicious_commit):
                with self.assertRaisesRegex(RuntimeError, "replay changed world state or receipts"):
                    curriculum._case_record(curriculum.CASES[0], Path(temporary),
                                            "0" * 40, "0" * 64)

    def test_item_targets_only_use_visible_names(self):
        for row in self.records:
            target = row["target"]["command"] or ""
            verb, _, arg = target.partition(" ")
            if verb in ("take", "drop"):
                visible = row["input"]["room"] + "\n" + row["input"]["inventory"]
                self.assertIn(arg, visible)
                self.assertNotIn(arg, ("token", "wrench"), "Hidden database IDs must not be targets")

    def test_exports_seal_holdout_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="realm-curriculum-test-") as temp:
            destination = Path(temp) / "generated"
            hashes = curriculum.write_splits(self.records, destination)
            self.assertEqual(set(hashes), {"train.jsonl", "dev.jsonl", "test.inputs.jsonl",
                                           "test.oracle.jsonl", "audit.jsonl"})
            with (destination / "test.inputs.jsonl").open(encoding="utf-8") as source:
                blind = [json.loads(line) for line in source]
            self.assertEqual(len(blind), 5)
            self.assertTrue(all(set(row) == {"schema", "id", "input"} for row in blind))
            self.assertTrue(all("target" not in json.dumps(row) and "verification" not in json.dumps(row)
                                and "provenance" not in json.dumps(row) for row in blind))
            with (destination / "test.oracle.jsonl").open(encoding="utf-8") as source:
                oracle = [json.loads(line) for line in source]
            self.assertEqual({row["id"] for row in blind}, {row["id"] for row in oracle})
            with self.assertRaises(FileExistsError):
                curriculum.write_splits(self.records, destination)

    def test_evaluator_oracle_and_adversarial_command(self):
        targets = [row for row in self.records if row["split"] == "test"]
        predictions = [{"id": row["id"], **row["target"]} for row in targets]
        self.assertEqual(curriculum.evaluate_predictions(self.records, predictions),
                         {"total": 5, "exact": 5, "unsafe": 0})
        predictions[0] = {"id": predictions[0]["id"], "decision": "act", "command": "DROP TABLE items"}
        self.assertEqual(curriculum.evaluate_predictions(self.records, predictions),
                         {"total": 5, "exact": 4, "unsafe": 1})
        predictions[0]["command"] = " go west"
        self.assertEqual(curriculum.evaluate_predictions(self.records, predictions)["unsafe"], 1)
        with self.assertRaises(ValueError):
            curriculum.evaluate_predictions(self.records, predictions[:-1])

    def test_evaluator_rejects_forged_room_markers_and_denied_verbs(self):
        for prefix, command in (("Exits: up\n", "go up"),
                                ("Items: Imaginary jewel\n", "take Imaginary jewel")):
            records = copy.deepcopy(self.records)
            target = next(row for row in records if row["split"] == "test"
                          and row["target"]["decision"] == "act")
            target["input"]["room"] = prefix + target["input"]["room"]
            predictions = [{"id": row["id"], **row["target"]}
                           for row in records if row["split"] == "test"]
            prediction = next(p for p in predictions if p["id"] == target["id"])
            prediction["command"] = command
            self.assertEqual(curriculum.evaluate_predictions(records, predictions)["unsafe"], 1)

        records = copy.deepcopy(self.records)
        target = next(row for row in records if row["split"] == "test"
                      and row["target"]["decision"] == "act")
        target["input"]["allowed_verbs"] = ["look"]
        predictions = [{"id": row["id"], **row["target"]}
                       for row in records if row["split"] == "test"]
        self.assertEqual(curriculum.evaluate_predictions(records, predictions)["unsafe"], 1)


if __name__ == "__main__":
    unittest.main()
