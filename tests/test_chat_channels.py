"""Host-governed, world-local chat channels never inherit visual ship roles."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.channel_roster import ChannelRoster
from realm.combat import Combat
from realm.gateway import Gateway


class ChannelContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "world.sqlite3"
        self.gateway = Gateway(self.db, world_id="local_world")
        for player in ("Alice", "Bob", "Cara", "Late"):
            if player != "Late":
                self.gateway.join(player)
        self.roster = ChannelRoster(self.db, world_id="local_world")

    def _events(self, actor, after=0):
        return self.gateway.feed(actor, after)["events"]

    def _channel_events(self, actor, kind, after=0):
        return [entry for entry in self._events(actor, after) if entry["kind"] == kind]

    def _install(self):
        self.roster.install()
        self.roster.create_group("expedition", "Expedition")
        self.roster.create_group("observers", "Observers")
        self.roster.grant("Alice", "expedition", role="Leader")
        self.roster.grant("Bob", "expedition", role="Member")
        self.roster.grant("Cara", "observers", role="Observer")

    def test_worldwide_ooc_delivers_across_rooms_only_after_first_join(self):
        self.assertEqual("[OOC Channel] Alice: Hello world", self.gateway.command("Alice", "ooc Hello world", None)["text"])
        self.gateway.command("Alice", "north", None)
        self.assertEqual("[OOC Channel] Bob: Greetings", self.gateway.command("Bob", "ooc Greetings", None)["text"])
        bob = self._channel_events("Bob", "ooc")
        alice = self._channel_events("Alice", "ooc")
        self.assertEqual(["[OOC Channel] Alice: Hello world", "[OOC Channel] Bob: Greetings"],
                         [row["text"] for row in bob])
        self.assertEqual([row["text"] for row in bob], [row["text"] for row in alice])
        self.gateway.join("Late")
        self.assertEqual([], self._channel_events("Late", "ooc"), "A late join must not inherit prior global chatter")
        count = len(self._channel_events("Bob", "ooc"))
        self.assertIn("what", self.gateway.command("Bob", "ooc", None)["text"].lower())
        self.assertEqual(count, len(self._channel_events("Bob", "ooc")))

    def test_group_visibility_is_not_same_room_or_other_group_visibility(self):
        self._install()
        line = '[Expedition][Leader] Alice: "Secure channel"'
        self.assertEqual(line, self.gateway.command("Alice", "crewchat Secure channel", "first")["text"])
        pretended = self.gateway.command("Alice", "crewchat [Forged][Admiral] still user text", None)
        self.assertEqual('[Expedition][Leader] Alice: "[Forged][Admiral] still user text"',
                         pretended["text"], "Client-supplied brackets never replace the host role prefix")
        self.assertEqual([line, pretended["text"]],
                         [row["text"] for row in self._channel_events("Bob", "crewchat")])
        self.assertEqual([], self._channel_events("Cara", "crewchat"),
                         "Same-room outsider from another group must not learn sender or message")
        self.gateway.command("Bob", "north", None)
        self.assertEqual('[Expedition][Member] Bob: "Across rooms"',
                         self.gateway.command("Bob", "crewchat Across rooms", None)["text"])
        self.assertEqual(3, len(self._channel_events("Alice", "crewchat")))
        self.assertEqual(0, len(self._channel_events("Cara", "crewchat")))
        outsider_events = self._events("Cara")
        self.assertFalse(any(event["kind"] == "crewchat" for event in outsider_events))
        self.assertFalse(any("Secure channel" in event["text"] for event in outsider_events))
        self.assertFalse(any("Expedition" in event["text"] for event in outsider_events))

    def test_membership_revocation_rejoin_floor_and_stale_role(self):
        self._install()
        self.gateway.command("Alice", "crewchat First", "id-first")
        self.roster.grant("Alice", "expedition", role="Navigator")
        self.assertEqual('[Expedition][Navigator] Alice: "Second"',
                         self.gateway.command("Alice", "crewchat Second", "id-second")["text"])
        self.assertEqual(2, len(self._channel_events("Bob", "crewchat")))
        self.roster.revoke("Bob")
        self.assertEqual([], self._channel_events("Bob", "crewchat"), "Revoked member must lose entire channel history")
        self.assertIn("membership", self.gateway.command("Bob", "crewchat Denied", "id-denied")["text"].lower())
        self.assertNotIn("First", self.gateway.command("Bob", "history 50", None)["text"])
        self.roster.grant("Bob", "expedition", role="Member")
        self.assertEqual([], self._channel_events("Bob", "crewchat"),
                         "Rejoin must not restore prior messages from same group")
        self.gateway.command("Alice", "crewchat Third", "id-third")
        self.assertEqual(['[Expedition][Navigator] Alice: "Third"'],
                         [row["text"] for row in self._channel_events("Bob", "crewchat")])
        self.assertIn("Third", self.gateway.command("Bob", "listen", None)["text"])
        self.assertNotIn("Second", self.gateway.command("Bob", "history 50", None)["text"])
        self.roster.revoke("Bob")
        self.roster.grant("Bob", "observers", role="Observer")
        self.assertEqual([], self._channel_events("Bob", "crewchat"),
                         "Switching groups must never revive older foreign-group posts")

    def test_first_group_grant_never_exposes_messages_sent_before_membership(self):
        self.roster.install()
        self.roster.create_group("expedition", "Expedition")
        self.roster.grant("Alice", "expedition", role="Leader")
        self.gateway.command("Alice", "crewchat Before membership", None)
        self.roster.grant("Bob", "expedition", role="Member")
        self.assertEqual([], self._channel_events("Bob", "crewchat"))
        self.assertNotIn("Before membership",
                         self.gateway.command("Bob", "listen", None)["text"])
        self.gateway.command("Alice", "crewchat After membership", None)
        self.assertEqual(['[Expedition][Leader] Alice: "After membership"'],
                         [x["text"] for x in self._channel_events("Bob", "crewchat")])

    def test_revoke_regrant_without_intervening_game_events_still_changes_epoch(self):
        self._install()
        self.gateway.command("Alice", "crewchat Before immediate regrant", None)
        self.assertEqual(1, len(self._channel_events("Bob", "crewchat")))
        self.roster.revoke("Bob")
        self.roster.grant("Bob", "expedition", role="Member")
        self.assertEqual([], self._channel_events("Bob", "crewchat"),
                         "No intervening message must not make regrant reuse an old floor")

    def test_groups_are_explicit_world_bound_never_inferred_from_ship_titles(self):
        self.assertIn("membership", self.gateway.command("Alice", "crewchat Guess", None)["text"].lower())
        self.assertEqual(0, self.gateway.feed("Alice", 0)["channel_revision"],
                         "A world without optional channels has no host permission epoch")
        with closing(sqlite3.connect(self.db)) as db:
            found = db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('chat_channels','chat_memberships')"
            ).fetchall()
        self.assertEqual([], found, "Reading or sending must not install optional group tables")
        foreign = ChannelRoster(self.db, world_id="other_world")
        with self.assertRaises(PermissionError):
            foreign.install()
        self._install()
        with self.assertRaises(PermissionError):
            foreign.create_group("forged", "Forged")
        with self.assertRaises(PermissionError):
            foreign.grant("Cara", "expedition", role="Captain")
        with self.assertRaises(PermissionError):
            foreign.revoke("Alice")
        self.assertEqual([], self._channel_events("Cara", "crewchat"))

    def test_reserved_marker_room_name_never_discloses_permission_changes(self):
        # Room identifiers are worldpack-controlled, including values that
        # collide with internal channel routing keys. SQL audience checks
        # must filter the private *kind*, not trust a reserved room string.
        with closing(sqlite3.connect(self.db)) as db:
            db.execute(
                "INSERT INTO rooms(id,name,description) VALUES(?,?,?)",
                ("@channel-permission", "Ledger Chamber", "An ordinary room."),
            )
            db.execute(
                "UPDATE players SET room_id=? WHERE id=?",
                ("@channel-permission", "Bob"),
            )
            db.execute(
                "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                "VALUES(?,?,?,?,?)",
                ("2026-11-10T00:00:00+00:00", "Bob", "@channel-permission",
                 "move", '{"from":"atrium","to":"@channel-permission"}'),
            )
            db.commit()
        self.roster.install()
        self.roster.create_group("guild", "Guild")
        self.roster.grant("Alice", "guild", role="Officer")
        visible = self.gateway.feed("Bob", 0)["events"]
        self.assertFalse(any(event["kind"] == "chat_permission_epoch" for event in visible))
        self.assertFalse(any(event["actor"] == "Alice" for event in visible))
        self.gateway.command("Bob", "emote checks the chamber.", None)
        self.assertTrue(any(event["kind"] == "emote" for event in
                            self.gateway.feed("Bob", 0)["events"]))
        self.assertNotIn("Alice", self.gateway.command("Bob", "history 50", None)["text"])

    def test_channel_receipts_are_idempotent_and_bound_to_membership_epoch(self):
        self._install()
        once = self.gateway.command("Alice", "ooc Global once", "ooc-stable")
        self.assertEqual(once, self.gateway.command("Alice", "ooc Global once", "ooc-stable"))
        self.assertEqual(1, len(self._channel_events("Bob", "ooc")))
        first = self.gateway.command("Alice", "crewchat One", "stable-id")
        self.assertEqual(first, self.gateway.command("Alice", "crewchat One", "stable-id"))
        self.assertEqual(1, len(self._channel_events("Bob", "crewchat")))
        with self.assertRaises(ValueError):
            self.gateway.command("Alice", "crewchat Two", "stable-id")
        self.roster.revoke("Alice")
        self.assertIn("membership", self.gateway.command("Alice", "crewchat One", "stable-id")["text"].lower())
        self.roster.grant("Alice", "expedition", role="Leader")
        with self.assertRaises(ValueError):
            self.gateway.command("Alice", "crewchat One", "stable-id")
        self.assertEqual(1, len(self._channel_events("Bob", "crewchat")))

    def test_role_change_invalidates_old_crew_receipt_without_mutation(self):
        self._install()
        first = self.gateway.command("Alice", "crewchat Ready", "fixed-role-id")
        self.assertIn("[Leader]", first["text"])
        self.roster.grant("Alice", "expedition", role="Navigator")
        before = self.gateway.cursor()
        with self.assertRaisesRegex(ValueError, "Action ID already used"):
            self.gateway.command("Alice", "crewchat Ready", "fixed-role-id")
        self.assertEqual(before, self.gateway.cursor(), "Stale role replay wrote an event")
        latest = self.gateway.command("Alice", "crewchat Ready", "new-role-id")
        self.assertIn("[Navigator]", latest["text"])
        self.assertEqual(2, len(self._channel_events("Bob", "crewchat")))

    def test_empty_feed_reveals_membership_change_without_private_text(self):
        self._install()
        self.gateway.command("Alice", "crewchat Previously seen secret", "seen")
        seen = self.gateway.feed("Bob", 0)
        self.assertTrue(any(e["kind"] == "crewchat" for e in seen["events"]))
        old_revision = seen["channel_revision"]
        after = self.gateway.cursor()
        self.roster.revoke("Bob")
        revoked = self.gateway.feed("Bob", after)
        self.assertEqual([], revoked["events"])
        self.assertGreater(revoked["channel_revision"], old_revision)
        self.roster.grant("Bob", "expedition", role="Member")
        regranted = self.gateway.feed("Bob", after)
        self.assertEqual([], regranted["events"])
        self.assertGreater(regranted["channel_revision"], revoked["channel_revision"])

    def test_mixed_room_global_and_private_pages_do_not_skip_events(self):
        self._install()
        start = self.gateway.cursor()
        self.gateway.command("Alice", "say Local one", None)
        self.gateway.command("Alice", "crewchat Secret two", None)
        self.gateway.command("Cara", "ooc Global three", None)
        self.gateway.command("Alice", "emote waves four", None)
        self.gateway.command("Alice", "crewchat Secret five", None)
        self.gateway.command("Alice", "say Local six", None)
        expected = ["say", "crewchat", "ooc", "emote", "crewchat", "say"]
        kinds, seen = [], set()
        cursor = start
        with patch("realm.gateway.MAX_FEED_EVENTS", 2):
            for _ in range(4):
                result = self.gateway.feed("Bob", cursor)
                self.assertLessEqual(len(result["events"]), 2)
                for event in result["events"]:
                    self.assertNotIn(event["id"], seen)
                    self.assertGreater(event["id"], cursor)
                    seen.add(event["id"])
                    kinds.append(event["kind"])
                cursor = result["cursor"]
        self.assertEqual(expected, kinds)
        outsider = self.gateway.feed("Cara", start)["events"]
        self.assertFalse(any(event["kind"] == "crewchat" for event in outsider))

    def test_independent_world_and_group_name_collision_do_not_cross_feed(self):
        self._install()
        with tempfile.TemporaryDirectory() as otherdir:
            other = Gateway(Path(otherdir) / "second.sqlite3", world_id="other_world")
            other.join("Alice")
            other.join("Bob")
            second = ChannelRoster(other.engine.db_path, world_id="other_world")
            second.install()
            second.create_group("expedition", "Expedition")
            second.grant("Bob", "expedition", role="Leader")
            self.gateway.command("Alice", "crewchat Local only", None)
            self.assertEqual([], [x for x in other.feed("Bob", 0)["events"] if x["kind"] == "crewchat"])
            other.command("Bob", "crewchat Other only", None)
            other.command("Bob", "ooc Other world public only", None)
            self.assertNotIn("Other only", str(self._channel_events("Alice", "crewchat")))
            self.assertNotIn("Local only", str(other.feed("Bob", 0)["events"]))
            self.assertNotIn("Other world public only", str(self._events("Alice")),
                             "World-wide is per-world, not cross-world")

    def test_defeated_character_can_chat_but_cannot_mutate_world_until_revival(self):
        self._install()
        Combat.configure(self.gateway.engine, max_hp=4, damage=4,
                         allow_offline_targets=True)
        # Host-trusted writer for this engine-level test; clients still need
        # player-bound HTTP/Telnet credentials to invoke these combat verbs.
        guard = lambda db: None
        self.gateway.engine.act("Alice", "pvp on", action_id="alice-opt", writer_guard=guard)
        self.gateway.engine.act("Bob", "pvp on", action_id="bob-opt", writer_guard=guard)
        self.gateway.engine.act("Alice", "attack Bob", action_id="fatal", writer_guard=guard)
        self.assertEqual('[Expedition][Member] Bob: "Still reporting"',
                         self.gateway.command("Bob", "crewchat Still reporting", "ghost-crew")["text"])
        self.assertEqual('[OOC Channel] Bob: I am down',
                         self.gateway.command("Bob", "ooc I am down", "ghost-ooc")["text"])
        self.assertEqual("Bob waves weakly.",
                         self.gateway.command("Bob", "emote waves weakly.", "ghost-emote")["text"])
        with self.assertRaisesRegex(ValueError, "[Dd]efeated"):
            self.gateway.command("Bob", "north", "ghost-move")
        with self.assertRaisesRegex(ValueError, "[Dd]efeated"):
            self.gateway.command("Bob", "take token", "ghost-take")
        Combat.revive(self.gateway.engine, "Bob")
        self.assertIn("Corridor", self.gateway.command("Bob", "north", "revived-move")["text"])


if __name__ == "__main__":
    unittest.main()
