"""A defeated character cannot bypass death custody through host-only seams."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from realm.clock_gameplay import perform_in_transaction
from realm.combat import Combat
from realm.engine import Engine
from realm.gateway import Gateway
from realm.npc_local_model import LocalChatConfig, LocalChatProposer
from realm.npc_turn_host import ResidentTurnHost
from realm.residents import ResidentController


class DefeatedResidentCustodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        self.gateway = Gateway(
            self.path, world_id="pvp_resident", scoped_credentials=True,
            scoped_guard_path=Path(tmp.name) / "credentials-guard.json",
        )
        self.engine = self.gateway.engine
        for player in ("Ada", "Bob"):
            self.engine.join(player)
        self.tokens = {
            player: self.gateway.scoped_credentials.provision(player)
            for player in ("Ada", "Bob")
        }
        self.bob = ResidentController(
            self.engine, "Bob", allowed_verbs=frozenset({"go", "say", "look"}),
        )
        proposer = LocalChatProposer(LocalChatConfig(
            endpoint="http://127.0.0.1:1/v1/chat/completions", model="fixture",
        ))
        self.host = ResidentTurnHost(self.engine, proposer)

    def command(self, actor, command, action_id):
        return self.gateway.command_remote(
            actor, command, action_id, self.tokens[actor],
        )["text"]

    def kill(self):
        Combat.configure(
            self.engine, max_hp=4, damage=4, allow_offline_targets=True,
        )
        self.command("Ada", "pvp on", "opt-ada")
        self.command("Bob", "pvp on", "opt-bob")
        self.command("Ada", "attack Bob", "fatal")

    def state(self):
        with closing(self.engine._connect()) as db:
            return (
                db.execute("SELECT room_id FROM players WHERE id='Bob'").fetchone()[0],
                db.execute("SELECT count(*) FROM events").fetchone()[0],
                db.execute("SELECT count(*) FROM action_receipts").fetchone()[0],
            )

    def test_direct_perform_cannot_move_defeated_resident(self):
        self.kill()
        before = self.state()
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "Defeated"):
                self.engine._perform(db, "Bob", "atrium", "go", "north")
        self.assertEqual(before, self.state())

    def test_cached_and_fresh_resident_receipts_denied_after_death(self):
        self.bob.commit({"command": "go north"}, action_id="prior-go")
        self.bob.commit({"command": "go south"}, action_id="prior-back")
        self.kill()
        before = self.state()
        for action_id in ("prior-go", "fresh-go"):
            with self.subTest(action_id=action_id):
                with self.assertRaisesRegex(ValueError, "Defeated"):
                    self.bob.commit({"command": "go north"}, action_id=action_id)
        with self.assertRaisesRegex(ValueError, "Defeated"):
            self.bob._replay_if_committed("prior-go")
        for action_id in ("prior-go", "host-fresh"):
            with self.subTest(host_action_id=action_id):
                with self.assertRaisesRegex(ValueError, "Defeated"):
                    self.host._commit_host_turn(
                        self.bob, "Bob", {"command": "go north"}, action_id,
                    )
        self.assertEqual(before, self.state())
        self.assertIn("You say:", self.bob.commit(
            {"command": "say I am still here"}, action_id="ghost-speech",
        ))
        self.assertIn("Atrium", self.bob.commit(
            {"command": "look"}, action_id="ghost-look",
        ))

    def test_scheduled_replay_and_fresh_gather_cannot_act_after_death(self):
        self.gateway.router.seed_demo_crafting()
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            perform_in_transaction(
                db, engine=self.engine, crafting=self.gateway.router.crafting,
                actor="Bob", command="go north", action_id="clock-go-before",
            )
        self.command("Bob", "south", "return-before-death")
        self.kill()
        before = self.state()
        for command, action_id in (
            ("go north", "clock-go-before"),
            ("go north", "clock-go-after"),
            ("gather stone", "clock-gather-after"),
        ):
            with self.subTest(command=command, action_id=action_id):
                with closing(self.engine._connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    with self.assertRaisesRegex(ValueError, "Defeated"):
                        perform_in_transaction(
                            db, engine=self.engine,
                            crafting=self.gateway.router.crafting,
                            actor="Bob", command=command, action_id=action_id,
                        )
        self.assertEqual(before, self.state())

    def test_predeath_pvp_consent_replay_does_not_reenable_defeated_player(self):
        self.kill()
        with self.assertRaisesRegex(ValueError, "Defeated"):
            self.command("Bob", "pvp on", "opt-bob")
        self.assertIn("PvP is now OFF", self.command("Bob", "pvp off", "ghost-off"))


if __name__ == "__main__":
    unittest.main()
