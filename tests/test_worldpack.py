"""Worldpack importer: safety, durability, and gameplay smoke tests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm import Engine
from realm.worldpack import WorldpackError, import_worldpack, read_worldpack, validate_worldpack


def example():
    return {
        "schema": "realm.worldpack.v1",
        "world_id": "sample",
        "name": "Sample World",
        "start_room": "shore",
        "rooms": [
            {"id": "shore", "name": "Quiet Shore", "description": "A field beside calm water.",
             "exits": {"north": "grove"}},
            {"id": "grove", "name": "Grove", "description": "A cluster of trees.",
             "exits": {"south": "shore"}},
        ],
        "items": [
            {"id": "stone_1", "name": "Small stone", "description": "A smooth fieldstone.",
             "room": "shore"},
        ],
    }


class WorldpackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / "world.sqlite3"
        self.file = self.base / "sample.json"

    def save(self, data=None):
        self.file.write_text(json.dumps(example() if data is None else data), encoding="utf-8")

    def test_import_starts_player_in_worldpack_start_room(self):
        self.save()
        result = import_worldpack(self.db, self.file)
        self.assertFalse(result["unchanged"])
        self.assertEqual(64, len(result["sha256"]))
        world = Engine(self.db)
        self.assertIn("Quiet Shore", world.join("Patrick"))
        self.assertIn("Small stone", world.act("Patrick", "look"))
        self.assertIn("Grove", world.act("Patrick", "north"))
        self.assertIn("Quiet Shore", world.act("Patrick", "south"))

    def test_same_import_is_idempotent_after_real_play(self):
        self.save()
        first = import_worldpack(self.db, self.file)
        world = Engine(self.db)
        world.join("Patrick")
        world.act("Patrick", "take stone_1")
        again = import_worldpack(self.db, self.file)
        self.assertTrue(again["unchanged"])
        self.assertEqual(first["sha256"], again["sha256"])
        self.assertIn("Small stone", Engine(self.db).act("Patrick", "inventory"))

    def test_different_import_never_replaces_live_world(self):
        self.save()
        import_worldpack(self.db, self.file)
        changed = example()
        changed["rooms"][0]["description"] = "Changed world."
        self.save(changed)
        with self.assertRaisesRegex(WorldpackError, "different worldpack"):
            import_worldpack(self.db, self.file)
        self.assertIn("A field beside calm water.", Engine(self.db).join("Patrick"))

    def test_used_demo_world_cannot_be_replaced(self):
        Engine(self.db).join("Patrick")
        self.save()
        with self.assertRaisesRegex(WorldpackError, "pristine"):
            import_worldpack(self.db, self.file)
        self.assertIn("DEMO WORLD", Engine(self.db).act("Patrick", "look"))

    def test_external_changes_to_unplayed_demo_prevent_import(self):
        Engine(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE rooms SET description='altered' WHERE id='atrium'")
        self.save()
        with self.assertRaisesRegex(WorldpackError, "differs"):
            import_worldpack(self.db, self.file)

    def test_missing_start_or_destination_rejected_without_mutation(self):
        bad = example()
        bad["rooms"][0]["exits"]["north"] = "missing"
        self.save(bad)
        with self.assertRaisesRegex(WorldpackError, "Dangling"):
            import_worldpack(self.db, self.file)
        self.assertFalse(self.db.exists())

    def test_duplicate_and_unknown_fields_rejected(self):
        bad = example()
        bad["rooms"].append(dict(bad["rooms"][0]))
        with self.assertRaisesRegex(WorldpackError, "Duplicate"):
            validate_worldpack(bad)
        bad = example()
        bad["can_execute_code"] = True
        with self.assertRaisesRegex(WorldpackError, "extra"):
            validate_worldpack(bad)

    def test_multiline_descriptions_allowed_but_terminal_controls_and_surrogates_rejected(self):
        valid = example()
        valid["rooms"][0]["description"] = "Line one.\nLine two."
        self.assertEqual(valid["rooms"][0]["description"],
                         validate_worldpack(valid)["rooms"][0]["description"])
        for payload in ("Name \u001b[31m", "Invalid \ud800"):
            bad = example()
            bad["rooms"][0]["description"] = payload
            with self.subTest(payload=repr(payload)), self.assertRaises(WorldpackError):
                validate_worldpack(bad)

    def test_invalid_utf8_or_oversized_rejected_before_engine_opens(self):
        self.file.write_bytes(b"\xff")
        with self.assertRaisesRegex(WorldpackError, "UTF-8"):
            read_worldpack(self.file)
        self.file.write_bytes(b"x" * (512 * 1024 + 1))
        with self.assertRaisesRegex(WorldpackError, "exceeds"):
            import_worldpack(self.db, self.file)
        self.assertFalse(self.db.exists())

    def test_retry_hash_ignores_formatting_and_whitespace(self):
        self.save()
        first = import_worldpack(self.db, self.file)
        self.file.write_text(json.dumps(example(), indent=4), encoding="utf-8")
        again = import_worldpack(self.db, self.file)
        self.assertEqual(first["sha256"], again["sha256"])
        self.assertTrue(again["unchanged"])

    def test_concurrent_same_worldpack_import_does_not_corrupt(self):
        self.save()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: import_worldpack(self.db, self.file), range(2)))
        self.assertEqual([False, True], sorted(result["unchanged"] for result in results))
        self.assertIn("Quiet Shore", Engine(self.db).join("Patrick"))


if __name__ == "__main__":
    unittest.main()
