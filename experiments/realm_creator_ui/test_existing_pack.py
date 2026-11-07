"""Synthetic-only preservation tests; no file/DB access or unpublished fiction."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

from experiments.realm_creator_ui.existing_pack import preview_existing_worldpack
from realm.worldpack import WorldpackError, validate_worldpack


PACK = {
    "schema": "realm.worldpack.v1",
    "world_id": "synthetic_preservation",
    "name": "Synthetic Preservation",
    "start_room": "dock_01",
    "rooms": [
        {"id": "dock_01", "name": "Dock", "description": "An empty dock.",
         "exits": {"east": "hall_2"}},
        {"id": "hall_2", "name": "Hall", "description": "An empty hall.",
         "exits": {"west": "dock_01"}},
    ],
    "items": [{"id": "lamp_07", "name": "Lamp",
               "description": "A synthetic light.", "room": "dock_01"}],
}


def _raw(pack: dict = PACK) -> bytes:
    return json.dumps(pack, indent=2, ensure_ascii=False).encode("utf-8")


class ExistingWorldpackPreviewTests(unittest.TestCase):
    def test_preserves_authored_ids_exits_and_exact_semantic_hash(self):
        raw = _raw()
        result = preview_existing_worldpack(raw, {"source_id": "fixture_pack"})
        self.assertEqual(result["worldpack"], validate_worldpack(PACK))
        self.assertEqual([room["id"] for room in result["worldpack"]["rooms"]],
                         ["dock_01", "hall_2"])
        self.assertEqual(result["worldpack"]["rooms"][0]["exits"], {"east": "hall_2"})
        self.assertEqual(result["worldpack"]["items"][0]["id"], "lamp_07")
        canonical = json.dumps(validate_worldpack(PACK), sort_keys=True,
                               ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(result["semantic_sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(result["provenance"]["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["provenance"]["source_status"], "UNVERIFIED")
        self.assertFalse(result["provenance"]["authenticated"])
        self.assertEqual(result["canon_status"], "UNREVIEWED")
        self.assertEqual(result["review_status"], "REVIEW_REQUIRED")
        self.assertFalse(result["approval_authenticated"])
        self.assertFalse(result["world_imported"])
        self.assertFalse(result["worldpack_generated"])

    def test_validator_rejects_duplicate_ids_and_dangling_exits(self):
        duplicate = copy.deepcopy(PACK)
        duplicate["rooms"][1]["id"] = "dock_01"
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(_raw(duplicate), {"source_id": "fixture_pack"})
        duplicate_item = copy.deepcopy(PACK)
        duplicate_item["items"].append(copy.deepcopy(duplicate_item["items"][0]))
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(_raw(duplicate_item), {"source_id": "fixture_pack"})
        dangling = copy.deepcopy(PACK)
        dangling["rooms"][0]["exits"]["east"] = "missing"
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(_raw(dangling), {"source_id": "fixture_pack"})

    def test_rejects_oversized_json_and_duplicate_object_keys(self):
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(b" " * (524288 + 1), {"source_id": "fixture_pack"})
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(b'{"schema":"one","schema":"two"}',
                                       {"source_id": "fixture_pack"})
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(b"not json", {"source_id": "fixture_pack"})
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(b"[" + b"9" * 6000 + b"]",
                                       {"source_id": "fixture_pack"})

    def test_untrusted_provenance_cannot_claim_approval_or_lie_about_digest(self):
        raw = _raw()
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(raw, {"source_id": "fixture_pack",
                                             "canon_status": "APPROVED"})
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(raw, {"source_id": "fixture_pack",
                                             "source_sha256": "0" * 64})
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(raw, {"source_id": "../private/path"})
        with self.assertRaises(WorldpackError):
            preview_existing_worldpack(raw, {"source_id": "fixture_pack",
                                             "authenticated": True})


if __name__ == "__main__":
    unittest.main()
