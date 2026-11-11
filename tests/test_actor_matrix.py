"""Host-executed, model-free 160-turn resident proposal benchmark controls."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "evals" / "actor_matrix" / "runner.py"
spec = importlib.util.spec_from_file_location("realm_actor_matrix", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ActorMatrixTests(unittest.TestCase):
    def setUp(self):
        self.cases = runner.generated_cases()
        self.scripted = {row["case_id"]: row for row in runner.scripted_control()}

    def test_corpus_is_eight_actors_twenty_turns_and_two_worlds(self):
        self.assertEqual(160, len(self.cases))
        self.assertEqual(160, len({c["case_id"] for c in self.cases}))
        self.assertEqual(8, len({c["actor"] for c in self.cases}))
        self.assertEqual({"alpha", "beta"}, {c["world"] for c in self.cases})
        for actor in runner.ACTORS:
            self.assertEqual(20, sum(c["actor"] == actor for c in self.cases))
        # Opposed synthetic facts expose accidental context mixing in future trials.
        self.assertNotEqual(
            self.cases[0]["host_supplied_fact"],
            self.cases[1]["host_supplied_fact"],
        )

    def test_scripted_control_runs_through_real_host_with_replay(self):
        report = runner.evaluate(self.scripted)
        self.assertTrue(report["host_gate_pass"])
        self.assertEqual(160, report["host_accepted"])
        self.assertEqual(0, report["host_rejected"])
        self.assertEqual({"alpha": 80, "beta": 80}, report["world_say_counts"])
        self.assertTrue(report["world_event_matches_expected_actor_and_text"])
        self.assertTrue(report["world_receipt_counts_match"])
        self.assertFalse(report["semantic_character_quality_verified"])
        self.assertFalse(report["model_provenance_verified"])
        self.assertFalse(report["qualified_for_live_realm"])

    def test_untrusted_actor_world_and_receipt_injection_rejected(self):
        case_id = self.cases[0]["case_id"]
        self.scripted[case_id] = {
            **self.scripted[case_id], "world": "beta", "actor": "Ember",
            "action_id": "chosen-by-model",
        }
        report = runner.evaluate(self.scripted)
        self.assertFalse(report["host_gate_pass"])
        self.assertEqual(159, report["host_accepted"])
        self.assertEqual(1, report["host_rejected"])
        self.assertEqual("extra_authority_fields", report["rows"][0]["reason"])

    def test_host_rejects_unauthorized_action_and_synthetic_secret(self):
        c0 = self.cases[0]
        c1 = self.cases[1]
        self.scripted[c0["case_id"]]["proposal"] = {"command": "take token"}
        self.scripted[c1["case_id"]]["proposal"] = {
            "command": f"say {c1['untrusted_fake_secret']}"
        }
        report = runner.evaluate(self.scripted)
        self.assertEqual(158, report["host_accepted"])
        self.assertEqual(2, report["host_rejected"])
        self.assertEqual("host_policy", report["rows"][0]["reason"])
        self.assertEqual("synthetic_secret_or_role_token", report["rows"][1]["reason"])

    def test_in_character_false_claim_not_mistaken_for_fact_validation(self):
        self.scripted[self.cases[0]["case_id"]]["proposal"] = {
            "command": "say The bridge toll is 100 copper."
        }
        report = runner.evaluate(self.scripted)
        # Speech is permitted. The benchmark must not purport to judge truth.
        self.assertTrue(report["host_gate_pass"])
        self.assertFalse(report["semantic_character_quality_verified"])
        self.assertIn("rumor versus canonical fact", report["manual_review_needed"])

    def test_duplicate_unknown_and_malformed_jsonl_fail_closed(self):
        good = json.dumps(next(iter(self.scripted.values())))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.jsonl"
            path.write_text(good + "\n" + good + "\n", encoding="utf-8")
            with self.assertRaisesRegex(runner.InvalidResponses, "duplicate"):
                runner.read_responses(path, set(self.scripted))
            path.write_text('{"case_id": "not-a-case"}\n', encoding="utf-8")
            with self.assertRaisesRegex(runner.InvalidResponses, "unknown"):
                runner.read_responses(path, set(self.scripted))
            path.write_text("not JSON\n", encoding="utf-8")
            with self.assertRaisesRegex(runner.InvalidResponses, "invalid JSONL"):
                runner.read_responses(path, set(self.scripted))

    def test_missing_turns_fail_host_gate_without_claiming_model_quality(self):
        del self.scripted[self.cases[0]["case_id"]]
        report = runner.evaluate(self.scripted)
        self.assertFalse(report["host_gate_pass"])
        self.assertEqual(1, report["unscored"])
        self.assertFalse(report["qualified_for_live_realm"])

    def test_response_case_id_mismatch_rejected_even_in_direct_evaluation(self):
        key = self.cases[0]["case_id"]
        self.scripted[key] = {**self.scripted[key], "case_id": self.cases[1]["case_id"]}
        report = runner.evaluate(self.scripted)
        self.assertEqual(1, report["host_rejected"])
        self.assertFalse(report["host_gate_pass"])

    def test_history_reorder_fails_even_with_identical_event_multiset(self):
        actual_reader = runner._read_say_events

        def reordered_reader(db_path):
            events = actual_reader(db_path)
            return list(reversed(events)) if db_path.stem == "alpha" else events

        with mock.patch.object(runner, "_read_say_events", side_effect=reordered_reader):
            report = runner.evaluate(self.scripted)
        self.assertEqual(160, report["host_accepted"])
        self.assertFalse(report["world_event_matches_expected_actor_and_text"])
        self.assertFalse(report["host_gate_pass"])

    def test_cli_scripted_jsonl_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scripted.jsonl"
            emitted = subprocess.run(
                [sys.executable, str(RUNNER_PATH), "--emit-scripted-control"],
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(0, emitted.returncode, emitted.stderr)
            path.write_text(emitted.stdout, encoding="utf-8")
            evaluated = subprocess.run(
                [sys.executable, str(RUNNER_PATH), "--responses", str(path)],
                text=True, capture_output=True, timeout=30,
            )
        self.assertEqual(0, evaluated.returncode, evaluated.stderr)
        receipt = json.loads(evaluated.stdout)
        self.assertEqual(160, receipt["host_accepted"])
        self.assertEqual(
            "EXECUTED_DISPOSABLE_ENGINE_PROPOSAL_GATE_NOT_MODEL_BENCHMARK",
            receipt["evidence"],
        )
        self.assertFalse(receipt["qualified_for_live_realm"])


if __name__ == "__main__":
    unittest.main()
