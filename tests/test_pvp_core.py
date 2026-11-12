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

    def test_nonlethal_hit_is_visible_in_victim_room_but_not_elsewhere(self):
        """A victim needs legible combat; remote spectators get no location leak."""
        from realm.combat import Combat
        Combat.configure(self.engine, max_hp=8, damage=3,
                         safe_rooms=(), allow_offline_targets=True)
        self.engine.join("Cora")
        self.tokens["Cora"] = self.gateway.scoped_credentials.provision("Cora")
        self.act("Ada", "pvp on")
        self.act("Bob", "pvp on")
        self.act("Ada", "north")
        self.act("Bob", "north")
        self.act("Ada", "attack Bob", action_id="private-hit-receipt")

        victim_feed = self.gateway.feed("Bob", 0)
        hits = [e for e in victim_feed["events"] if e["kind"] == "pvp_hit"]
        self.assertEqual(1, len(hits))
        self.assertIn("Ada", hits[0]["text"])
        self.assertIn("Bob", hits[0]["text"])
        self.assertIn("3 damage", hits[0]["text"])
        self.assertNotIn("acted", hits[0]["text"])
        self.assertNotIn("private-hit-receipt", str(hits[0]))
        self.assertNotIn("remaining_hp", str(hits[0]))

        other_room_feed = self.gateway.feed("Cora", 0)
        self.assertFalse(any(e["kind"] == "pvp_hit" for e in other_room_feed["events"]))

    def test_stale_pvp_consent_receipts_do_not_misreport_current_choice(self):
        from realm.combat import Combat
        Combat.configure(self.engine, allow_offline_targets=True)
        self.assertIn("now ON", self.act("Ada", "pvp on", action_id="consent-on-1"))
        self.assertIn("now OFF", self.act("Ada", "pvp off", action_id="consent-off-1"))
        self.assertIn("now ON", self.act("Ada", "pvp on", action_id="consent-on-2"))

        stale_off = self.act("Ada", "pvp off", action_id="consent-off-1")
        self.assertIn("superseded", stale_off)
        self.assertIn("currently ON", stale_off)
        self.assertIn("PvP ON", self.act("Ada", "pvp status"))

        self.assertIn("now OFF", self.act("Ada", "pvp off", action_id="consent-off-2"))
        stale_on = self.act("Ada", "pvp on", action_id="consent-on-2")
        self.assertIn("superseded", stale_on)
        self.assertIn("currently OFF", stale_on)
        self.assertIn("PvP OFF", self.act("Ada", "pvp status"))
        combatants, events = self.state()
        self.assertEqual((12, 0), combatants["Ada"])
        self.assertEqual([], events)

    def test_presence_required_status_does_not_claim_witness_is_absent(self):
        from realm.combat import Combat
        Combat.configure(self.engine, allow_offline_targets=False)
        status = self.act("Ada", "pvp status")
        self.assertIn("attacks require recent authenticated presence", status)
        self.assertNotIn("online presence unverified; attacks blocked", status)

    def test_stale_opt_in_receipt_cannot_override_host_revival_opt_out(self):
        from realm.combat import Combat
        Combat.configure(self.engine, max_hp=4, damage=4, allow_offline_targets=True)
        self.act("Ada", "pvp on", action_id="ada-before-death")
        self.act("Bob", "pvp on", action_id="bob-consent")
        self.act("Bob", "attack Ada", action_id="fatal-hit")
        Combat.revive(self.engine, "Ada")

        stale = self.act("Ada", "pvp on", action_id="ada-before-death")
        self.assertIn("superseded", stale)
        self.assertIn("currently OFF", stale)
        self.assertIn("PvP OFF", self.act("Ada", "pvp status"))
        combatants, events = self.state()
        self.assertEqual((4, 0), combatants["Ada"])
        self.assertEqual(["pvp_hit", "pvp_death"], [e["kind"] for e in events])

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

    def test_original_host_selected_pk_styles_are_durable_and_room_private(self):
        from realm.combat import Combat
        styles = {
            "black_bell": ("realm-original-black-bell-v1",
                           "The black bells toll: Bob has fallen to Ada."),
            "omen": ("realm-original-omen-v1",
                     "A shadow crosses the realm: Ada has laid Bob low."),
            "taunt": ("realm-original-taunt-v1",
                      "Somewhere a bard revises the tale. Ada has bested Bob."),
        }
        for style, (template_id, announcement) in styles.items():
            with self.subTest(style=style), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                gateway = Gateway(root / "world.sqlite3", world_id="style_world",
                                  scoped_credentials=True,
                                  scoped_guard_path=root / "scope.json")
                for player in ("Ada", "Bob", "Cora"):
                    gateway.engine.join(player)
                tokens = {
                    player: gateway.scoped_credentials.provision(player)
                    for player in ("Ada", "Bob")
                }
                Combat.configure(gateway.engine, max_hp=4, damage=4,
                                 announcement_style=style, allow_offline_targets=True)
                def act(player, command, action_id):
                    return gateway.command_remote(player, command, action_id,
                                                  tokens[player])["text"]
                act("Ada", "pvp on", "ada-on")
                act("Bob", "pvp on", "bob-on")
                act("Ada", "north", "ada-north")
                act("Bob", "north", "bob-north")
                first = act("Ada", "attack Bob", "fatal-1")
                self.assertIn("has fallen", first)
                self.assertEqual(first, act("Ada", "attack Bob", "fatal-1"))
                death_events = [e for e in gateway.feed("Cora", 0)["events"]
                                if e["kind"] == "pvp_death"]
                self.assertEqual(1, len(death_events))
                self.assertEqual("global", death_events[0]["room"])
                self.assertEqual(announcement, death_events[0]["text"])
                self.assertNotIn("corridor", str(death_events).lower())
                with closing(sqlite3.connect(root / "world.sqlite3")) as db:
                    rows = db.execute(
                        "SELECT template_id,message FROM combat_announcements"
                    ).fetchall()
                self.assertEqual([(template_id, announcement)], rows)


if __name__ == "__main__":
    unittest.main()
