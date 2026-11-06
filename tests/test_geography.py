"""Stdlib-only deterministic tests for REALM geography/navigation."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.geography import Geography, MapUnavailable


class GeographyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "realm.sqlite3"
        self.geo = Geography(self.path)
        self.geo.add_place("universe", "Test universe", "universe")
        self.geo.add_place("sol", "Home system", "system", "universe", x=3, y=2)
        self.geo.add_place("nearby", "Nearby system", "system", "universe", x=6, y=2)
        self.geo.add_place("earth", "Homeworld", "planet", "sol", x=1, y=1)
        self.geo.add_place("moon", "Moon", "planet", "sol", x=2, y=1)
        self.geo.add_place("alien", "Far planet", "planet", "nearby", x=2, y=3)
        self.geo.add_place("coast", "Coastal region", "region", "earth", x=1, y=0)
        self.geo.add_place("town", "Test town", "settlement", "coast", x=1, y=1)
        self.geo.add_place("shore", "Shoreline", "room", "town", x=0, y=0)
        self.geo.add_place("forest", "Forest", "room", "town", x=1, y=0)

    def test_hierarchy_validation(self):
        with self.assertRaisesRegex(ValueError, "cannot be a child"):
            self.geo.add_place("bad", "Bad planet", "planet", "coast")
        with self.assertRaisesRegex(ValueError, "Map positions"):
            self.geo.add_place("bad", "Bad", "room", "town", x=0)
        with self.assertRaisesRegex(ValueError, "control"):
            self.geo.add_place("bad", "Bad\x00name", "room", "town")

    def test_map_snapshot_is_bounded_and_contains_terrain(self):
        self.geo.set_map("town", [".^~", ".==", "#o*"])
        snapshot = self.geo.snapshot("town", viewport=3, player_room_id="shore")
        self.assertEqual({"x": 0, "y": 0}, snapshot["origin"])
        self.assertEqual(9, len(snapshot["tiles"]))
        self.assertEqual("forest", snapshot["tiles"][1]["terrain"])
        self.assertEqual("water", snapshot["tiles"][2]["terrain"])
        self.assertFalse(snapshot["tiles"][2]["passable"])
        self.assertEqual({"x": 0, "y": 0}, snapshot["player"])
        self.assertEqual(["forest", "shore"], sorted(m["id"] for m in snapshot["markers"]))

    def test_map_never_shrinks_below_existing_coordinates(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.geo.set_map("town", ["."])
        self.geo.set_map("town", ["..", ".."])
        with self.assertRaisesRegex(ValueError, "outside"):
            self.geo.add_place("further", "Further room", "room", "town", x=5, y=0)
        with self.assertRaisesRegex(ValueError, "rectangular"):
            self.geo.set_map("earth", ["^^", "^"])

    def test_nested_player_marker_resolves_to_direct_map_child(self):
        self.geo.set_map("earth", ["...", "...", "..."])
        snap = self.geo.snapshot("earth", player_room_id="forest")
        self.assertEqual({"x": 1, "y": 0}, snap["player"])
        self.assertEqual("coast", snap["markers"][0]["id"])

    def test_snapshot_for_room_finds_closest_mapped_ancestor(self):
        with self.assertRaises(MapUnavailable):
            self.geo.snapshot_for_room("forest")
        self.geo.set_map("earth", ["...", "...", "..."])
        self.assertEqual("earth", self.geo.snapshot_for_room("forest")["space"]["id"])
        self.geo.set_map("town", [".^~", "...", "..."])
        self.assertEqual("town", self.geo.snapshot_for_room("forest")["space"]["id"])
        self.assertEqual({"x": 1, "y": 0}, self.geo.snapshot_for_room("forest")["player"])

    def test_map_viewport_crops_without_leaking_outside_marker(self):
        self.geo.set_map("universe", ["." * 20] * 20)
        snap = self.geo.snapshot("universe", center_x=6, center_y=2, viewport=4)
        self.assertEqual({"x": 4, "y": 0}, snap["origin"])
        self.assertEqual(["nearby"], [m["id"] for m in snap["markers"]])
        with self.assertRaisesRegex(ValueError, "Viewport"):
            self.geo.snapshot("universe", viewport=1000)

    def test_route_search_uses_declared_weighted_edges_only(self):
        self.geo.link("earth", "moon", "ship", seconds=50)
        self.geo.link("moon", "alien", "ship", seconds=20)
        self.geo.link("earth", "alien", "ship", seconds=100)
        self.assertEqual(["earth", "moon", "alien"], self.geo.route("earth", "alien", "ship")["places"])
        self.assertEqual(70, self.geo.route("earth", "alien", "ship")["seconds"])
        self.assertIsNone(self.geo.route("alien", "earth", "ship"))
        with self.assertRaisesRegex(ValueError, "endpoints"):
            self.geo.link("shore", "earth", "ship")
        with self.assertRaisesRegex(ValueError, "cross planets"):
            self.geo.add_place("alien_region", "Alien area", "region", "alien")
            self.geo.add_place("alien_room", "Alien room", "room", "alien_region")
            self.geo.link("shore", "alien_room", "walk")

    def test_direct_ship_departure_arrival_persists_and_does_not_tick_itself(self):
        self.geo.link("earth", "moon", "ship", seconds=50)
        self.geo.register_ship("wanderer", "Wanderer", "Patrick", "earth")
        with self.assertRaises(PermissionError):
            self.geo.depart("wanderer", "moon", actor="Stranger", now=100)
        departed = self.geo.depart("wanderer", "moon", actor="Patrick", now=100, action_id="leave-1")
        self.assertEqual(150, departed["arrival_at"])
        self.assertEqual(departed, Geography(self.path).depart(
            "wanderer", "moon", actor="Patrick", now=101, action_id="leave-1"))
        with self.assertRaisesRegex(ValueError, "different command"):
            Geography(self.path).depart("wanderer", "alien", actor="Patrick", now=102, action_id="leave-1")
        with self.assertRaisesRegex(ValueError, "already in transit"):
            self.geo.depart("wanderer", "moon", actor="Patrick", now=120)
        with self.assertRaisesRegex(ValueError, "not reached"):
            Geography(self.path).arrive("wanderer", now=149)
        self.assertEqual("in_transit", Geography(self.path).ship_status("wanderer")["state"])
        self.assertEqual("moon", Geography(self.path).arrive("wanderer", now=150, action_id="land-1")["planet"])
        self.assertEqual("moon", Geography(self.path).arrive("wanderer", now=160, action_id="land-1")["planet"])
        self.assertEqual("docked", Geography(self.path).ship_status("wanderer")["state"])
        with sqlite3.connect(self.path) as db:
            kinds = [row[0] for row in db.execute("SELECT kind FROM geo_ship_events ORDER BY id")]
        self.assertEqual(["depart", "arrive"], kinds)

    def test_database_coexists_with_core_engine_tables(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY, name TEXT)")
            db.execute("INSERT INTO rooms VALUES('shore', 'Shoreline')")
        with sqlite3.connect(self.path) as db:
            self.assertEqual("Shoreline", db.execute("SELECT name FROM rooms WHERE id='shore'").fetchone()[0])
        with self.geo._connect() as db:
            self.assertEqual("room", self.geo._place(db, "shore")["kind"])


if __name__ == "__main__":
    unittest.main()
