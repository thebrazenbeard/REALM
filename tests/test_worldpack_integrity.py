"""Same-hash import must not certify a partly damaged bound world.

All destructive SQL is limited to disposable temporary databases. These are
external-tamper/corruption fixtures, not a claim of an ordinary crash exploit.
"""

from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.worldpack import WorldpackError, import_worldpack


PACK = {
    "schema": "realm.worldpack.v1",
    "world_id": "boundary-grove",
    "name": "Boundary Grove",
    "start_room": "shore",
    "rooms": [
        {"id": "shore", "name": "Stone Shore", "description": "A bay beside the wood.",
         "exits": {"north": "grove"}},
        {"id": "grove", "name": "Old Grove", "description": "The branches bend inward.",
         "exits": {"south": "shore"}},
    ],
    "items": [
        {"id": "pebble", "name": "River pebble", "description": "A worn gray stone.",
         "room": "shore"},
    ],
}


class BoundWorldIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "world.sqlite3"
        self.pack_path = self.root / "pack.json"
        self.pack_path.write_text(json.dumps(PACK), encoding="utf-8")
        import_worldpack(self.path, self.pack_path)

    def _external_damage(self, statement, params=()):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(statement, params)

    def _expect_rejected_without_repair(self, statement, params=()):
        self._external_damage(statement, params)
        with sqlite3.connect(self.path) as db:
            before = list(db.iterdump())
        with self.assertRaisesRegex(WorldpackError, "Bound world integrity"):
            import_worldpack(self.path, self.pack_path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_missing_only_one_room_is_not_mistaken_for_healthy_unchanged_world(self):
        self._expect_rejected_without_repair("DELETE FROM rooms WHERE id='grove'")

    def test_missing_exit_is_not_mistaken_for_healthy_unchanged_world(self):
        self._expect_rejected_without_repair(
            "DELETE FROM exits WHERE room_id='shore' AND direction='north'"
        )

    def test_modified_room_description_is_detected(self):
        self._expect_rejected_without_repair(
            "UPDATE rooms SET description='tampered' WHERE id='grove'"
        )

    def test_missing_or_modified_authored_item_is_detected(self):
        self._expect_rejected_without_repair("DELETE FROM items WHERE id='pebble'")

    def test_bound_metadata_mismatch_is_rejected_even_with_same_hash(self):
        self._expect_rejected_without_repair(
            "UPDATE world_meta SET value='other-world' WHERE key='world_id'"
        )

    def test_orphaned_player_room_detected_after_foreign_keys_were_disabled(self):
        Engine(self.path).join("Traveler")
        self._expect_rejected_without_repair(
            "UPDATE players SET room_id='phantom' WHERE id='Traveler'"
        )

    def test_item_cannot_be_both_held_and_in_room_after_check_bypass(self):
        Engine(self.path).join("Traveler")
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE items SET held_by='Traveler' WHERE id='pebble'")
            db.execute("PRAGMA ignore_check_constraints=OFF")
            self.assertEqual([], list(db.execute("PRAGMA foreign_key_check")))
            self.assertIn("CHECK constraint failed", db.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0])
        with self.assertRaisesRegex(WorldpackError, "item.*placement"):
            import_worldpack(self.path, self.pack_path)

    def test_item_cannot_be_nowhere_after_check_bypass(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE items SET room_id=NULL WHERE id='pebble'")
        with self.assertRaisesRegex(WorldpackError, "item.*placement"):
            import_worldpack(self.path, self.pack_path)

    def test_item_held_after_play_is_still_legitimate_unchanged_import(self):
        game = Engine(self.path)
        game.join("Traveler")
        self.assertIn("You take River pebble", game.act("Traveler", "take pebble"))
        self.assertTrue(import_worldpack(self.path, self.pack_path)["unchanged"])
        self.assertIn("River pebble", Engine(self.path).act("Traveler", "inventory"))
        game.act("Traveler", "north")
        self.assertIn("You drop River pebble", game.act("Traveler", "drop pebble"))
        self.assertTrue(import_worldpack(self.path, self.pack_path)["unchanged"])


if __name__ == "__main__":
    unittest.main()
