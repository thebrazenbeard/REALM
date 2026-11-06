"""Offline negative and positive controls for tiny NPC evaluation triage."""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCORER_PATH = ROOT / "evals" / "tiny_npc" / "score.py"
CORPUS_PATH = ROOT / "evals" / "tiny_npc" / "cases.v1.json"
spec = importlib.util.spec_from_file_location("tiny_npc_offline_scorer", SCORER_PATH)
scorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorer)


class TinyNPCEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = scorer.load_cases(CORPUS_PATH)
        cls.cases = {item["id"]: item for item in cls.corpus["cases"]}

    @staticmethod
    def receipt(case, text, **override):
        row = {
            "case_id": case["id"], "model_id": case["model_id"],
            "logical_prompt_sha256": hashlib.sha256(case["prompt"].encode()).hexdigest(),
            "rendered_prompt_sha256": "f" * 64,  # Fixture, not a real rendered prompt receipt.
            "artifact_sha256": "a" * 64,  # Synthetic fixture only.
            "source_head": "b" * 40,  # Synthetic fixture only.
            "backend": "fixture-only", "backend_version": "test-1",
            "latency_ms": 150, "stop_type": "word", "stopping_word": ".",
            "text": text,
        }
        row.update(override)
        return row

    def test_corpus_original_names_rights_and_both_candidate_tracks(self):
        self.assertEqual(12, len(self.cases))
        self.assertEqual(12, len({c["id"] for c in self.corpus["cases"]}))
        self.assertEqual(
            {"cloudunity/stealth-rifle", "walter-bd/npc-voice-soup06"},
            {c["model_id"] for c in self.corpus["cases"]},
        )
        self.assertTrue(all(c.get("manual_review") for c in self.corpus["cases"]))
        self.assertTrue(all(not source["revision_pinned"] for source in self.corpus["source_cards"]))
        self.assertNotIn("World of Darkness", json.dumps(self.corpus["cases"]))

    def test_valid_fact_retention_is_lexically_passed_but_not_qualified(self):
        case = self.cases["soup_price_preservation"]
        row = scorer.assess(case, self.receipt(case, "That coil costs 17 copper."))
        self.assertEqual("PASS_LEXICAL", row["result"])
        self.assertIn("price_semantics", row["manual_review"])

    def test_wrong_fact_and_unauthorized_discounts_fail_closed(self):
        case = self.cases["soup_price_preservation"]
        row = scorer.assess(case, self.receipt(case, "The rope costs 15 copper with a discount."))
        self.assertEqual("FAIL_LEXICAL", row["result"])
        self.assertIn("missing_required_fact:17 copper", row["violations"])
        self.assertIn("forbidden_text:discount", row["violations"])

    def test_model_stop_and_artifact_and_prompt_binding(self):
        case = self.cases["stealth_steward_persona"]
        row = scorer.assess(case, self.receipt(
            case, "I will wait for the river to settle.", stop_type="limit",
            artifact_sha256="b" * 64, logical_prompt_sha256="0" * 64,
            rendered_prompt_sha256="bad", source_head="bad",
        ), expected_sha256="a" * 64)
        self.assertEqual("FAIL_LEXICAL", row["result"])
        self.assertIn("generation_did_not_complete", row["violations"])
        self.assertIn("artifact_sha256_mismatch", row["violations"])
        self.assertIn("logical_prompt_sha256_mismatch", row["violations"])
        self.assertIn("rendered_prompt_sha256_missing_or_invalid", row["violations"])
        self.assertIn("source_head_missing_or_invalid", row["violations"])

    def test_osc52_terminal_control_is_not_a_valid_character_line(self):
        case = self.cases["stealth_steward_persona"]
        row = scorer.assess(case, self.receipt(case, "\x1b]52;c;Zm9v\x07I will wait for the river."))
        self.assertEqual("FAIL_LEXICAL", row["result"])
        self.assertIn("nonprintable_or_terminal_control", row["violations"])

    def test_template_leak_narration_and_private_marker_controls(self):
        steward = self.cases["stealth_steward_persona"]
        narration = scorer.assess(steward, self.receipt(steward, "The traveler nodded."))
        self.assertIn("forbidden_text:The traveler nodded", narration["violations"])
        secret = self.cases["stealth_private_note_injection"]
        leaked = scorer.assess(secret, self.receipt(secret, "PLAYER_TOKEN_FAKE_619."))
        self.assertIn("forbidden_text:PLAYER_TOKEN_FAKE_619", leaked["violations"])
        templated = scorer.assess(steward, self.receipt(steward, "<think>I will wait.</think>"))
        self.assertIn("model_control_tokens", templated["violations"])

    def test_duplicate_unknown_or_invalid_response_rejected(self):
        case = self.cases["stealth_steward_persona"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.jsonl"
            good = json.dumps(self.receipt(case, "I will wait for the river."))
            path.write_text(good + "\n" + good + "\n")
            with self.assertRaisesRegex(scorer.EvalInputError, "Duplicate"):
                scorer.load_responses(path, known_ids=set(self.cases))
            path.write_text(json.dumps({"case_id": "unknown_case"}) + "\n")
            with self.assertRaisesRegex(scorer.EvalInputError, "Unknown"):
                scorer.load_responses(path, known_ids=set(self.cases))
            path.write_text("not json\n")
            with self.assertRaisesRegex(scorer.EvalInputError, "Invalid receipt"):
                scorer.load_responses(path, known_ids=set(self.cases))
            path.write_text(json.dumps({"case_id": ["not", "hashable"]}) + "\n")
            with self.assertRaisesRegex(scorer.EvalInputError, "Unknown"):
                scorer.load_responses(path, known_ids=set(self.cases))

    def test_missing_receipts_and_reported_latency_are_not_verified_runtime(self):
        case = self.cases["soup_price_preservation"]
        receipts = {case["id"]: self.receipt(case, "The price is 17 copper.")}
        report = scorer.score(self.corpus, receipts)
        self.assertEqual(1, report["lexical_pass"])
        self.assertEqual(11, report["unscored"])
        self.assertFalse(report["qualified_for_live_realm"])
        self.assertEqual("unverified_external_receipt_not_host_measurement",
                         report["reported_latency_ms"]["source"])
        self.assertEqual(150, report["reported_latency_ms"]["p50"])

    def test_semantic_paraphrase_pass_is_not_character_fidelity(self):
        # An explicit negative-control *bypass*: lexical pass is NOT a semantic
        # truth or roleplay verdict; this reply lacks the requested grounding.
        case = self.cases["stealth_steward_persona"]
        row = scorer.assess(case, self.receipt(case, "That is certainly possible."))
        self.assertEqual("PASS_LEXICAL", row["result"])
        self.assertIn("grounded_reply", row["manual_review"])
        for drift in ("I am a new vampire mayor.", "The traveler lowered his head in agreement."):
            self.assertEqual("PASS_LEXICAL", scorer.assess(case, self.receipt(case, drift))["result"])

    def test_input_size_and_schema_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps({"schema": "unknown", "cases": []}))
            with self.assertRaisesRegex(scorer.EvalInputError, "schema"):
                scorer.load_cases(path)
            malformed = {**self.corpus, "cases": [{**self.corpus["cases"][0], "id": 17}]}
            path.write_text(json.dumps(malformed))
            with self.assertRaisesRegex(scorer.EvalInputError, "case ID"):
                scorer.load_cases(path)
            path.write_bytes(b"x" * (scorer.MAX_FILE_BYTES + 1))
            with self.assertRaisesRegex(scorer.EvalInputError, "1 MB"):
                scorer.load_cases(path)

    def test_cli_partial_corpus_exits_nonzero_with_valid_json_report(self):
        case = self.cases["soup_price_preservation"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.jsonl"
            path.write_text(json.dumps(self.receipt(case, "The price is 17 copper.")) + "\n")
            run = subprocess.run(
                [sys.executable, str(SCORER_PATH), "--cases", str(CORPUS_PATH),
                 "--responses", str(path)],
                check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=10,
            )
        self.assertEqual(1, run.returncode)
        self.assertEqual(11, json.loads(run.stdout)["unscored"])
        self.assertEqual("", run.stderr)

    def test_single_model_sha_selection_does_not_score_other_model_as_missing(self):
        case = self.cases["soup_price_preservation"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.jsonl"
            path.write_text(json.dumps(self.receipt(case, "The price is 17 copper.")) + "\n")
            args = [sys.executable, str(SCORER_PATH), "--responses", str(path),
                    "--expected-sha256", "a" * 64]
            missing_selector = subprocess.run(args, capture_output=True, text=True, timeout=10)
            selected = subprocess.run(
                args + ["--model-id", "walter-bd/npc-voice-soup06"],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(2, missing_selector.returncode)
        self.assertIn("requires --model-id", missing_selector.stderr)
        self.assertEqual(1, selected.returncode)  # Five other Soup06 cases unscored.
        report = json.loads(selected.stdout)
        self.assertEqual(6, report["total_cases"])
        self.assertEqual(5, report["unscored"])


if __name__ == "__main__":
    unittest.main()
