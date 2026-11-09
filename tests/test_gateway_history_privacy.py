"""Hostile event-time privacy tests for the default HTTP feed.

The optional long-poll handler already protects audience entry time.  The
plain GatewayServer must enforce the same boundary for cursor=0 clients.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer


class DefaultFeedHistoryPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = Gateway(Path(self.temp.name) / "world.sqlite3", world_id="privacy")
        self.tokens = {
            name: self.gateway.credentials.provision(name)
            for name in ("Alice", "Bob", "Cara")
        }
        self.server = GatewayServer(self.gateway, (), port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def http(self, actor: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {self.tokens[actor]}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=data, headers=headers,
        )
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def join(self, actor: str) -> None:
        self.assertEqual(200, self.http(actor, "/api/v1/join", {"player": actor})[0])

    def act(self, actor: str, command: str, action_id: str) -> None:
        self.assertEqual(200, self.http(actor, "/api/v1/command", {
            "player": actor, "command": command, "action_id": action_id,
        })[0])

    def feed(self, actor: str, after: int = 0) -> dict:
        status, payload = self.http(actor, f"/api/v1/feed?player={actor}&after={after}")
        self.assertEqual(200, status)
        return payload

    def test_newcomer_must_not_see_earlier_current_room_speech(self) -> None:
        self.join("Alice")
        self.act("Alice", "say A private conversation before Bob joined", "before-bob")
        self.join("Bob")
        events = self.feed("Bob", 0)["events"]
        self.assertFalse(any("private conversation" in e["text"] for e in events))
        self.assertFalse(any(e["actor"] == "Alice" and e["kind"] == "join" for e in events))
        self.assertTrue(any(e["actor"] == "Bob" and e["kind"] == "join" for e in events))
        self.act("Alice", "say A public conversation after Bob joined", "after-bob")
        self.assertTrue(any("public conversation" in e["text"] for e in self.feed("Bob", 0)["events"]))

    def test_reentry_revokes_prior_room_history_even_with_cursor_zero(self) -> None:
        self.join("Alice")
        self.join("Bob")
        self.act("Bob", "north", "bob-depart")
        self.act("Alice", "say Atrium while Bob was away", "during-bob-absence")
        self.act("Bob", "south", "bob-return")
        events = self.feed("Bob", 0)["events"]
        self.assertFalse(any("while Bob was away" in e["text"] for e in events))
        self.assertFalse(any(e["actor"] == "Bob" and e["kind"] == "join" for e in events))
        self.assertTrue(any(e["actor"] == "Bob" and e["kind"] == "move" for e in events))
        self.act("Alice", "say After Bob returned", "after-return")
        self.assertTrue(any("After Bob returned" in e["text"] for e in self.feed("Bob", 0)["events"]))

    def test_denied_other_player_and_world_never_reveals_events(self) -> None:
        self.join("Alice")
        self.act("Alice", "say First world secret", "world-secret")
        self.join("Bob")
        status, _ = self.http("Bob", "/api/v1/feed?player=Alice&after=0")
        self.assertEqual(403, status)
        other = Gateway(Path(self.temp.name) / "second.sqlite3", world_id="second")
        other.join("Bob")
        self.assertFalse(any("First world secret" in e["text"] for e in other.feed("Bob", 0)["events"]))


if __name__ == "__main__":
    unittest.main()
