"""Opt-in PvP state belongs to the host's one-world SQLite transaction."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from realm.engine import Engine
from realm.gateway import Gateway


class PvPCombatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        self.gateway = Gateway(self.path, world_id="pvp_world", scoped_credentials=True,
                               scoped_guard_path=Path(tmp.name) / "scoped-guard.json")
        self.engine = self.gateway.engine
        self.engine.join("Ada")
        self.engine.join("Bob")
        self.tokens = {
            player: self.gateway.scoped_credentials.provision(player)
            for player in ("Ada", "Bob")
        }

    def act(self, player, command, action_id=None):
        return self.gateway.command_remote(player, command, action_id,
                                           self.tokens[player])["text"]

    def state(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            combatants = {
                row["player"]: (row["hp"], row["pvp_enabled"])
                for row in db.execute("SELECT * FROM combatants")
            }
            events = [dict(row) for row in db.execute(
                "SELECT id,actor,room_id,kind,payload_json FROM events "
                "WHERE kind IN ('pvp_hit','pvp_death') ORDER BY id"
            )]
            return combatants, events

    def test_unconfigured_world_denies_combat_without_schema_install(self):
        with self.assertRaisesRegex(ValueError, "PvP is not enabled"):
            self.act("Ada", "pvp on")
        with self.assertRaisesRegex(ValueError, "PvP is not enabled"):
            self.act("Ada", "attack Bob", action_id="denied")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='combatants'"
            ).fetchone()[0])

    def test_opt_in_safe_zone_damage_death_and_durable_replay(self):
        from realm.combat import Combat
        Combat.configure(self.engine, max_hp=8, damage=4,
                         safe_rooms=("atrium",), announcement_style="herald",
                         allow_offline_targets=True)
        self.assertIn("HP 8/8", self.act("Ada", "pvp status"))
        with self.assertRaisesRegex(PermissionError, "player-bound"):
            self.engine.act("Ada", "pvp on")
        with self.assertRaisesRegex(PermissionError, "player-bound"):
            self.engine.act("Bob", "pvp status")
        self.act("Ada", "pvp on")
        with self.assertRaisesRegex(ValueError, "not opted in"):
            self.act("Ada", "attack Bob", action_id="no-consent")
        self.act("Bob", "pvp on")
        with self.assertRaisesRegex(ValueError, "safe zone"):
            self.act("Ada", "attack Bob", action_id="no-safe")
        self.act("Ada", "north")
        with self.assertRaisesRegex(ValueError, "same room"):
            self.act("Ada", "attack Bob", action_id="no-room")
        self.act("Bob", "north")
        with self.assertRaisesRegex(ValueError, "yourself"):
            self.act("Ada", "attack Ada", action_id="no-self")
        first = self.act("Ada", "attack Bob", action_id="pvp-1")
        self.assertIn("4 damage", first)
        self.assertEqual(first, self.act("Ada", "attack Bob", action_id="pvp-1"))
        state, events = self.state()
        self.assertEqual((4, 1), state["Bob"])
        self.assertEqual(["pvp_hit"], [event["kind"] for event in events])

        ending = self.act("Ada", "attack Bob", action_id="pvp-2")
        self.assertIn("has fallen", ending)
        self.assertEqual(ending, self.act("Ada", "attack Bob", action_id="pvp-2"))
        state, events = self.state()
        self.assertEqual((0, 1), state["Bob"])
        self.assertEqual(["pvp_hit", "pvp_hit", "pvp_death"],
                         [event["kind"] for event in events])
        death = json.loads(events[-1]["payload_json"])
        self.assertEqual("realm-original-herald-v1", death["template_id"])
        self.assertEqual("pvp-2", death["action_id"])
        self.assertEqual(("Ada", "Bob"), (death["killer"], death["victim"]))
        self.assertIn(death["message"], self.act("Ada", "pvp news"))
        self.assertIn("HP 0/8", self.act("Bob", "pvp status"))
        with self.assertRaisesRegex(ValueError, "[Dd]efeated"):
            self.act("Bob", "attack Ada", action_id="dead-cannot-attack")

    def test_opt_out_and_cross_world_target_scope(self):
        from realm.combat import Combat
        Combat.configure(self.engine, safe_rooms=(), allow_offline_targets=True)
        self.act("Ada", "pvp on")
        self.act("Bob", "pvp on")
        self.act("Ada", "pvp off")
        with self.assertRaisesRegex(ValueError, "not opted in"):
            self.act("Bob", "attack Ada", action_id="opted-out")
        with self.assertRaisesRegex(ValueError, "target"):
            self.act("Bob", "attack Outside", action_id="not-here")
        other = Engine(self.path.parent / "separate.sqlite3")
        other.join("Outside")
        self.assertIn("Outside", other.act("Outside", "who"))
        with self.assertRaisesRegex(ValueError, "target"):
            self.act("Bob", "attack Outside", action_id="other-world")

    def test_global_death_announcement_is_minimal_and_room_safe(self):
        from realm.combat import Combat
        Combat.configure(self.engine, max_hp=4, damage=4, safe_rooms=(),
                         announcement_style="stark", allow_offline_targets=True)
        self.engine.join("Cora")
        self.tokens["Cora"] = self.gateway.scoped_credentials.provision("Cora")
        self.act("Ada", "pvp on")
        self.act("Bob", "pvp on")
        self.act("Ada", "north")
        self.act("Bob", "north")
        self.act("Ada", "attack Bob", action_id="death-1")
        # Legacy shared-token spectators may read host-authored world news,
        # but cannot inspect a specific player's private consent or HP.
        public_news = self.engine.act("Cora", "pvp news")
        self.assertIn("Ada defeated Bob", public_news)
        self.assertNotIn("corridor", public_news.lower())
        with self.assertRaisesRegex(PermissionError, "player-bound"):
            self.engine.act("Cora", "pvp status")
        feed = self.gateway.feed("Cora", 0)
        announcements = [event for event in feed["events"] if event["kind"] == "pvp_death"]
        self.assertEqual(1, len(announcements))
        notice = announcements[0]
        self.assertEqual("global", notice["room"], "Global feed must not expose the hidden fight room")
        self.assertIn("Ada defeated Bob", notice["text"])
        self.assertNotIn("corridor", str(notice).lower())


if __name__ == "__main__":
    unittest.main()
