"""Hostile checks for draft PR stack-currentness release HOLD."""
import copy
import json
import unittest
from pathlib import Path

from research.pr_stack_gate import assess, validate


class PRStackGateTest(unittest.TestCase):
    def setUp(self):
        a, b, c, d = (letter * 40 for letter in "abcd")
        self.manifest = {"schema": "realm.pr_stack_gate.v1",
                         "release_authority": "HOLD_NOT_GRANTED", "prs": [
            {"n": 61, "head_ref": "feature/parent", "head_sha": b,
             "base_ref": "main", "base_sha": a,
             "release": "DRAFT_UNMERGED_HOLD"},
            {"n": 62, "head_ref": "feature/child", "head_sha": d,
             "base_ref": "feature/parent", "base_sha": c,
             "release": "DRAFT_UNMERGED_HOLD"}]}
        self.tips = {"main": a, "feature/parent": b, "feature/child": d}

    def test_stale_creator_parent_is_not_mergeability_pass(self):
        report = assess(self.manifest, self.tips,
                        lambda parent, child: parent != "b"*40 or child != "d"*40)
        self.assertEqual(report["branch_gate"], "HOLD")
        self.assertIn("BASE_MOVED_REVIEW_STALE", report["results"][1]["findings"])
        self.assertIn("CURRENT_PARENT_NOT_ANCESTOR", report["results"][1]["findings"])
        self.assertEqual(report["release"], "HOLD_NO_OWNER_AUTHORITY")

    def test_parent_ancestor_cannot_erase_moved_review_subject(self):
        report = assess(self.manifest, self.tips, lambda *_: True)
        self.assertEqual(report["branch_gate"], "HOLD")
        self.assertEqual(report["results"][1]["findings"],
                         ["BASE_MOVED_REVIEW_STALE"])

    def test_exact_graph_pass_still_does_not_authorize_release(self):
        self.manifest["prs"][1]["base_sha"] = self.tips["feature/parent"]
        report = assess(self.manifest, self.tips, lambda *_: True)
        self.assertEqual(report["branch_gate"], "PASS")
        self.assertEqual(report["release"], "HOLD_NO_OWNER_AUTHORITY")

    def test_branch_tip_moves_after_review(self):
        self.tips["feature/child"] = "f"*40
        report = assess(self.manifest, self.tips, lambda *_: True)
        self.assertIn("HEAD_MOVED_REVIEW_STALE", report["results"][1]["findings"])

    def test_absent_branch_never_passes(self):
        del self.tips["feature/child"]
        report = assess(self.manifest, self.tips, lambda *_: True)
        self.assertIn("BRANCH_NOT_OBSERVED", report["results"][1]["findings"])

    def test_ancestor_probe_error_never_becomes_pass(self):
        def inaccessible(*_):
            raise OSError("object unavailable")
        report = assess(self.manifest, self.tips, inaccessible)
        self.assertIn("ANCESTRY_UNVERIFIED", report["results"][0]["findings"])

    def test_manifest_cannot_self_authorize(self):
        bad = copy.deepcopy(self.manifest)
        bad["release_authority"] = "APPROVED"
        with self.assertRaises(ValueError):
            validate(bad)
        bad = copy.deepcopy(self.manifest)
        bad["prs"][0]["release"] = "MERGED"
        with self.assertRaises(ValueError):
            validate(bad)

    def test_duplicate_and_injection_fail_closed(self):
        bad = copy.deepcopy(self.manifest)
        bad["prs"][1]["n"] = 61
        with self.assertRaises(ValueError):
            validate(bad)
        bad = copy.deepcopy(self.manifest)
        bad["prs"][1]["head_ref"] = "--upload-pack=sh"
        with self.assertRaises(ValueError):
            validate(bad)

    def test_real_manifest_marks_creator_suspect(self):
        path = Path(__file__).resolve().parents[1] / "research" / "pr_stack_20261107.json"
        real = json.loads(path.read_text(encoding="utf-8"))
        validate(real)
        by_n = {r["n"]: r for r in real["prs"]}
        self.assertNotEqual(by_n[61]["head_sha"], by_n[62]["base_sha"])
        self.assertEqual(by_n[91]["base_sha"], by_n[88]["head_sha"])


if __name__ == "__main__":
    unittest.main()
