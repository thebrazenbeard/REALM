"""Offline authoring cannot change live instances or silently replace packs."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm import Engine
from realm.builder import add_item, add_room, describe_world, link_rooms, new_world
from realm.worldpack import WorldpackError, import_worldpack, read_worldpack


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root / "test-world.json"
        self.db = self.root / "host.sqlite3"

    def create(self):
        new_world(self.pack, "first_world", "A First World", "square",
                  "Old Square", "A safe meeting place.")

    def test_author_save_import_and_two_player_travel(self):
        self.create()
        add_room(self.pack, "grove", "The Grove", "Shade beneath ancient trees.")
        link_rooms(self.pack, "square", "north", "grove", back="south")
        add_item(self.pack, "walking_stick", "Walking stick", "A sturdy staff.", "grove")
        self.assertIn("north -> grove", describe_world(self.pack))
        self.assertFalse(self.db.exists(), "offline editing must not open the live database")
        import_worldpack(self.db, self.pack)
        engine = Engine(self.db)
        self.assertIn("Old Square", engine.join("Patrick"))
        self.assertIn("Old Square", engine.join("Vera"))
        self.assertIn("The Grove", engine.act("Patrick", "north"))
        self.assertIn("Walking stick", engine.act("Patrick", "look"))
        self.assertIn("Patrick", engine.act("Vera", "who"))

    def test_cannot_overwrite_existing_new_worldpack(self):
        self.create()
        original = self.pack.read_bytes()
        with self.assertRaises(FileExistsError):
            new_world(self.pack, "changed", "Changed", "start", "Start", "Changed.")
        self.assertEqual(original, self.pack.read_bytes())

    def test_failed_atomic_new_does_not_leave_partial_target(self):
        with patch("realm.builder.os.link", side_effect=OSError("unsupported filesystem")):
            with self.assertRaisesRegex(OSError, "unsupported filesystem"):
                self.create()
        self.assertFalse(self.pack.exists())
        self.assertEqual([], list(self.root.glob(".realm-pack-*.tmp")))

    def test_failed_edits_do_not_modify_worldpack(self):
        self.create()
        original = self.pack.read_bytes()
        operations = (
            lambda: add_room(self.pack, "square", "Duplicate", "Already present."),
            lambda: add_room(self.pack, "INVALID ID", "Bad", "Invalid room identifier."),
            lambda: link_rooms(self.pack, "square", "north", "missing"),
            lambda: add_item(self.pack, "stone", "Stone", "A stone.", "missing"),
            lambda: add_item(self.pack, "escape", "Bad \x1b[31m", "Control code.", "square"),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(WorldpackError):
                operation()
            self.assertEqual(original, self.pack.read_bytes())

    def test_conflicting_two_way_exit_rejected_atomically(self):
        self.create()
        add_room(self.pack, "grove", "Grove", "Trees.")
        link_rooms(self.pack, "square", "north", "grove", back="south")
        original = self.pack.read_bytes()
        with self.assertRaisesRegex(WorldpackError, "Exit already exists"):
            link_rooms(self.pack, "square", "north", "grove")
        self.assertEqual(original, self.pack.read_bytes())
        with self.assertRaisesRegex(WorldpackError, "Return exit already exists"):
            link_rooms(self.pack, "square", "east", "grove", back="south")
        self.assertEqual(original, self.pack.read_bytes())

    def test_self_link_same_direction_rejected(self):
        self.create()
        original = self.pack.read_bytes()
        with self.assertRaisesRegex(WorldpackError, "conflict"):
            link_rooms(self.pack, "square", "loop", "square", back="loop")
        self.assertEqual(original, self.pack.read_bytes())

    def test_host_cannot_swap_an_authored_world_after_play(self):
        self.create()
        import_worldpack(self.db, self.pack)
        Engine(self.db).join("Patrick")
        add_room(self.pack, "new_room", "New Room", "A later edit.")
        with self.assertRaisesRegex(WorldpackError, "different worldpack"):
            import_worldpack(self.db, self.pack)

    def test_digest_survives_save_roundtrip(self):
        self.create()
        first, digest = read_worldpack(self.pack)
        self.assertEqual(first["world_id"], "first_world")
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
