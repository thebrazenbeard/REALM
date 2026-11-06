"""Passive Siltglass lore: referential integrity versus REALM's actual v1 engine.

No NPCs, prices, weather, factions or quests are executed by these tests.
Gameplay assertions cover only imported rooms, exits and portable items.
"""

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from realm.engine import Engine, _PLAYER_RE
from realm.worldpack import WorldpackError, import_worldpack, read_worldpack, validate_worldpack


WORLD_DIR = ROOT / "worlds" / "flagship" / "siltglass-reach"
PACK = WORLD_DIR / "worldpack.v1.json"
LORE = WORLD_DIR / "living_lore.v1.json"


class SiltglassLivingLoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world, cls.digest = read_worldpack(PACK)
        cls.lore = json.loads(LORE.read_text(encoding="utf-8"))
        cls.rooms = {room["id"] for room in cls.world["rooms"]}
        cls.items = {item["id"]: item for item in cls.world["items"]}

    def test_lore_is_passive_not_an_engine_worldpack_extension(self):
        lore = self.lore
        self.assertEqual("realm.siltglass-lore.v1", lore["schema"])
        self.assertEqual(self.world["world_id"], lore["world_id"])
        self.assertEqual("passive-reference-only", lore["status"])
        self.assertEqual(24, len(self.world["rooms"]))
        self.assertEqual(14, len(self.world["items"]))
        self.assertEqual(
            {"schema", "world_id", "name", "start_room", "rooms", "items"},
            set(self.world),
        )
        rejected = dict(self.world, npcs=lore["npcs"])
        with self.assertRaisesRegex(WorldpackError, "extra"):
            validate_worldpack(rejected)
        forbidden = {"commands", "scripts", "code", "sql", "python", "effects", "model_weights"}
        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(key, forbidden)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(lore)

    def test_npcs_factions_economy_weather_quests_reference_real_places(self):
        lore = self.lore
        factions = {f["id"]: f for f in lore["factions"]}
        self.assertEqual(3, len(factions))
        for f in factions.values():
            self.assertIn(f["meeting_room"], self.rooms)
            self.assertGreater(len(f["name"]), 3)
            for key in ("stake", "fault_line", "player_choice"):
                self.assertGreater(len(f[key]), 15)
        npc_ids = [n["id"] for n in lore["npcs"]]
        self.assertEqual(6, len(npc_ids))
        self.assertEqual(len(npc_ids), len(set(npc_ids)))
        for npc in lore["npcs"]:
            self.assertIn(npc["home_room"], self.rooms)
            self.assertIn(npc["faction_id"], factions)
            self.assertGreater(len(npc["knowledge_limit"]), 25)
            self.assertIsNotNone(_PLAYER_RE.fullmatch(npc["actor_id"]))
            self.assertTrue(1 <= len(npc["persona"]) <= 240)
            self.assertEqual(npc["persona"].strip(), npc["persona"])
            self.assertTrue(all(char.isprintable() for char in npc["persona"]))
            self.assertNotIn("<|im_", npc["persona"].casefold())
        self.assertEqual(len(lore["npcs"]), len({n["actor_id"].casefold() for n in lore["npcs"]}))
        offers = lore["economy"]["offers"]
        self.assertEqual(5, len(offers))
        for offer in offers:
            self.assertIn(offer["item_id"], self.items)
            self.assertEqual(offer["room_id"], self.items[offer["item_id"]]["room"])
            self.assertGreater(offer["asking_tallies"], 0)
        weather_ids = {state["id"] for state in lore["weather"]["states"]}
        self.assertEqual(4, len(weather_ids))
        for state in lore["weather"]["states"]:
            self.assertTrue(set(state["rooms"]) <= self.rooms)
            self.assertGreater(len(state["uncertain_effect"]), 15)
        quests = lore["quest_seeds"]
        self.assertEqual(4, len(quests))
        self.assertEqual(len(quests), len({q["id"] for q in quests}))
        for quest in quests:
            self.assertIn(quest["start_room"], self.rooms)
            self.assertTrue(set(quest["clue_items"]) <= set(self.items))
            self.assertEqual(2, len(quest["player_paths"]))

    def test_items_are_playable_but_lore_roles_do_not_spawn_actors(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.sqlite3"
            second = Path(temporary) / "second.sqlite3"
            self.assertFalse(import_worldpack(first, PACK)["unchanged"])
            self.assertFalse(import_worldpack(second, PACK)["unchanged"])
            with sqlite3.connect(first) as db:
                self.assertEqual(0, db.execute("SELECT COUNT(*) FROM players").fetchone()[0])
                self.assertEqual(14, db.execute("SELECT COUNT(*) FROM items").fetchone()[0])
            player = Engine(first)
            self.assertIn("The Tidegate", player.join("Gatherer"))
            self.assertIn("Lantern Jetty", player.act("Gatherer", "east"))
            self.assertIn("Notched crossing token", player.act("Gatherer", "take crossing_token"))
            self.assertIn("Notched crossing token", player.act("Gatherer", "inventory"))
            self.assertIn("You drop", player.act("Gatherer", "drop crossing_token"))
            # Same source worldpack does not mean shared live ownership/state.
            other = Engine(second)
            self.assertIn("The Tidegate", other.join("Gatherer"))
            self.assertIn("Lantern Jetty", other.act("Gatherer", "east"))
            self.assertIn("Notched crossing token", other.act("Gatherer", "look"))
            self.assertTrue(import_worldpack(first, PACK)["unchanged"])


if __name__ == "__main__":
    unittest.main()
