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
        self.host.command("Visitor", "say BEFORE RYLOK JOINED", None)
        self.host.join("Rylok")
        history = self.host.command("Rylok", "history 50", None)["text"]
        events = self.host.feed("Rylok", 0)["events"]
        self.assertNotIn("BEFORE RYLOK JOINED", history)
        self.assertFalse(any("BEFORE RYLOK JOINED" in e["text"] for e in events))
        self.host.command("Visitor", "say AFTER RYLOK JOINED", None)
        self.assertIn("AFTER RYLOK JOINED", self.host.command("Rylok", "history 50", None)["text"])
        self.assertTrue(any("AFTER RYLOK JOINED" in e["text"] for e in self.host.feed("Rylok", 0)["events"]))

    def test_returning_actor_cannot_read_speech_from_before_reentry(self):
        self.host.join("Visitor")
        self.host.join("Rylok")
        self.host.command("Rylok", "north", None)
        self.host.command("Visitor", "say SECRET WHILE AWAY", None)
        self.host.command("Rylok", "south", None)
        self.assertNotIn("SECRET WHILE AWAY", self.host.command("Rylok", "listen", None)["text"])
        self.assertFalse(any("SECRET WHILE AWAY" in x["text"] for x in self.host.feed("Rylok", 0)["events"]))
        self.host.command("Visitor", "say NEW AFTER REENTRY", None)
        self.assertIn("NEW AFTER REENTRY", self.host.command("Rylok", "history 50", None)["text"])

    def test_move_entry_event_kept_for_current_room_feed(self):
        self.host.join("Rylok")
        self.host.command("Rylok", "north", None)
        entries = self.host.feed("Rylok", 0)["events"]
        self.assertTrue(any(e["actor"] == "Rylok" and e["kind"] == "move" for e in entries))
        self.assertFalse(any(e["room"] != "corridor" for e in entries))

    def test_returning_login_does_not_read_messages_while_disconnected(self):
        """An authenticated rejoin must establish a fresh room visibility floor."""
        self.host.join("Visitor")
        self.host.join("Rylok")
        self.host.command("Visitor", "say SECRET WHILE RYLOK DISCONNECTED", None)
        result = self.host.join("Rylok")
        self.assertIn("Welcome back, Rylok.", result["text"])
        self.assertNotIn(
            "SECRET WHILE RYLOK DISCONNECTED",
            self.host.command("Rylok", "history 50", None)["text"],
        )
        self.assertFalse(any(
            "SECRET WHILE RYLOK DISCONNECTED" in e["text"]
            for e in self.host.feed("Rylok", 0)["events"]
        ))
        self.host.command("Visitor", "say POST RECONNECT", None)
        self.assertIn("POST RECONNECT", self.host.command("Rylok", "history 50", None)["text"])

    def test_reconnect_records_new_entry_without_resetting_position_or_inventory(self):
        self.host.join("Rylok")
        self.host.command("Rylok", "take token", None)
        self.host.command("Rylok", "north", None)
        self.host.join("Rylok")
        self.assertIn("The Corridor", self.host.command("Rylok", "look", None)["text"])
        self.assertIn("Brass token", self.host.command("Rylok", "inventory", None)["text"])
        self.assertTrue(any(
            e["actor"] == "Rylok" and e["kind"] == "join"
            for e in self.host.feed("Rylok", 0)["events"]
        ))

    def test_join_reports_room_entry_cursor_even_if_speech_interleaves(self):
        from unittest.mock import patch

        self.host.join("Visitor")
        original_cursor = self.host.cursor

        def speech_while_sampling_cursor():
            self.engine.act("Visitor", "say BETWEEN JOIN AND CURSOR")
            return original_cursor()

        with patch.object(self.host, "cursor", side_effect=speech_while_sampling_cursor):
            joined = self.host.join("Rylok")
        self.assertIn("room_entry_cursor", joined)
        self.assertLess(joined["room_entry_cursor"], joined["cursor"])
        self.assertTrue(any(
            "BETWEEN JOIN AND CURSOR" in event["text"]
            for event in self.host.feed("Rylok", joined["room_entry_cursor"])["events"]
        ), "Post-entry speech must remain visible with the room-entry cursor")

    def test_move_reports_room_entry_cursor_even_if_speech_interleaves(self):
        from unittest.mock import patch

        self.host.join("Visitor")
        self.host.command("Visitor", "north", None)
        self.host.join("Rylok")
        original_cursor = self.host.cursor

        def speech_while_sampling_cursor():
            self.engine.act("Visitor", "say BETWEEN MOVE AND CURSOR")
            return original_cursor()

        with patch.object(self.host, "cursor", side_effect=speech_while_sampling_cursor):
            moved = self.host.command("Rylok", "north", None)
        self.assertIn("The Corridor", moved["text"])
        self.assertIn("room_entry_cursor", moved)
        self.assertLess(moved["room_entry_cursor"], moved["cursor"])
        self.assertTrue(any(
            "BETWEEN MOVE AND CURSOR" in event["text"]
            for event in self.host.feed("Rylok", moved["room_entry_cursor"])["events"]
        ))

    def test_authenticated_http_join_and_move_publish_room_entry_cursor(self):
        from threading import Thread
        from urllib.request import Request, urlopen
        from realm.gateway import GatewayServer
        import json

        token = self.host.credentials.provision("Rylok")
        server = GatewayServer(self.host, (), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(path, body):
                req = Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=4) as response:
                    return json.loads(response.read())

            joined = post("/api/v1/join", {"player": "Rylok"})
            self.assertIsInstance(joined["room_entry_cursor"], int)
            self.assertLessEqual(joined["room_entry_cursor"], joined["cursor"])
            moved = post("/api/v1/command", {"player": "Rylok", "command": "north"})
            self.assertIn("The Corridor", moved["text"])
            self.assertIsInstance(moved["room_entry_cursor"], int)
            self.assertGreater(moved["room_entry_cursor"], joined["room_entry_cursor"])
            self.assertLessEqual(moved["room_entry_cursor"], moved["cursor"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_inconsistent_player_room_and_latest_entry_fails_closed(self):
        """A missing host entry event must not resurrect an older room's history."""
        self.host.join("Visitor")
        self.host.join("Rylok")
        self.host.command("Rylok", "north", None)
        self.host.command("Visitor", "say PRIVATE WHILE RYLOK WAS AWAY", None)
        # Simulate a future plugin or administrator changing only the player
        # location without a host-recorded move back to the original room.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE players SET room_id='atrium' WHERE id='Rylok'")
        self.assertNotIn(
            "PRIVATE WHILE RYLOK WAS AWAY",
            self.host.command("Rylok", "history 50", None)["text"],
        )
        self.assertFalse(any(
            "PRIVATE WHILE RYLOK WAS AWAY" in event["text"]
            for event in self.host.feed("Rylok", 0)["events"]
        ))
        # A genuine subsequent departure and re-entry should restore access
        # to future room events, but never to the earlier secret.
        self.host.command("Rylok", "north", None)
        self.host.command("Rylok", "south", None)
        self.host.command("Visitor", "say AFTER TRUSTED REENTRY", None)
        history = self.host.command("Rylok", "history 50", None)["text"]
        self.assertIn("AFTER TRUSTED REENTRY", history)
        self.assertNotIn("PRIVATE WHILE RYLOK WAS AWAY", history)

    def test_missing_entry_ledger_fails_closed_without_secret_exposure(self):
        self.host.join("Visitor")
        self.host.command("Visitor", "say PREEXISTING SECRET", None)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO players(id,room_id,created_at) VALUES(?,?,?)",
                ("Legacy", "atrium", "2026-11-11T00:00:00+00:00"),
            )
        self.assertNotIn("PREEXISTING SECRET", self.host.command("Legacy", "history 50", None)["text"])
        self.assertFalse(any("PREEXISTING SECRET" in e["text"] for e in self.host.feed("Legacy", 0)["events"]))


    def test_http_authenticated_feed_enforces_same_entry_floor(self):
        from threading import Thread
        from urllib.request import Request, urlopen
        from realm.gateway import GatewayServer
        import json

        visitor_token = self.host.credentials.provision("Visitor")
        captain_token = self.host.credentials.provision("Rylok")
        self.host.join("Visitor")
        self.host.command("Visitor", "say PRIVATE BEFORE ENTRY", None)
        self.host.join("Rylok")
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
        self.assertEqual([], self.host.feed("Legacy", 0)["events"])
        self.host.command("Legacy", "north", None)
        self.host.command("Legacy", "south", None)
        self.assertNotIn("OLD NOT VISIBLE", self.host.command("Legacy", "history 50", None)["text"])
        self.host.command("Visitor", "say POST ENTRY", None)
        self.assertIn("POST ENTRY", self.host.command("Legacy", "history 50", None)["text"])

if __name__ == "__main__":
    unittest.main()
