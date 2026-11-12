"""Optional, setting-neutral political allegiances with durable consequences."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway


class PoliticalOathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        self.g = Gateway(self.path, world_id="oath_world")
        self.g.join("Alice")
        self.g.join("Bob")

    def test_default_world_is_inert_without_optional_politics(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(any(
                row[0].startswith("political_")
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ))
        self.assertEqual("Political allegiances are not enabled.",
                         self.g.command("Alice", "allegiance", None)["text"])
        self.assertEqual("Political allegiances are not enabled.",
                         self.g.command("Alice", "factions", None)["text"])
        self.assertEqual("Political allegiances are not enabled.",
                         self.g.command("Alice", "pledge guild", "no-install")["text"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(any(
                row[0].startswith("political_")
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ))

    def test_shared_token_session_cannot_forge_a_political_oath(self):
        self.assertEqual(
            "Political pledges require player-bound credentials.",
            self.g.engine.act("Alice", "pledge guild", private_channels=False)
        )

    def enable(self):
        from realm.political_oaths import WorldPolitics
        politics = WorldPolitics(self.path, world_id="oath_world")
        politics.install()
        politics.create_faction("north", "Northern League")
        politics.create_faction("south", "Southern Compact")
        return politics

    def event_kinds(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [row[0] for row in db.execute(
                "SELECT kind FROM events WHERE kind LIKE 'political_%' ORDER BY id"
            )]

    def test_host_created_factions_do_not_grant_chat_membership(self):
        self.enable()
        self.assertIn("Northern League (0 pledged)",
                      self.g.command("Alice", "factions", None)["text"])
        first = self.g.command("Alice", "pledge NORTH", "oath-1")["text"]
        self.assertEqual("Alice pledged allegiance to Northern League.", first)
        self.assertIn("Allegiance: Northern League",
                      self.g.command("Alice", "allegiance", None)["text"])
        self.assertIn("Alice: Northern League",
                      self.g.command("Bob", "allegiance Alice", None)["text"])
        self.assertEqual(["political_pledge"], self.event_kinds())
        self.assertIn("No active group channel membership",
                      self.g.command("Alice", "crewchat Pledge confers permission", None)["text"])
        self.assertEqual(["political_pledge"], self.event_kinds())

    def test_public_broken_oath_and_competing_choices_survive_restart(self):
        self.enable()
        self.g.command("Alice", "pledge north", "oath-1")
        self.assertIn("Renounce", self.g.command("Alice", "pledge south", "oath-2")["text"])
        self.assertEqual(["political_pledge"], self.event_kinds())
        self.assertEqual("Alice broke allegiance to Northern League.",
                         self.g.command("Alice", "renounce", "break-1")["text"])
        self.assertEqual("Alice pledged allegiance to Southern Compact.",
                         self.g.command("Alice", "pledge south", "oath-3")["text"])
        self.assertEqual(["political_pledge", "political_break", "political_pledge"],
                         self.event_kinds())
        self.assertIn("broken oaths: 1", self.g.command("Bob", "allegiance Alice", None)["text"])
        restarted = Gateway(self.path, world_id="oath_world")
        self.assertIn("Southern Compact", restarted.command("Alice", "allegiance", None)["text"])
        self.assertIn("broken oaths: 1",
                      restarted.command("Bob", "allegiance Alice", None)["text"])

    def test_replays_are_once_only_and_stale_receipts_do_not_claim_current_power(self):
        self.enable()
        first = self.g.command("Alice", "pledge north", "pledge-1")["text"]
        self.assertEqual(first, self.g.command("Alice", "pledge north", "pledge-1")["text"])
        self.assertEqual(["political_pledge"], self.event_kinds())
        self.assertIn("Already pledged", self.g.command("Alice", "pledge north", "other")["text"])
        self.g.command("Alice", "renounce", "break-1")
        replay = self.g.command("Alice", "pledge north", "pledge-1")["text"]
        self.assertIn("superseded", replay)
        self.assertNotIn("pledged allegiance", replay)
        self.assertEqual(["political_pledge", "political_break"], self.event_kinds())
        self.assertEqual("Already unaffiliated.", self.g.command("Alice", "renounce", None)["text"])
        self.assertIn("superseded", self.g.command("Alice", "renounce", "break-1")["text"])
        self.assertEqual(["political_pledge", "political_break"], self.event_kinds())

    def test_invalid_or_unrecognized_faction_never_mints_a_political_identity(self):
        politics = self.enable()
        for malicious in ("unknown", "north;DROP", "north--", "Southern Compact"):
            self.assertIn("Unknown faction", self.g.command("Alice", "pledge " + malicious, None)["text"])
        with self.assertRaises(ValueError):
            politics.create_faction("..", "Forged")
        with self.assertRaises(ValueError):
            politics.create_faction("ghost", "[King] Forged")
        self.assertIn("Unknown player", self.g.command("Alice", "allegiance ImNobody", None)["text"])
        self.assertEqual([], self.event_kinds())

    def test_wrong_world_host_cannot_install_or_modify_another_world(self):
        from realm.political_oaths import WorldPolitics
        bad = WorldPolitics(self.path, world_id="other_world")
        with self.assertRaises(PermissionError):
            bad.install()
        owner = self.enable()
        with self.assertRaises(PermissionError):
            bad.create_faction("enemy", "False Lord")
        self.assertEqual(["Northern League", "Southern Compact"],
                         [v[0] for v in self._faction_names()])
        self.assertEqual([], self.event_kinds())

    def _faction_names(self):
        with closing(sqlite3.connect(self.path)) as db:
            return list(db.execute("SELECT display_name FROM political_factions ORDER BY id"))

    def test_room_feed_proves_political_pledges_without_global_private_leaks(self):
        self.enable()
        start = self.g.join("Bob")["cursor"]
        self.g.command("Alice", "pledge north", "oath-1")
        feed = self.g.feed("Bob", start)["events"]
        self.assertTrue(any(x["kind"] == "political_pledge" and
                            x["text"] == "Alice pledged allegiance to Northern League."
                            for x in feed))
        self.g.command("Bob", "north", None)
        later = self.g.feed("Bob", 0)["events"]
        self.assertFalse(any(x["kind"] == "political_pledge" for x in later))
        self.assertIn("Northern League",
                      self.g.command("Bob", "allegiance Alice", None)["text"])

    def test_missing_faction_schema_partial_configuration_fails_closed(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE political_factions(id TEXT PRIMARY KEY)")
        with self.assertRaises(RuntimeError):
            self.g.command("Alice", "allegiance", None)

    def test_host_cli_installs_factions_without_starting_a_listener(self):
        from realm.gateway import main as gateway_main
        fresh = self.path.parent / "host_console.sqlite3"
        self.assertEqual(0, gateway_main([
            "--db", str(fresh), "--world-id", "host_console",
            "--install-politics",
            "--political-faction", "lions=Lion Council",
            "--political-faction", "wolves=Wolf Pact",
        ]))
        hosted = Gateway(fresh, world_id="host_console")
        hosted.join("Member")
        self.assertIn("Lion Council (0 pledged)", hosted.command("Member", "factions", None)["text"])
        self.assertIn("Wolf Pact (0 pledged)", hosted.command("Member", "factions", None)["text"])

    def test_cli_invalid_configuration_cannot_install_partial_politics(self):
        from realm.gateway import main as gateway_main
        fresh = self.path.parent / "must_not_create.sqlite3"
        with self.assertRaises(SystemExit):
            gateway_main([
                "--db", str(fresh), "--world-id", "host_console",
                "--political-faction", "wolves=Wolf Pact",
            ])
        self.assertFalse(fresh.exists())
        with self.assertRaises(SystemExit):
            gateway_main([
                "--db", str(fresh), "--world-id", "host_console",
                "--install-politics", "--political-faction", "x;DROP=Forged",
            ])
        self.assertFalse(fresh.exists())


    def test_two_competing_pledges_serialize_to_one_active_oath(self):
        from concurrent.futures import ThreadPoolExecutor
        self.enable()
        other = Gateway(self.path, world_id="oath_world")
        def choose(which):
            return other.command("Alice", "pledge " + which, which + "-id")["text"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(choose, ("north", "south")))
        self.assertEqual(1, sum("pledged allegiance" in x for x in results))
        self.assertEqual(1, sum("Renounce" in x for x in results))
        self.assertEqual(["political_pledge"], self.event_kinds())

    def test_failed_event_append_rolls_back_oath_and_action_receipt(self):
        self.enable()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TRIGGER deny_political_event BEFORE INSERT ON events "
                "WHEN NEW.kind='political_pledge' "
                "BEGIN SELECT RAISE(ABORT,'blocked political event'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.g.command("Alice", "pledge north", "one-atomic-write")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM political_oaths").fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM action_receipts WHERE action_id='one-atomic-write'"
            ).fetchone()[0])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER deny_political_event")
        self.assertEqual("Alice pledged allegiance to Northern League.",
                         self.g.command("Alice", "pledge north", "one-atomic-write")["text"])
        self.assertEqual(["political_pledge"], self.event_kinds())



if __name__ == "__main__":
    unittest.main()
