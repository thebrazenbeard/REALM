"""Host-side persistence, contention and resource-economy regression tests."""

import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.crafting import Crafting
from realm.engine import Engine


class CraftingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "world.db"
        self.engine = Engine(self.db_path)
        self.engine.join("Patrick")
        self.engine.join("Vera")
        self.crafting = Crafting(self.db_path)
        self.crafting.place_node("atrium", "stone", stock=20, batch_yield=3)
        self.crafting.place_node("corridor", "wood", stock=9, batch_yield=4)
        self.crafting.register_recipe("field knife", kind="craft", ingredients={"stone": 2})
        self.crafting.register_recipe("supply cache", kind="build", ingredients={"wood": 4, "stone": 2})

    def test_gather_and_craft_survive_restart(self):
        self.assertEqual("You gather stone x3.", self.crafting.perform("Patrick", "gather stone"))
        self.assertEqual("You craft field knife x1.", Crafting(self.db_path).perform("Patrick", "craft field knife"))
        resources = Crafting(self.db_path).perform("Patrick", "resources")
        self.assertIn("stone x1", resources)
        self.assertIn("field knife x1", resources)

    def test_batch_gather_reduces_repeated_clicks_and_caps_at_ten(self):
        self.assertEqual("You gather stone x20.", self.crafting.perform("Patrick", "gather stone 8"))
        self.assertIn("No stone remains", self.crafting.perform("Vera", "gather stone"))
        self.assertIn("between 1 and 10", self.crafting.perform("Patrick", "gather stone 100"))
        self.assertIn("between 1 and 10", self.crafting.perform("Patrick", "gather stone 0"))

    def test_only_location_nodes_can_be_gathered(self):
        self.assertIn("no wood", self.crafting.perform("Patrick", "gather wood"))
        self.engine.act("Patrick", "north")
        self.assertEqual("You gather wood x4.", self.crafting.perform("Patrick", "gather wood"))

    def test_missing_recipe_materials_do_not_consume_resources(self):
        self.crafting.perform("Patrick", "gather stone")
        self.assertIn("No build recipe", self.crafting.perform("Patrick", "build field knife"))
        self.assertIn("Missing materials", self.crafting.perform("Patrick", "build supply cache"))
        self.assertIn("stone x3", self.crafting.perform("Patrick", "resources"))

    def test_build_is_persistent_room_scoped_and_not_repeated_for_owner(self):
        self.crafting.perform("Patrick", "gather stone")
        self.engine.act("Patrick", "north")
        self.crafting.perform("Patrick", "gather wood")
        self.assertEqual("You build supply cache here.", self.crafting.perform("Patrick", "build supply cache"))
        self.assertIn("supply cache (Patrick)", Crafting(self.db_path).perform("Patrick", "structures"))
        self.assertIn("already built", self.crafting.perform("Patrick", "build supply cache"))
        self.assertNotIn("wood x", self.crafting.perform("Patrick", "resources"))
        self.assertEqual("Structures here: none", self.crafting.perform("Vera", "structures"))

    def test_idempotent_retry_does_not_gather_or_craft_twice(self):
        self.assertEqual("You gather stone x3.", self.crafting.perform("Patrick", "gather stone", action_id="tx-1"))
        self.assertEqual("You gather stone x3.", Crafting(self.db_path).perform("Patrick", "gather stone", action_id="tx-1"))
        self.assertEqual("You craft field knife x1.", self.crafting.perform("Patrick", "craft field knife", action_id="tx-2"))
        self.assertEqual("You craft field knife x1.", self.crafting.perform("Patrick", "craft field knife", action_id="tx-2"))
        with self.assertRaisesRegex(ValueError, "different command"):
            self.crafting.perform("Patrick", "gather stone", action_id="tx-2")
        self.assertIn("field knife x1", self.crafting.perform("Patrick", "resources"))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM events WHERE kind='craft'").fetchone()[0])

    def test_resources_read_receipt_reflects_new_holdings_after_gather(self):
        before = self.crafting.perform("Patrick", "resources", action_id="read-resources")
        self.assertIn("Materials: none", before)
        self.crafting.perform("Patrick", "gather stone")
        after = self.crafting.perform("Patrick", "resources", action_id="read-resources")
        self.assertIn("stone x3", after)
        self.assertNotEqual(before, after, "an old read receipt cannot conceal new holdings")

    def test_recipes_read_receipt_reflects_author_registration(self):
        before = self.crafting.perform("Patrick", "recipes", action_id="read-recipes")
        self.assertNotIn("signal fire", before)
        self.crafting.register_recipe("signal fire", kind="build", ingredients={"wood": 2})
        after = Crafting(self.db_path).perform("Patrick", "recipes", action_id="read-recipes")
        self.assertIn("signal fire", after)
        self.assertNotEqual(before, after)

    def test_structures_read_receipt_tracks_current_room(self):
        before = self.crafting.perform("Patrick", "structures", action_id="read-structures")
        self.assertEqual("Structures here: none", before)
        self.crafting.perform("Patrick", "gather stone")
        self.engine.act("Patrick", "north")
        self.crafting.perform("Patrick", "gather wood")
        self.assertEqual("You build supply cache here.", self.crafting.perform("Patrick", "build supply cache"))
        after = self.crafting.perform("Patrick", "structures", action_id="read-structures")
        self.assertIn("supply cache (Patrick)", after)
        self.assertNotEqual(before, after, "room-scoped reads cannot replay the old room")

    def test_gather_without_material_is_a_read_that_tracks_node_stock(self):
        before = self.crafting.perform("Patrick", "gather", action_id="read-nodes")
        self.assertIn("stone (20)", before)
        self.crafting.perform("Vera", "gather stone")
        after = self.crafting.perform("Patrick", "gather", action_id="read-nodes")
        self.assertIn("stone (17)", after)
        with self.assertRaisesRegex(ValueError, "different command"):
            self.crafting.perform("Patrick", "resources", action_id="read-nodes")

    def test_duplicate_recipe_definition_identical_is_safe_but_conflict_rejected(self):
        self.assertFalse(self.crafting.register_recipe("field knife", kind="craft", ingredients={"stone": 2}))
        with self.assertRaisesRegex(ValueError, "differently"):
            self.crafting.register_recipe("field knife", kind="craft", ingredients={"stone": 1})

    def test_author_setup_does_not_restock_once_depleted(self):
        self.crafting.perform("Patrick", "gather stone 7")
        self.assertFalse(self.crafting.place_node("atrium", "stone", stock=999))
        self.assertIn("No stone remains", self.crafting.perform("Vera", "gather stone"))

    def test_concurrent_gathering_does_not_mint_extra_materials(self):
        def take(player):
            return Crafting(self.db_path).perform(player, "gather wood 10")
        self.engine.act("Patrick", "north")
        self.engine.act("Vera", "north")
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(take, ["Patrick", "Vera"]))
        self.assertEqual(1, sum("x9" in output for output in outputs))
        self.assertEqual(1, sum("No wood remains" in output for output in outputs))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            left = db.execute("SELECT remaining FROM gathering_nodes WHERE room_id='corridor' AND material='wood'").fetchone()[0]
            total = db.execute("SELECT SUM(quantity) FROM material_pouches WHERE material='wood'").fetchone()[0]
        self.assertEqual((0, 9), (left, total))

    def test_separate_worlds_cannot_share_resources(self):
        other = Crafting(Path(self.temp.name) / "other.db")
        Engine(other.db_path).join("Patrick")
        self.assertEqual("Gatherable here: none", other.perform("Patrick", "gather"))
        self.assertIn("stone", self.crafting.perform("Patrick", "gather"))

    def test_unrelated_commands_are_not_captured(self):
        self.assertIsNone(self.crafting.perform("Patrick", "north"))
        self.assertIsNone(self.crafting.perform("Patrick", "register_recipe hacked"))

    def test_invalid_setup_or_unjoined_actor_is_rejected(self):
        with self.assertRaises(ValueError):
            self.crafting.place_node("atrium", "stone", stock=-1)
        with self.assertRaises(ValueError):
            self.crafting.register_recipe("invalid", kind="craft", ingredients={"stone": True})
        with self.assertRaises(ValueError):
            self.crafting.register_recipe("invalid", kind="build", ingredients={"stone": 1}, output_count=2)
        with self.assertRaises(ValueError):
            self.crafting.perform("NoSuchPlayer", "gather stone")


if __name__ == "__main__":
    unittest.main()
