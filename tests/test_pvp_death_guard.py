"""A defeated combatant is a spectator until host-controlled revival."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from realm.combat import Combat
from realm.crafting import Crafting
from realm.engine import Engine
from realm.gateway import Gateway


class PvPDeathGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        self.gateway = Gateway(self.path, world_id="pvp_death", scoped_credentials=True,
                               scoped_guard_path=Path(tmp.name) / "guard.json")
        self.engine = self.gateway.engine
        for actor in ("Ada", "Bob"):
            self.engine.join(actor)
        self.tokens = {name: self.gateway.scoped_credentials.provision(name)
                       for name in ("Ada", "Bob")}
        self.gateway.router.seed_demo_crafting()

    def act(self, name, command, action_id=None):
        return self.gateway.command_remote(name, command, action_id,
                                           self.tokens[name])["text"]

    def kill(self):
        Combat.configure(self.engine, max_hp=4, damage=4, safe_rooms=(),
                         allow_offline_targets=True)
        self.act("Ada", "pvp on", "pvp-ada")
        self.act("Bob", "pvp on", "pvp-bob")
        self.act("Ada", "attack Bob", "kill-bob")

    def test_defeated_actor_cannot_mutate_world_even_with_old_receipt(self):
        predeath = self.act("Bob", "north", "predeath-move")
        self.assertIn("Corridor", predeath)
        self.act("Bob", "south", "back")
        self.assertIn("stone x3", self.act("Bob", "gather stone", "predeath-gather"))
        self.kill()
        with closing(self.engine._connect()) as db:
            before = (db.execute("SELECT room_id FROM players WHERE id='Bob'").fetchone()[0],
                      db.execute("SELECT count(*) FROM events").fetchone()[0],
                      db.execute("SELECT count(*) FROM action_receipts").fetchone()[0])
        for command, action_id in (("north", "blocked-move"),
                                   ("north", "predeath-move"),
                                   ("take token", "blocked-take"),
                                   ("drop token", "blocked-drop"),
                                   ("give token to Ada", "blocked-give"),
                                   ("gather stone", "blocked-gather"),
                                   ("gather stone", "predeath-gather"),
                                   ("craft field knife", "blocked-craft"),
                                   ("build supply cache", "blocked-build")):
            with self.subTest(command=command, action_id=action_id):
                with self.assertRaisesRegex(ValueError, "Defeated"):
                    self.act("Bob", command, action_id)
        with closing(self.engine._connect()) as db:
            after = (db.execute("SELECT room_id FROM players WHERE id='Bob'").fetchone()[0],
                     db.execute("SELECT count(*) FROM events").fetchone()[0],
                     db.execute("SELECT count(*) FROM action_receipts").fetchone()[0])
        self.assertEqual(before, after)
        for read in ("look", "where", "inventory", "who", "history", "pvp status",
                     "pvp news", "resources", "recipes", "structures", "gather"):
            self.assertIsInstance(self.act("Bob", read, "read-" + read), str)
        self.assertIn("You say:", self.act("Bob", "say Ghostly hello", "ghost-says"))
        self.assertIn("HP 0/4", self.act("Bob", "pvp status"))

    def test_host_revival_reenables_mutations_and_requires_new_consent(self):
        self.kill()
        with self.assertRaisesRegex(ValueError, "Defeated"):
            self.act("Bob", "north", "deny-when-dead")
        Combat.revive(self.engine, "Bob")
        self.assertIn("HP 4/4", self.act("Bob", "pvp status"))
        self.assertIn("PvP OFF", self.act("Bob", "pvp status"))
        self.assertIn("Corridor", self.act("Bob", "north", "allow-after-revival"))
        self.assertIn("Materials:", self.act("Bob", "resources"))
        self.assertIn("gather", self.act("Bob", "gather wood", "gather-after-revival"))

    def test_legacy_world_without_combat_remains_fully_operational(self):
        self.assertIn("Corridor", self.act("Bob", "north", "legacy-move"))
        self.assertIn("gather", self.act("Bob", "gather wood", "legacy-gather"))

    def test_scheduled_engine_and_crafting_actions_cannot_revive_a_defeated_actor(self):
        from realm.clock_gameplay import perform_in_transaction
        self.kill()
        for command in ("go north", "take token", "drop token", "gather stone",
                        "craft field knife", "build supply cache"):
            with self.subTest(command=command), closing(self.engine._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(ValueError, "Defeated"):
                    perform_in_transaction(db, engine=self.engine,
                                           crafting=self.gateway.router.crafting,
                                           actor="Bob", command=command,
                                           action_id="scheduled-" + command)
        Combat.revive(self.engine, "Bob")
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            response = perform_in_transaction(
                db, engine=self.engine, crafting=self.gateway.router.crafting,
                actor="Bob", command="go north", action_id="scheduled-after-revival",
            )
            self.assertIn("Corridor", response)
        self.assertIn("Corridor", self.act("Bob", "where"))


if __name__ == "__main__":
    unittest.main()
