"""Provenance/review separation tests; no world DB or model touched."""

from __future__ import annotations

import unittest
import hashlib
import json

from experiments.realm_creator import CreatorDraftError, compile_approved_worldpack, prepare_draft


SOURCE = b"# Harbor\nA ship arrives.\n\n## Dock\nCopper rails, cold air.\n"


def rehash(draft):
    unsigned = {key: value for key, value in draft.items() if key != "sha256"}
    draft["sha256"] = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def approval_for(draft):
    first, second = draft["proposals"]
    return {
        "schema": "realm.creator.approval.v1", "draft_sha256": draft["sha256"],
        "world_id": draft["world_id"], "name": draft["name"], "start_room": "harbor",
        "rooms": [
            {"id": "harbor", "name": "Harbor", "description": "A ship arrives.",
             "exits": {"east": "dock"}, "source_proposal_ids": [first["proposal_id"]]},
            {"id": "dock", "name": "Dock", "description": "Copper rails, cold air.",
             "exits": {"west": "harbor"}, "source_proposal_ids": [second["proposal_id"]]},
        ],
        "items": [], "acknowledged_review": True,
    }


class DraftContractTests(unittest.TestCase):
    def test_deterministic_provenance_no_canon_promotion(self):
        one = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        two = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        self.assertEqual(one, two)
        self.assertEqual(one["status"], "REVIEW_REQUIRED")
        self.assertIsNone(one["worldpack"])
        self.assertFalse(one["live_world_modified"])
        self.assertEqual([p["heading"] for p in one["proposals"]], ["Harbor", "Dock"])
        self.assertTrue(all(p["status"] == "UNREVIEWED" and p["room_candidate"] is False
                            for p in one["proposals"]))
        self.assertEqual(one["proposals"][0]["provenance"]["start_line"], 1)
        self.assertEqual(one["proposals"][1]["provenance"]["start_line"], 4)

    def test_compiled_worldpack_is_validated_and_unimported(self):
        draft = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        compiled = compile_approved_worldpack(draft, approval_for(draft))
        self.assertEqual(compiled["worldpack"]["world_id"], "driftspace_draft")
        self.assertEqual(compiled["worldpack"]["rooms"][0]["exits"]["east"], "dock")
        self.assertFalse(compiled["world_imported"])
        self.assertFalse(compiled["approval_authenticated"])
        self.assertTrue(compiled["requires_separate_host_import"])
        self.assertEqual(compiled["lineage"]["rooms"]["harbor"],
                         [draft["proposals"][0]["proposal_id"]])
        self.assertEqual(compiled["lineage"]["source_link_status"], "UNVERIFIED_CITATION")
        self.assertEqual(compiled["lineage"]["semantic_relation"], "NOT_ENTAILMENT")

    def test_tampering_a_draft_is_rejected(self):
        draft = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        approved = approval_for(draft)
        draft["proposals"][0]["excerpt"] = "Forged canon"
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, approved)

    def test_rehashed_malformed_draft_structure_rejected_with_typed_error(self):
        base = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        cases = []
        malformed = json.loads(json.dumps(base))
        malformed["proposals"] = "bad"
        cases.append(malformed)
        malformed = json.loads(json.dumps(base))
        malformed["proposals"] = [{"proposal_id": "x"}]
        cases.append(malformed)
        malformed = json.loads(json.dumps(base))
        malformed["sources"] = {"unexpected": "object"}
        cases.append(malformed)
        malformed = json.loads(json.dumps(base))
        malformed["proposals"][0]["provenance"]["start_line"] = "first"
        cases.append(malformed)
        malformed = json.loads(json.dumps(base))
        malformed["proposals"][0]["provenance"]["source_sha256"] = "0" * 64
        cases.append(malformed)
        malformed = json.loads(json.dumps(base))
        malformed["proposals"][0]["status"] = "APPROVED"
        cases.append(malformed)
        malformed = json.loads(json.dumps(base))
        malformed["proposals"][0]["room_candidate"] = True
        cases.append(malformed)
        for case in cases:
            with self.subTest(case=str(case.get("proposals"))[:25]):
                rehash(case)
                with self.assertRaises(CreatorDraftError):
                    compile_approved_worldpack(case, approval_for(base))

    def test_changed_source_revision_cannot_reuse_approval(self):
        original = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        changed = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE + b"New fact\n"})
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(changed, approval_for(original))

    def test_review_is_explicit_and_room_sources_are_checked(self):
        draft = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        blank = approval_for(draft)
        blank["acknowledged_review"] = False
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, blank)
        unknown = approval_for(draft)
        unknown["rooms"][0]["source_proposal_ids"] = ["src-forged"]
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, unknown)
        no_sources = approval_for(draft)
        no_sources["rooms"][0]["source_proposal_ids"] = []
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, no_sources)
        bad_type = approval_for(draft)
        bad_type["rooms"][0]["source_proposal_ids"] = [{"forged": "id"}]
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, bad_type)

    def test_invalid_graph_fails_existing_engine_validator(self):
        draft = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        approved = approval_for(draft)
        approved["rooms"][0]["exits"]["east"] = "missing"
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, approved)

    def test_filename_and_type_boundaries(self):
        for name in ("../private.md", "/root/secret.md", "credentials.zip", "a.py", "subdir/a.md"):
            with self.subTest(name=name), self.assertRaises(CreatorDraftError):
                prepare_draft("draft", "Draft", {name: SOURCE})
        for content in (b"", b"\x00oops", b"\xff", b"  \n", b"x" * (256 * 1024 + 1)):
            with self.subTest(content=content[:16]), self.assertRaises(CreatorDraftError):
                prepare_draft("draft", "Draft", {"notes.md": content})
        for invisible in ("\u202e", "\u2066", "\u200d", "\u0085", "\u2028"):
            with self.subTest(invisible=repr(invisible)), self.assertRaises(CreatorDraftError):
                prepare_draft("draft", "Draft", {"notes.md": f"# Starport{invisible}\nThe dock".encode("utf-8")})

    def test_caps_are_fail_closed(self):
        with self.assertRaises(CreatorDraftError):
            prepare_draft("draft", "Draft", {f"file{i}.txt": b"text" for i in range(9)})
        oversized_sections = "\n".join(f"# Section {i}\nFact" for i in range(65)).encode("utf-8")
        with self.assertRaises(CreatorDraftError):
            prepare_draft("draft", "Draft", {"notes.md": oversized_sections})
        long_excerpt = prepare_draft("draft", "Draft", {"notes.md": b"# Place\n" + b"A" * 900})
        self.assertTrue(long_excerpt["proposals"][0]["excerpt_truncated"])
        self.assertEqual(len(long_excerpt["proposals"][0]["excerpt"]), 600)

    def test_same_bytes_in_two_files_retain_distinct_source_ids(self):
        draft = prepare_draft("draft", "Draft", {"one.md": SOURCE, "two.md": SOURCE})
        ids = [proposal["proposal_id"] for proposal in draft["proposals"]]
        self.assertEqual(len(ids), len(set(ids)))
        with self.assertRaises(CreatorDraftError):
            prepare_draft("draft", "Draft", {123: SOURCE})

    def test_cross_world_approval_is_rejected(self):
        draft = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        approved = approval_for(draft)
        approved["world_id"] = "anotherworld"
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, approved)

    def test_no_copy_of_unapproved_source_into_worldpack(self):
        source = b"# Private Notes\nSecret plot twist; never publish.\n"
        draft = prepare_draft("draft", "Draft", {"private.txt": source})
        self.assertIn("Secret plot", draft["proposals"][0]["excerpt"])
        approved = {
            "schema": "realm.creator.approval.v1", "draft_sha256": draft["sha256"],
            "world_id": "draft", "name": "Draft", "start_room": "entry",
            "rooms": [{"id": "entry", "name": "Arrival", "description": "Unrelated public-safe description",
                       "exits": {}, "source_proposal_ids": [draft["proposals"][0]["proposal_id"]]}],
            "items": [], "acknowledged_review": True,
        }
        compiled = compile_approved_worldpack(draft, approved)
        self.assertNotIn("Secret plot", str(compiled))

    def test_items_need_source_provenance_too(self):
        draft = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": SOURCE})
        approved = approval_for(draft)
        approved["items"] = [{"id": "token", "name": "Token", "description": "Brass token",
                              "room": "dock", "source_proposal_ids": [draft["proposals"][1]["proposal_id"]]}]
        compiled = compile_approved_worldpack(draft, approved)
        self.assertEqual(compiled["lineage"]["items"]["token"],
                         [draft["proposals"][1]["proposal_id"]])
        approved["items"][0]["source_proposal_ids"] = []
        with self.assertRaises(CreatorDraftError):
            compile_approved_worldpack(draft, approved)

    def test_unrelated_source_link_is_not_semantic_entailment(self):
        draft = prepare_draft("driftspace_draft", "Driftspace draft", {"notes.md": b"# Orchard\nApples grow here.\n"})
        proposal_id = draft["proposals"][0]["proposal_id"]
        approved = {"schema": "realm.creator.approval.v1", "draft_sha256": draft["sha256"],
                    "world_id": "driftspace_draft", "name": "Driftspace draft",
                    "start_room": "false_starport", "acknowledged_review": True,
                    "rooms": [{"id": "false_starport", "name": "False Starport",
                               "description": "Ships depart for the stars.", "exits": {},
                               "source_proposal_ids": [proposal_id]}], "items": []}
        compiled = compile_approved_worldpack(draft, approved)
        # This is a *syntactically* approved candidate, not source-supported truth.
        self.assertEqual(compiled["lineage"]["source_link_status"], "UNVERIFIED_CITATION")
        self.assertEqual(compiled["lineage"]["semantic_relation"], "NOT_ENTAILMENT")
        self.assertEqual(compiled["worldpack"]["rooms"][0]["name"], "False Starport")


if __name__ == "__main__":
    unittest.main()
