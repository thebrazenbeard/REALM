"""REALM defaults are setting-neutral; world themes require explicit import."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway
from realm.geography import MapUnavailable
from realm.worldpack import import_worldpack, read_worldpack


FANTASY_PACK = Path(__file__).resolve().parents[1] / "examples" / "cedarwatch-world.json"
CRAFTING_TABLES = {"gathering_nodes", "material_pouches", "craft_recipes", "crafted_goods", "built_structures"}


def _tables(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as db:
        return {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


def _world_rows(db_path: Path, table: str) -> list[tuple]:
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute(f"SELECT * FROM main.{table} ORDER BY 1").fetchall()


class SettingNeutralWorldTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def test_gateway_starts_without_any_ship_or_atmosphere_install(self):
        path = self.directory / "fantasy.sqlite3"
        gateway = Gateway(path, world_id="cedarwatch-example")
        initial_tables = _tables(path)
        self.assertFalse(any(t.startswith(("geo_", "atm_", "clock_", "ship_"))
                             for t in initial_tables))
        self.assertTrue(CRAFTING_TABLES.isdisjoint(initial_tables))
        self.assertFalse(any("space station" in str(row).lower() or
                             "quantum slip" in str(row).lower() or
                             "driftspace" in str(row).lower()
                             for row in _world_rows(path, "rooms")))

        installed = import_worldpack(path, FANTASY_PACK)
        self.assertFalse(installed["unchanged"])
        self.assertEqual("cedarwatch-example", installed["world_id"])
        self.assertEqual(initial_tables, _tables(path))
        self.assertIn("Village Green", gateway.join("Rowan")["text"])
        self.assertTrue(CRAFTING_TABLES.isdisjoint(_tables(path)))
        self.assertIn("Woodland Path", gateway.command("Rowan", "north", "walk_once")["text"])
        self.assertIn("Woodland Path", gateway.room("Rowan")["text"])
        self.assertFalse(any(t.startswith("geo_") for t in _tables(path)))

    def test_optional_geography_initialized_only_on_explicit_access(self):
        path = self.directory / "fantasy.sqlite3"
        gateway = Gateway(path, world_id="cedarwatch-example")
        import_worldpack(path, FANTASY_PACK)
        self.assertFalse(any(t.startswith("geo_") for t in _tables(path)))
        with ThreadPoolExecutor(max_workers=6) as pool:
            instances = list(pool.map(lambda _: gateway.geography, range(12)))
        self.assertTrue(all(instance is instances[0] for instance in instances))
        self.assertIn("geo_ships", _tables(path))
        self.assertEqual([], _world_rows(path, "geo_ships"))
        self.assertEqual([], _world_rows(path, "geo_places"))
        self.assertTrue(import_worldpack(path, FANTASY_PACK)["unchanged"])

    def test_simultaneous_first_map_reads_initialize_geography_once(self):
        path = self.directory / "fantasy.sqlite3"
        gateway = Gateway(path, world_id="cedarwatch-example")
        import_worldpack(path, FANTASY_PACK)
        gateway.join("Rowan")
        self.assertFalse(any(t.startswith("geo_") for t in _tables(path)))

        def first_map(_):
            try:
                gateway.map("Rowan")
            except MapUnavailable as error:
                return str(error)
            self.fail("An unconfigured world must not get a fabricated map")

        # The HTTP server dispatches requests on separate threads; all of
        # these calls race the same first lazy geography schema install.
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(first_map, range(36)))
        self.assertEqual(["No map is configured for this location"] * 36, results)
        self.assertIs(gateway.geography, gateway.geography)
        self.assertIn("geo_ships", _tables(path))
        self.assertEqual([], _world_rows(path, "geo_ships"))

    def test_concurrent_first_router_reads_preserve_command_api(self):
        path = self.directory / "fantasy.sqlite3"
        gateway = Gateway(path, world_id="cedarwatch-example")
        import_worldpack(path, FANTASY_PACK)
        gateway.join("Rowan")
        self.assertTrue(CRAFTING_TABLES.isdisjoint(_tables(path)))
        with ThreadPoolExecutor(max_workers=8) as pool:
            routers = list(pool.map(lambda _: gateway.router, range(24)))
        self.assertTrue(all(router is routers[0] for router in routers))
        self.assertTrue(CRAFTING_TABLES.issubset(_tables(path)))
        self.assertIn("Woodland Path", gateway.command("Rowan", "north", "walk")["text"])
        self.assertFalse(any(t.startswith("geo_") for t in _tables(path)))

    def test_fantasy_and_optional_station_worlds_do_not_cross_contaminate(self):
        medieval = self.directory / "medieval.sqlite3"
        station = self.directory / "station.sqlite3"
        fantasy = Gateway(medieval, world_id="cedarwatch-example")
        import_worldpack(medieval, FANTASY_PACK)

        # Deliberately generic synthetic station layout: this is neither
        # Driftspace lore nor an implicit ship or atmosphere ruleset.
        station_pack = {
            "schema": "realm.worldpack.v1", "world_id": "station-example",
            "name": "Optional Test Station", "start_room": "quiet_dock",
            "rooms": [
                {"id": "quiet_dock", "name": "Quiet Dock",
                 "description": "An empty test platform overlooks a painted star chart.",
                 "exits": {"up": "observation"}},
                {"id": "observation", "name": "Observation Room",
                 "description": "An unoccupied window frames a distant synthetic light.",
                 "exits": {"down": "quiet_dock"}},
            ],
            "items": [],
        }
        station_file = self.directory / "station-world.json"
        station_file.write_text(json.dumps(station_pack), encoding="utf-8")
        sci_fi = Gateway(station, world_id="station-example")
        import_worldpack(station, station_file)

        # Same player and action ID are independent in separate world files.
        fantasy.join("Traveler")
        sci_fi.join("Traveler")
        self.assertIn("Woodland Path", fantasy.command("Traveler", "north", "same_id")["text"])
        self.assertIn("Observation Room", sci_fi.command("Traveler", "up", "same_id")["text"])
        self.assertEqual("woodland_path", _world_rows(medieval, "players")[0][1])
        self.assertEqual("observation", _world_rows(station, "players")[0][1])
        self.assertEqual("north", _world_rows(medieval, "action_receipts")[0][2])
        self.assertEqual("up", _world_rows(station, "action_receipts")[0][2])
        self.assertEqual("Village Green", next(row[1] for row in
            _world_rows(medieval, "rooms") if row[0] == "village_green"))
        self.assertNotIn("quiet_dock", {row[0] for row in _world_rows(medieval, "rooms")})
        self.assertNotIn("village_green", {row[0] for row in _world_rows(station, "rooms")})
        self.assertFalse(any(t.startswith("geo_") for t in _tables(medieval)))
        sci_fi.geography  # Explicit station host opts into generic geography.
        self.assertIn("geo_ships", _tables(station))
        self.assertNotIn("geo_ships", _tables(medieval))
        with self.assertRaisesRegex(ValueError, "different world_id"):
            Gateway(station, world_id="cedarwatch-example")
        self.assertEqual("cedarwatch-example", read_worldpack(FANTASY_PACK)[0]["world_id"])


if __name__ == "__main__":
    unittest.main()
