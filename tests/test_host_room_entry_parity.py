"""Cross-PR regression parity from source PR #189 exact f8c0d7b.

Retains six original source tests; replaces private station character name
with a generic newcomer. Test evidence is not a code merge or reviewer approval.
"""
"""REALM actor/room entry floor: real Engine and Gateway, disposable SQLite."""
from pathlib import Path
import sys
from contextlib import closing
from tempfile import TemporaryDirectory
import sqlite3
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.gateway import Gateway


class RoomEntryPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory(prefix="realm-issue187-")
        self.addCleanup(self.scratch.cleanup)
        self.db = Path(self.scratch.name) / "world.sqlite3"
        self.host = Gateway(self.db, world_id="privacy-probe")
        self.engine = self.host.engine

    def test_new_entrant_cannot_read_prior_speech_from_history_or_feed(self):
        self.host.join("Visitor")
        self.host.command("Visitor", "say BEFORE NEWCOMER JOINED", None)
        self.host.join("Newcomer")
        history = self.host.command("Newcomer", "history 50", None)["text"]
        events = self.host.feed("Newcomer", 0)["events"]
        self.assertNotIn("BEFORE NEWCOMER JOINED", history)
        self.assertFalse(any("BEFORE NEWCOMER JOINED" in e["text"] for e in events))
        self.host.command("Visitor", "say AFTER NEWCOMER JOINED", None)
        self.assertIn("AFTER NEWCOMER JOINED", self.host.command("Newcomer", "history 50", None)["text"])
        self.assertTrue(any("AFTER NEWCOMER JOINED" in e["text"] for e in self.host.feed("Newcomer", 0)["events"]))

    def test_returning_actor_cannot_read_speech_from_before_reentry(self):
        self.host.join("Visitor")
        self.host.join("Newcomer")
        self.host.command("Newcomer", "north", None)
        self.host.command("Visitor", "say SECRET WHILE AWAY", None)
        self.host.command("Newcomer", "south", None)
        self.assertNotIn("SECRET WHILE AWAY", self.host.command("Newcomer", "listen", None)["text"])
        self.assertFalse(any("SECRET WHILE AWAY" in x["text"] for x in self.host.feed("Newcomer", 0)["events"]))
        self.host.command("Visitor", "say NEW AFTER REENTRY", None)
        self.assertIn("NEW AFTER REENTRY", self.host.command("Newcomer", "history 50", None)["text"])

    def test_move_entry_event_kept_for_current_room_feed(self):
        self.host.join("Newcomer")
        self.host.command("Newcomer", "north", None)
        entries = self.host.feed("Newcomer", 0)["events"]
        self.assertTrue(any(e["actor"] == "Newcomer" and e["kind"] == "move" for e in entries))
        self.assertFalse(any(e["room"] != "corridor" for e in entries))

    def test_missing_entry_ledger_fails_closed_without_secret_exposure(self):
        self.host.join("Visitor")
        self.host.command("Visitor", "say PREEXISTING SECRET", None)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO players(id,room_id,created_at) VALUES(?,?,?)",
                ("Legacy", "atrium", "2026-11-11T00:00:00+00:00"),
            )
        # Integrated PR #188 is stronger than source PR #189: a missing
        # trusted entry event raises rather than returning an empty success.
        with self.assertRaisesRegex(ValueError, "No provable current-room arrival"):
            self.host.command("Legacy", "history 50", None)
        with self.assertRaisesRegex(ValueError, "No provable current-room arrival"):
            self.host.feed("Legacy", 0)


    def test_http_authenticated_feed_enforces_same_entry_floor(self):
        from threading import Thread
        from urllib.request import Request, urlopen
        from realm.gateway import GatewayServer
        import json

        visitor_token = self.host.credentials.provision("Visitor")
        captain_token = self.host.credentials.provision("Newcomer")
        self.host.join("Visitor")
        self.host.command("Visitor", "say PRIVATE BEFORE ENTRY", None)
        self.host.join("Newcomer")
        server = GatewayServer(self.host, (), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def read(token, cursor):
                url = f"http://127.0.0.1:{server.server_port}/api/v1/feed?after={cursor}"
                req = Request(url, headers={"Authorization": "Bearer " + token})
                with urlopen(req, timeout=4) as response:
                    return json.loads(response.read())
            first = read(captain_token, 0)
            self.assertFalse(any("PRIVATE BEFORE ENTRY" in e["text"] for e in first["events"]))
            self.host.command("Visitor", "say PUBLIC AFTER ENTRY", None)
            second = read(captain_token, 0)
            self.assertTrue(any("PUBLIC AFTER ENTRY" in e["text"] for e in second["events"]))
            self.assertFalse(any("PRIVATE BEFORE ENTRY" in e["text"] for e in second["events"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_legacy_actor_can_resume_after_new_real_room_entry(self):
        self.host.join("Visitor")
        self.host.command("Visitor", "say OLD NOT VISIBLE", None)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO players(id,room_id,created_at) VALUES(?,?,?)",
                ("Legacy", "atrium", "2026-11-11T00:00:00+00:00"),
            )
        with self.assertRaisesRegex(ValueError, "No provable current-room arrival"):
            self.host.feed("Legacy", 0)
        self.host.command("Legacy", "north", None)
        self.host.command("Legacy", "south", None)
        self.assertNotIn("OLD NOT VISIBLE", self.host.command("Legacy", "history 50", None)["text"])
        self.host.command("Visitor", "say POST ENTRY", None)
        self.assertIn("POST ENTRY", self.host.command("Legacy", "history 50", None)["text"])

    def test_returning_login_cannot_read_messages_from_prior_session(self):
        self.host.join("Visitor")
        self.host.join("Newcomer")
        self.host.command("Visitor", "say SECRET BEFORE RETURN", None)
        returned = self.host.join("Newcomer")
        self.assertIn("Welcome back, Newcomer.", returned["text"])
        self.assertNotIn("SECRET BEFORE RETURN", self.host.command(
            "Newcomer", "history 50", None,
        )["text"])
        self.assertFalse(any("SECRET BEFORE RETURN" in e["text"]
                             for e in self.host.feed("Newcomer", 0)["events"]))
        self.host.command("Visitor", "say POST RETURN", None)
        self.assertIn("POST RETURN", self.host.command(
            "Newcomer", "history 50", None,
        )["text"])

    def test_returning_login_preserves_room_position_and_owned_inventory(self):
        self.host.join("Newcomer")
        self.host.command("Newcomer", "take token", None)
        self.host.command("Newcomer", "north", None)
        returned = self.host.join("Newcomer")
        self.assertIn("Welcome back, Newcomer.", returned["text"])
        self.assertIn("The Corridor", self.host.command(
            "Newcomer", "look", None,
        )["text"])
        self.assertIn("Brass token", self.host.command(
            "Newcomer", "inventory", None,
        )["text"])
        self.assertTrue(any(
            e["actor"] == "Newcomer" and e["kind"] == "join"
            for e in self.host.feed("Newcomer", 0)["events"]
        ))


if __name__ == "__main__":
    unittest.main()
