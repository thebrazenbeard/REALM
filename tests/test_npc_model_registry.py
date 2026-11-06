"""Offline research-manifest checks; model-card data is not inference evidence."""

import json
import unittest
from pathlib import Path


REGISTRY = Path(__file__).resolve().parents[1] / "research" / "npc-model-candidates.v1.json"


class NPCModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))

    def test_registry_preserves_research_and_host_check_evidence_types(self):
        self.assertEqual("realm.npc-model-candidates.v1", self.registry["schema"])
        self.assertEqual("source_research_plus_two_bounded_local_host_checks", self.registry["registry_status"])
        self.assertIs(self.registry["weights_in_repository"], False)
        self.assertEqual("per_candidate_as_observed_on_Lappy_2026-11-06", self.registry["weights_status_scope"])
        self.assertEqual(
            "research_triage_order_not_measured_quality", self.registry["priority_semantics"]
        )
        self.assertEqual("performed_for_two_host_tested_candidates_only", self.registry["model_card_revision_pinning"])
        self.assertEqual("performed_for_two_host_tested_candidates_only", self.registry["model_artifact_sha256_verification"])
        searches = self.registry["discovery_queries"]
        self.assertEqual(2, len(searches))
        self.assertEqual({"character", "npc"}, {
            item.split("search=")[-1] for item in searches
        })
        self.assertTrue(all(item.startswith("https://huggingface.co/models?") for item in searches))

    def test_candidates_are_exact_hf_cards_without_bundled_weights_or_fake_tests(self):
        candidates = self.registry["model_candidates"]
        self.assertGreaterEqual(len(candidates), 5)
        names = [model["repo_id"] for model in candidates]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(sorted(model["priority"] for model in candidates), list(range(1, len(candidates) + 1)))
        for model in candidates:
            with self.subTest(model=model["repo_id"]):
                self.assertRegex(model["repo_id"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
                self.assertEqual("https://huggingface.co/" + model["repo_id"], model["model_card_url"])
                self.assertTrue(0 < model["approx_parameters_millions"] <= 1000)
                self.assertEqual("model_card_reviewed", model["evidence"])
                if model["repo_id"] in {"walter-bd/npc-voice-soup06", "cloudunity/stealth-rifle"}:
                    self.assertEqual("downloaded_outside_git_on_Lappy_sha256_verified", model["weights_status"])
                    self.assertEqual("one_bounded_cpu_inference_observed_not_integrated_or_qualified", model["host_benchmark_status"])
                    observed = model["local_host_experiment"]
                    self.assertEqual("Lappy", observed["host"])
                    self.assertRegex(observed["source_revision"], r"^[0-9a-f]{40}$")
                    self.assertRegex(observed["artifact_sha256"], r"^[0-9a-f]{64}$")
                    self.assertGreater(observed["artifact_bytes"], 100_000_000)
                    self.assertNotIn("integration_qualified", observed["status"])
                else:
                    self.assertEqual("not_downloaded", model["weights_status"])
                    self.assertEqual("not_run", model["host_benchmark_status"])
                    self.assertNotIn("local_host_experiment", model)
                self.assertIs(model["qualified_for_real_players"], False)
                self.assertEqual("training_source_review_required", model["rights_status"])
                self.assertTrue(model["roles"])
                self.assertTrue(model["limitations"])


if __name__ == "__main__":
    unittest.main()
