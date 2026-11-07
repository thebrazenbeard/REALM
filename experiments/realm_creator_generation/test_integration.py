"""Synthetic Creator generation -> reviewed compilation -> isolated import contract.

The upload is fictional. JSON acknowledgement is not authenticated approval, and
this test imports only into a disposable database, never a live REALM world.
"""

import json
import tempfile
import unittest
from pathlib import Path

from experiments.realm_creator import compile_approved_worldpack, prepare_draft
from experiments.realm_creator_generation import propose_region
from realm.worldpack import WorldpackError, import_worldpack, read_worldpack


SYNTHETIC_ATLAS = (
    b"# Synthetic Atlas\nThis is test data, not canon.\n"
    b"## Location: Copper Harbor\nOld copper rails line the dock.\n"
    b"## Location: Glasswood\nSilvery trees stand in the mist.\n"
)


def sample_pipeline():
    sources = {"synthetic-atlas.md": SYNTHETIC_ATLAS}
    draft = prepare_draft("creator_synthetic", "Creator Synthetic Test", sources)
    region = propose_region(draft, sources)
    # This is deliberately authored input, simulating a separate *review* step.
    # It is not a transformation that the generator can approve for itself.
    descriptions = {
        "copper_harbor": "A reviewed description of a copper-sided port.",
        "glasswood": "A reviewed description of a grove of silver trees.",
    }
    approval = {
        "schema": "realm.creator.approval.v1",
        "draft_sha256": draft["sha256"],
        "world_id": draft["world_id"],
        "name": draft["name"],
        "start_room": region["start_room_candidate"]["id"],
        "rooms": [{
            "id": room["id"],
            "name": room["name"],
            "description": descriptions[room["id"]],
            "exits": {direction: candidate["to"] for direction, candidate in room["exits"].items()},
            "source_proposal_ids": [room["name_evidence"]["proposal_id"]],
        } for room in region["rooms"]],
        "items": [],
        "acknowledged_review": True,
    }
    compiled = compile_approved_worldpack(draft, approval)
    return draft, region, compiled


class CreatorIntegrationTests(unittest.TestCase):
    def test_synthetic_pipeline_does_not_promote_proposal_to_authority(self):
        draft, region, compiled = sample_pipeline()
        self.assertEqual(region["source_draft_sha256"], draft["sha256"])
        self.assertIsNone(region["worldpack"])
        self.assertFalse(region["live_world_modified"])
        self.assertFalse(region["approval_authenticated"])
        self.assertEqual(region["topology_evidence"], "NOT_SOURCE_ESTABLISHED")
        self.assertEqual(compiled["worldpack"]["world_id"], "creator_synthetic")
        self.assertEqual(compiled["worldpack"]["start_room"], "copper_harbor")
        self.assertEqual(compiled["worldpack"]["rooms"][0]["exits"], {"east": "glasswood"})
        self.assertEqual(compiled["lineage"]["source_link_status"], "UNVERIFIED_CITATION")
        self.assertEqual(compiled["lineage"]["semantic_relation"], "NOT_ENTAILMENT")
        self.assertFalse(compiled["approval_authenticated"])
        self.assertFalse(compiled["world_imported"])

    def test_reviewed_worldpack_imports_only_to_disposable_pristine_db(self):
        _, region, compiled = sample_pipeline()
        with tempfile.TemporaryDirectory(prefix="realm_creator_synthetic_") as td:
            db_path = Path(td) / "new-world.sqlite"
            pack_path = Path(td) / "worldpack.json"
            pack_path.write_text(json.dumps(compiled["worldpack"]), encoding="utf-8")
            checked, digest = read_worldpack(pack_path)
            self.assertEqual(checked, compiled["worldpack"])
            self.assertEqual(digest, compiled["worldpack_sha256"])
            self.assertFalse(db_path.exists())
            receipt = import_worldpack(db_path, pack_path)
            self.assertEqual(receipt["world_id"], region["world_id"])
            self.assertEqual(receipt["sha256"], digest)
            self.assertFalse(receipt["unchanged"])
            self.assertTrue(db_path.exists())
            # A successful importer receipt cannot retroactively promote
            # the generator or the unauthenticated JSON review.
            self.assertFalse(compiled["world_imported"])
            self.assertFalse(compiled["approval_authenticated"])
            retry = import_worldpack(db_path, pack_path)
            self.assertTrue(retry["unchanged"])
            changed = dict(checked, name="A Different Synthetic World")
            pack_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(WorldpackError):
                import_worldpack(db_path, pack_path)


if __name__ == "__main__":
    unittest.main()
