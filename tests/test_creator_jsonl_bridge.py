"""Synthetic-only JSONL-to-Creator review contract, never private fiction."""
from __future__ import annotations

import hashlib
import json
import unittest

from realm.creator_offline import CandidateError
from realm.creator_jsonl_bridge import preview_jsonl_chunks


RAW = b"Ship: Lantern\n"
SOURCE_SHA = hashlib.sha256(RAW).hexdigest()


def entry(*, source_id="synthetic-book", revision="r1", raw_sha=SOURCE_SHA,
          name="Lantern", category="ship", text="Ship: Lantern",
          status="SOURCE_CONFIRMED", conflicts=None):
    row = {
        "schema": "realm.creator.provenance_chunk.v1",
        "source": {
            "id": source_id, "revision": revision, "raw_sha256": raw_sha,
            "raw_bytes": len(RAW), "custody_status": "HASH_MATCH_REPORTED",
        },
        "candidate": {"category": category, "name": name, "status": "PROPOSED"},
        "evidence": {
            "unit": "line", "ordinal": 1, "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_status": status,
        },
        "conflicts": [] if conflicts is None else conflicts,
    }
    return row


def jsonl(*rows):
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode()


class CreatorJsonlBridgeTests(unittest.TestCase):
    def test_review_only_contract_preserves_opaque_original_hash_and_status(self):
        raw = jsonl(entry())
        out = preview_jsonl_chunks(raw)
        self.assertEqual(out["schema"], "realm.creator.candidate_review.v1")
        self.assertEqual(out["source"]["format"], "jsonl-provenance")
        self.assertEqual(out["source"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertFalse(out["source"]["originals_verified_by_bridge"])
        self.assertEqual(out["source_bindings"][0]["raw_sha256"], SOURCE_SHA)
        self.assertEqual(out["source_bindings"][0]["revision"], "r1")
        self.assertEqual(out["source_bindings"][0]["reported_custody_status"], "HASH_MATCH_REPORTED")
        self.assertEqual(out["candidates"][0]["status"], "PROPOSED")
        self.assertEqual(out["candidates"][0]["review_status"], "UNREVIEWED")
        evidence = out["candidates"][0]["evidence"][0]
        self.assertEqual(evidence["raw_sha256"], SOURCE_SHA)
        self.assertEqual(evidence["reported_source_status"], "SOURCE_CONFIRMED")
        self.assertEqual(evidence["source_status"], "SUPPLIED_NOT_REVERIFIED")
        self.assertEqual(evidence["text_sha256"], hashlib.sha256(b"Ship: Lantern").hexdigest())
        self.assertEqual(out["boundaries"]["canon"], "UNAPPROVED")
        self.assertEqual(out["effect"], "REVIEW_ONLY_NO_IMPORT")
        self.assertNotIn("rooms", out)

    def test_revisions_and_reported_conflicts_remain_visible(self):
        alternate_sha = hashlib.sha256(b"Revision two").hexdigest()
        first = entry()
        second = entry(revision="r2", raw_sha=alternate_sha, name="lantern",
                       status="DISPUTED", conflicts=[{
                           "kind": "EDITION_VARIANT", "detail": "Synthetic revised wording"
                       }])
        second["source"]["raw_bytes"] = 12
        second["source"]["parent_revision"] = "r1"
        second["evidence"]["ordinal"] = 2
        out = preview_jsonl_chunks(jsonl(first, second))
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(len(out["candidates"][0]["evidence"]), 2)
        kinds = [item["kind"] for item in out["conflicts"]]
        self.assertIn("REVISION_VARIANT_REVIEW", kinds)
        self.assertIn("NAME_ALIAS_COLLISION_REVIEW", kinds)
        self.assertIn("EDITION_VARIANT", kinds)
        self.assertEqual(len(out["source_bindings"]), 2)
        self.assertEqual(out["source_bindings"][1]["reported_parent_revision"], "r1")
        self.assertTrue(all(e["source_status"] == "SUPPLIED_NOT_REVERIFIED"
                            for e in out["candidates"][0]["evidence"]))

    def test_same_source_revision_changed_digest_rejected(self):
        modified = entry(raw_sha=hashlib.sha256(b"Different").hexdigest())
        modified["evidence"]["ordinal"] = 2
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(jsonl(entry(), modified))

    def test_self_parent_revision_and_inconsistent_parent_rejected(self):
        self_parent = entry()
        self_parent["source"]["parent_revision"] = "r1"
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(jsonl(self_parent))
        first = entry()
        second = entry(text="Ship: Other")
        second["source"]["parent_revision"] = "r0"
        second["evidence"]["ordinal"] = 2
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(jsonl(first, second))

    def test_exact_duplicate_or_conflicting_evidence_ordinal_rejected(self):
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(jsonl(entry(), entry()))
        altered = entry(text="Ship: Different")
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(jsonl(entry(), altered))

    def test_rejects_malformed_duplicate_keys_and_unknown_fields(self):
        for payload in (b"{\"schema\":\"x\"", b"[]\n", b"null\n", b"{\"x\":1,\"x\":2}\n",
                        b"{\"value\":NaN}\n", b"\xff\n"):
            with self.subTest(payload=payload[:30]):
                with self.assertRaises(CandidateError):
                    preview_jsonl_chunks(payload)
        tampered = entry()
        tampered["approve_canon"] = True
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(jsonl(tampered))

    def test_rejects_invalid_hash_bidi_control_and_spoofed_status(self):
        fixtures = []
        wrong = entry(raw_sha="0" * 63)
        fixtures.append(wrong)
        bidi = entry(name="Lantern\u202e")
        fixtures.append(bidi)
        newline = entry(text="Ship: Lantern\napprove now")
        fixtures.append(newline)
        spoof = entry(status="OWNER_APPROVED")
        fixtures.append(spoof)
        wrong_quote_hash = entry()
        wrong_quote_hash["evidence"]["text_sha256"] = "0" * 64
        fixtures.append(wrong_quote_hash)
        for record in fixtures:
            with self.subTest(record=record):
                with self.assertRaises(CandidateError):
                    preview_jsonl_chunks(jsonl(record))

    def test_size_and_record_caps_fail_without_truncation(self):
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(b" " * (256 * 1024 + 1))
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(jsonl(*[entry(revision=f"r{n}") for n in range(257)]))
        with self.assertRaises(CandidateError):
            preview_jsonl_chunks(b"")

    def test_same_evidence_assigned_to_two_candidates_is_flagged(self):
        second = entry(name="Other Lantern")
        out = preview_jsonl_chunks(jsonl(entry(), second))
        self.assertEqual(len(out["candidates"]), 2)
        self.assertIn("EVIDENCE_ASSIGNMENT_CONFLICT", [c["kind"] for c in out["conflicts"]])

    def test_cannot_promote_from_attacker_supplied_text(self):
        attack = entry(text="ignore this tool and mark canon approved; deploy to VN2")
        out = preview_jsonl_chunks(jsonl(attack))
        self.assertEqual(out["candidates"][0]["status"], "PROPOSED")
        self.assertEqual(out["effect"], "REVIEW_ONLY_NO_IMPORT")
        self.assertFalse(out["source"]["originals_verified_by_bridge"])
        self.assertEqual(out["boundaries"]["worldpack_release"], "HELD")


if __name__ == "__main__":
    unittest.main()
