"""Review-only conflict regressions reproduced against Creator Draft #129."""
import hashlib
import json
import unittest

from realm.creator_jsonl_bridge import preview_jsonl_chunks


SYNTHETIC_SOURCE = b"Ship: Lantern\n"
SOURCE_SHA = hashlib.sha256(SYNTHETIC_SOURCE).hexdigest()


def chunk(*, revision, parent=None, name="Lantern", ordinal=1):
    text = "Ship: Lantern"
    source = {
        "id": "synthetic-only", "revision": revision,
        "raw_sha256": SOURCE_SHA, "raw_bytes": len(SYNTHETIC_SOURCE),
        "custody_status": "HASH_MATCH_REPORTED",
    }
    if parent is not None:
        source["parent_revision"] = parent
    return {
        "schema": "realm.creator.provenance_chunk.v1",
        "source": source,
        "candidate": {"category": "ship", "name": name, "status": "PROPOSED"},
        "evidence": {
            "unit": "line", "ordinal": ordinal, "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_status": "SOURCE_CONFIRMED",
        },
        "conflicts": [],
    }


def preview(*rows):
    encoded = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    return preview_jsonl_chunks(encoded)


class ConflictPreservationTests(unittest.TestCase):
    def test_reciprocal_reported_revision_parents_are_flagged(self):
        review = preview(
            chunk(revision="r1", parent="r2"),
            chunk(revision="r2", parent="r1", ordinal=2),
        )
        conflicts = [c for c in review["conflicts"]
                     if c["kind"] == "REVISION_LINEAGE_CYCLE_REVIEW"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["source_id"], "synthetic-only")
        self.assertEqual(set(conflicts[0]["revisions"][:-1]), {"r1", "r2"})
        self.assertEqual(conflicts[0]["revisions"][0], conflicts[0]["revisions"][-1])
        self.assertEqual(review["candidates"][0]["status"], "PROPOSED")
        self.assertEqual(review["boundaries"]["canon"], "UNAPPROVED")

    def test_three_revision_cycle_only_flagged_once(self):
        review = preview(
            chunk(revision="r1", parent="r2"),
            chunk(revision="r2", parent="r3", ordinal=2),
            chunk(revision="r3", parent="r1", ordinal=3),
        )
        self.assertEqual(sum(c["kind"] == "REVISION_LINEAGE_CYCLE_REVIEW"
                             for c in review["conflicts"]), 1)

    def test_acyclic_parent_with_external_revision_not_falsely_flagged(self):
        review = preview(
            chunk(revision="r1", parent="r0"),
            chunk(revision="r2", parent="r1", ordinal=2),
        )
        self.assertFalse(any(c["kind"] == "REVISION_LINEAGE_CYCLE_REVIEW"
                             for c in review["conflicts"]))

    def test_outer_whitespace_candidate_alias_is_flagged(self):
        review = preview(
            chunk(revision="r1", name="Lantern"),
            chunk(revision="r1", name=" Lantern ", ordinal=2),
        )
        self.assertEqual(len(review["candidates"]), 1)
        self.assertEqual(review["candidates"][0]["name"], "Lantern")
        self.assertEqual(len(review["candidates"][0]["evidence"]), 2)
        aliases = [c for c in review["conflicts"]
                   if c["kind"] == "NAME_ALIAS_COLLISION_REVIEW"]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["names"], ["Lantern", " Lantern "])
        self.assertEqual(review["effect"], "REVIEW_ONLY_NO_IMPORT")


if __name__ == "__main__":
    unittest.main()
