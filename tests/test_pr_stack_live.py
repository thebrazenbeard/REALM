"""Hostile tests: live GitHub must bind real source, not a cached manifest."""
import copy
import unittest
from unittest.mock import patch

from research.pr_stack_live import (canonical_origin, assess_live,
                                    validate_live_pr, verify_origin)


class PRStackLiveTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = "a" * 40, "b" * 40
        self.snapshot = {"schema": "realm.pr_stack_gate.v1",
                         "release_authority": "HOLD_NOT_GRANTED",
                         "prs": [{"n": 61, "head_ref": "feature/parent",
                                  "head_sha": self.a, "base_ref": "main",
                                  "base_sha": self.b,
                                  "release": "DRAFT_UNMERGED_HOLD"}]}
        self.live = {61: {"number": 61, "state": "open", "draft": True,
                          "merged_at": None, "head": {"ref": "feature/parent",
                          "sha": self.a, "repo": {"full_name": "thebrazenbeard/REALM"}},
                          "base": {"ref": "main", "sha": self.b,
                          "repo": {"full_name": "thebrazenbeard/REALM"}}}}
    def test_origin_exact_https_only(self):
        self.assertTrue(canonical_origin("https://github.com/thebrazenbeard/REALM.git"))
        for attack in ("git@github.com:thebrazenbeard/REALM.git",
                       "https://evil.example/thebrazenbeard/REALM.git",
                       "https://github.com/thebrazenbeard/REALM.git?token=secret",
                       "https://username@github.com/thebrazenbeard/REALM.git",
                       "https://github.com/thebrazenbeard/OTHER.git",
                       "https://github.com/thebrazenbeard/REALM.git/extra"):
            with self.subTest(attack=attack):
                self.assertFalse(canonical_origin(attack))

    def test_live_exact_head_and_draft(self):
        result = assess_live(self.snapshot, self.live)
        self.assertEqual(result["pr_gate"], "PASS")
        self.assertEqual(result["release"], "HOLD_NO_OWNER_AUTHORITY")
        bad = copy.deepcopy(self.live)
        bad[61]["head"]["sha"] = "f" * 40
        self.assertIn("LIVE_HEAD_MOVED", assess_live(self.snapshot, bad)["findings"])
        bad = copy.deepcopy(self.live)
        bad[61]["draft"] = False
        self.assertIn("NOT_OPEN_DRAFT", assess_live(self.snapshot, bad)["findings"])
    def test_missing_extra_and_foreign_repo_fail_closed(self):
        self.assertIn("LIVE_PR_MISSING:61", assess_live(self.snapshot, {})["findings"])
        bad = copy.deepcopy(self.live)
        bad[92] = copy.deepcopy(bad[61])
        bad[92]["number"] = 92
        self.assertIn("UNTRACKED_OPEN_PR:92", assess_live(self.snapshot, bad)["findings"])
        bad = copy.deepcopy(self.live)
        bad[61]["head"]["repo"]["full_name"] = "evil/REALM"
        self.assertIn("FOREIGN_PR_REPO:61", assess_live(self.snapshot, bad)["findings"])

    def test_moved_base_and_ref_fail_closed(self):
        bad = copy.deepcopy(self.live)
        bad[61]["base"]["sha"] = "f" * 40
        self.assertIn("LIVE_BASE_MOVED:61", assess_live(self.snapshot, bad)["findings"])
        bad = copy.deepcopy(self.live)
        bad[61]["head"]["ref"] = "other"
        self.assertIn("LIVE_HEAD_REF_MOVED:61", assess_live(self.snapshot, bad)["findings"])

    def test_broken_live_schema_rejected(self):
        with self.assertRaises(ValueError):
            validate_live_pr({"number": 61, "head": {}, "base": {}})
    def test_closed_or_merged_never_passes(self):
        for key, value in (("state", "closed"), ("merged_at", "2026-11-07T00:00:00Z")):
            bad = copy.deepcopy(self.live)
            bad[61][key] = value
            self.assertIn("NOT_OPEN_DRAFT", assess_live(self.snapshot, bad)["findings"])

    def test_old_unrelated_pr_outside_bounded_manifest(self):
        bad = copy.deepcopy(self.live)
        bad[40] = copy.deepcopy(bad[61])
        bad[40]["number"] = 40
        self.assertEqual(assess_live(self.snapshot, bad)["pr_gate"], "PASS")

    def test_verified_origin_blocks_fake_or_unreachable(self):
        with patch("research.pr_stack_live.git", return_value=(0, "https://evil.invalid/fake.git", "")):
            with self.assertRaises(RuntimeError):
                verify_origin("unused")
        with patch("research.pr_stack_live.git", return_value=(2, "", "origin missing")):
            with self.assertRaises(RuntimeError):
                verify_origin("unused")


if __name__ == "__main__":
    unittest.main()
