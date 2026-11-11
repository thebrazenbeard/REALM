"""Siltglass Reach showcase integrity and two-client persistence tests.

These tests import passive, original worldpack data into temporary databases.
They never contact a server, import a private world, or mutate an existing game.
"""

from collections import deque
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm import Engine
from realm.worldpack import import_worldpack, read_worldpack


PACK_PATH = (
    Path(__file__).resolve().parents[1]
    / "worlds" / "flagship" / "siltglass-reach" / "worldpack.v1.json"
)
OPPOSITES = {"north": "south", "south": "north", "east": "west", "west": "east"}


class FlagshipWorldpackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack, cls.digest = read_worldpack(PACK_PATH)
        cls.rooms = {room["id"]: room for room in cls.pack["rooms"]}

    def test_shape_and_original_content_density(self):
        self.assertEqual(self.pack["schema"], "realm.worldpack.v1")
        self.assertEqual(self.pack["world_id"], "siltglass-reach")
        self.assertEqual(self.pack["start_room"], "tidegate")
        self.assertEqual(24, len(self.rooms))
        self.assertEqual(14, len(self.pack["items"]))
        self.assertEqual(64, len(self.digest))
        for room in self.rooms.values():
            with self.subTest(room=room["id"]):
                self.assertGreaterEqual(len(room["description"]), 130)
                self.assertGreaterEqual(len(room["exits"]), 2)
                self.assertLessEqual(len(room["exits"]), 12)
        for item in self.pack["items"]:
            with self.subTest(item=item["id"]):
                self.assertGreaterEqual(len(item["description"]), 50)

    def test_all_rooms_reachable_with_reciprocal_cardinal_exits(self):
        visited = {"tidegate"}
        queue = deque(["tidegate"])
        while queue:
            room_id = queue.popleft()
            for direction, destination in self.rooms[room_id]["exits"].items():
                self.assertIn(destination, self.rooms)
                if direction in OPPOSITES:
                    reverse = OPPOSITES[direction]
                    self.assertEqual(
                        self.rooms[destination]["exits"].get(reverse), room_id,
                        f"{room_id}.{direction} has no reciprocal {reverse} route",
                    )
                if destination not in visited:
                    visited.add(destination)
                    queue.append(destination)
        self.assertEqual(set(self.rooms), visited)
        self.assertEqual(
            self.rooms["old_spillway"]["exits"]["archway"], "fault_arch"
        )
        self.assertEqual(
            self.rooms["fault_arch"]["exits"]["archway"], "old_spillway"
        )

    def test_two_clients_share_state_but_other_world_is_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            primary = Path(temp) / "primary.sqlite3"
            separate = Path(temp) / "separate.sqlite3"
            original = import_worldpack(primary, PACK_PATH)
            self.assertFalse(original["unchanged"])

            first = Engine(primary)
            second = Engine(primary)
            self.assertIn("The Tidegate", first.join("Cedar"))
            self.assertIn("The Tidegate", second.join("Juniper"))
            self.assertIn("The Knotted Bell", first.act("Cedar", "south"))
            self.assertIn("The Knotted Bell", second.act("Juniper", "south"))
            self.assertIn("You take River chime", first.act("Cedar", "take river_chime"))
            self.assertEqual("That item is not here.", second.act("Juniper", "take river_chime"))
            first.act("Cedar", "say The crossing record disagrees.")
            self.assertIn(
                "Cedar said: The crossing record disagrees.",
                second.act("Juniper", "listen"),
            )

            restarted = Engine(primary)
            self.assertIn("Welcome back, Cedar.", restarted.join("Cedar"))
            self.assertIn("The Knotted Bell", restarted.act("Cedar", "look"))
            self.assertIn("River chime", restarted.act("Cedar", "inventory"))
            retry = import_worldpack(primary, PACK_PATH)
            self.assertTrue(retry["unchanged"])
            self.assertEqual(original["sha256"], retry["sha256"])

            self.assertFalse(import_worldpack(separate, PACK_PATH)["unchanged"])
            another = Engine(separate)
            self.assertIn("The Tidegate", another.join("Cedar"))
            self.assertIn("The Knotted Bell", another.act("Cedar", "south"))
            self.assertIn("River chime", another.act("Cedar", "look"))
            self.assertEqual(
                "Players who have joined this world: Cedar",
                another.act("Cedar", "who"),
            )


if __name__ == "__main__":
    unittest.main()
