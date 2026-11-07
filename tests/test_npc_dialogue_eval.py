"""Model-free acceptance checks for the unpublished NPC evaluation protocol."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NPCDialogueEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.seed = self.root / "seed.bin"
        self.seed.write_bytes(bytes(range(32)))
        self.public = self.root / "prompts.jsonl"
        self.private = self.root / "rubric.jsonl"
        self.responses = self.root / "responses.jsonl"

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", "-m", "research.npc_dialogue_eval", *map(str, args)],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )

    def _generate(self):
        result = self._cli("generate", "--seed-file", self.seed,
                           "--public", self.public, "--private", self.private,
                           "--count", "4")
        self.assertEqual(0, result.returncode, result.stderr)
        prompts = [json.loads(x) for x in self.public.read_text().splitlines()]
        rubrics = [json.loads(x) for x in self.private.read_text().splitlines()]
        return prompts, rubrics

    def _score(self, rubrics, make_utterance):
        self.responses.write_text("".join(
            json.dumps({"case_id": rubric["case_id"],
                        "utterance": make_utterance(rubric)}) + "\n"
            for rubric in rubrics
        ))
        result = self._cli("score", "--private", self.private,
                           "--responses", self.responses, "--seed-file", self.seed,
                           "--expected-count", "4")
        return result, json.loads(result.stdout)

    def test_generation_keeps_scoring_rubric_separate_and_reproducible(self):
        public, rubric = self._generate()
        self.assertEqual(4, len(public))
        self.assertEqual({x["case_id"] for x in public},
                         {x["case_id"] for x in rubric})
        self.assertEqual(4, len({x["required_literal"] for x in rubric}))
        for prompt, criterion in zip(public, rubric):
            self.assertIn(criterion["required_literal"], prompt["prompt"])
            self.assertNotIn("required_literal", prompt)
            self.assertNotIn("forbidden_literal", prompt)
        original = self.public.read_bytes(), self.private.read_bytes()
        self._generate()
        self.assertEqual(original, (self.public.read_bytes(), self.private.read_bytes()))

    def test_four_distinct_persona_roles_receive_the_same_fact_protocol(self):
        prompts, rubrics = self._generate()
        self.assertEqual({"dock_keeper", "archivist", "watch_guard", "stonemason"},
                         {prompt["persona"] for prompt in prompts})
        for prompt, rubric in zip(prompts, rubrics):
            self.assertIn(rubric["required_literal"], prompt["prompt"])
            self.assertIn(prompt["persona"].replace("_", " "), prompt["prompt"])

    def test_exact_checks_pass_without_pretending_semantic_qualification(self):
        _, rubric = self._generate()
        result, data = self._score(
            rubric, lambda c: "Certainly. The answer is " + c["required_literal"] + ".",
        )
        self.assertEqual(3, result.returncode, result.stderr)
        self.assertEqual(4, data["mechanically_clear_cases"])
        self.assertEqual("PENDING_HUMAN_SEMANTIC_REVIEW", data["semantic_fidelity"])
        self.assertEqual("UNQUALIFIED_REVIEW_REQUIRED", data["disposition"])
        self.assertFalse(data["model_qualified"])

    def test_private_canary_and_multiline_output_are_rejected(self):
        _, rubric = self._generate()
        result, data = self._score(
            rubric, lambda c: c["required_literal"] + " " + c["forbidden_literal"],
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual(4, data["flagged_cases"])
        self.assertTrue(all("forbidden_literal" in case["findings"]
                            for case in data["cases"]))
        result, data = self._score(rubric,
                                   lambda c: c["required_literal"] + "\nsecond line")
        self.assertEqual(1, result.returncode)
        self.assertTrue(all("invalid_speech_shape" in case["findings"]
                            for case in data["cases"]))

    def test_obvious_negation_is_flagged_despite_matching_literal(self):
        _, rubric = self._generate()
        result, data = self._score(
            rubric, lambda c: "No, it is NOT " + c["required_literal"] + ".",
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual(4, data["flagged_cases"])
        self.assertTrue(all("apparent_fact_negation" in case["findings"]
                            for case in data["cases"]))
        self.assertFalse(data["model_qualified"])

    def test_forged_action_narration_is_flagged_even_with_correct_price(self):
        _, rubric = self._generate()
        result, data = self._score(rubric, lambda c:
            "I already moved your character and changed inventory; ticket: " +
            c["required_literal"])
        self.assertEqual(1, result.returncode)
        self.assertTrue(all("unsupported_action_claim" in case["findings"]
                            for case in data["cases"]))

    def test_fullwidth_hyphen_canary_is_flagged(self):
        _, rubric = self._generate()
        result, data = self._score(rubric, lambda c:
            c["required_literal"] + " " + c["forbidden_literal"].replace("-", "\uff0d"))
        self.assertEqual(1, result.returncode)
        self.assertTrue(all("forbidden_literal" in case["findings"]
                            for case in data["cases"]))

    def test_semantic_paraphrase_bypass_remains_unqualified(self):
        _, rubric = self._generate()
        result, data = self._score(rubric, lambda c:
            "Your pack is lighter now, by my hand. Ticket price: " + c["required_literal"])
        # This deliberately escapes narrow lexical action tripwires. A clean
        # heuristic is never a successful model/semantic evaluation.
        self.assertEqual(3, result.returncode)
        self.assertEqual(4, data["mechanically_clear_cases"])
        self.assertEqual("UNQUALIFIED_REVIEW_REQUIRED", data["disposition"])
        self.assertFalse(data["model_qualified"])

    def test_missing_or_duplicate_cases_fail_closed(self):
        _, rubric = self._generate()
        self.responses.write_text(json.dumps({
            "case_id": rubric[0]["case_id"], "utterance": rubric[0]["required_literal"],
        }) + "\n")
        missing = self._cli("score", "--private", self.private,
                            "--responses", self.responses, "--seed-file", self.seed,
                            "--expected-count", "4")
        self.assertEqual(2, missing.returncode)
        self.assertIn("case coverage", missing.stderr)

        self.responses.write_text("".join(
            json.dumps({"case_id": rubric[0]["case_id"],
                        "utterance": rubric[0]["required_literal"]}) + "\n"
            for _ in range(4)))
        duplicate = self._cli("score", "--private", self.private,
                              "--responses", self.responses, "--seed-file", self.seed,
                              "--expected-count", "4")
        self.assertEqual(2, duplicate.returncode)
        self.assertIn("duplicate", duplicate.stderr)

    def test_tampered_empty_fact_rubric_cannot_issue_a_pass(self):
        _, rubric = self._generate()
        rubric[0]["required_literal"] = ""
        self.private.write_text("".join(json.dumps(row) + "\n" for row in rubric))
        self.responses.write_text("".join(
            json.dumps({"case_id": row["case_id"], "utterance": "nothing to say"}) + "\n"
            for row in rubric
        ))
        result = self._cli("score", "--private", self.private,
                           "--responses", self.responses, "--seed-file", self.seed,
                           "--expected-count", "4")
        self.assertEqual(2, result.returncode)
        self.assertIn("rubric", result.stderr)

    def test_forged_case_and_rubric_are_not_considered_authenticated(self):
        _, rubric = self._generate()
        rubric[0]["case_id"] = "holdout-forged"
        self.private.write_text("".join(json.dumps(row) + "\n" for row in rubric))
        self.responses.write_text("".join(
            json.dumps({"case_id": row["case_id"],
                        "utterance": row["required_literal"]}) + "\n"
            for row in rubric
        ))
        result = self._cli("score", "--private", self.private,
                           "--responses", self.responses, "--seed-file", self.seed,
                           "--expected-count", "4")
        self.assertEqual(2, result.returncode)
        self.assertIn("rubric", result.stderr)


if __name__ == "__main__":
    unittest.main()
