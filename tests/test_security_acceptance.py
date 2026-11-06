"""REALM security regression probes for player-bound local HTTP sessions.

These are development acceptance tests, not independent production-security
qualification. Network exposure, token expiry and fog of war remain blocked.
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


class SecurityAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = Gateway(Path(self.temp.name) / "review.sqlite3")
        self.player_tokens = {
            name: self.gateway.credentials.provision(name)
            for name in ("Patrick", "Vera")
        }
        self.server = GatewayServer(
            self.gateway,
            ("http://127.0.0.1:8000",),
            port=0,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _call(self, route, body=None, *, token=True, origin=None, token_player="Patrick"):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + self.player_tokens[token_player]
        if origin is not None:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(self.base + route, payload, headers=headers)
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except error.HTTPError as exception:
            return exception.code, json.load(exception)

    def test_gateway_binds_only_to_loopback(self):
        self.assertEqual("127.0.0.1", self.server.server_address[0])

    def test_rejects_anonymous_world_mutation(self):
        status, _ = self._call("/api/v1/join", {"player": "Patrick"}, token=False)
        self.assertEqual(401, status)

    def test_rejects_unapproved_browser_origin_even_with_bearer(self):
        status, _ = self._call(
            "/api/v1/join", {"player": "Patrick"}, origin="https://not-approved.example"
        )
        self.assertEqual(403, status)

    def test_player_session_cannot_command_as_another_player(self):
        # Patrick cannot act as Vera even when he knows her displayed name.
        self._call("/api/v1/join", {"player": "Patrick"})
        self._call("/api/v1/join", {"player": "Vera"}, token_player="Vera")
        status, _ = self._call(
            "/api/v1/command",
            {"player": "Vera", "command": "say Forged by another client.", "action_id": "forged-1"},
        )
        self.assertIn(status, (401, 403))

    def test_player_session_cannot_observe_another_players_room(self):
        # Patrick's valid token must not fetch Vera's separate room activity.
        self._call("/api/v1/join", {"player": "Patrick"})
        self._call("/api/v1/join", {"player": "Vera"}, token_player="Vera")
        self._call("/api/v1/command", {"player": "Vera", "command": "north"}, token_player="Vera")
        self._call("/api/v1/command", {"player": "Vera", "command": "say Private room"}, token_player="Vera")
        status, response = self._call("/api/v1/feed?player=Vera&after=0")
        self.assertIn(status, (401, 403))
        self.assertFalse(any("Private room" in e.get("text", "") for e in response.get("events", [])))

    def test_departure_is_visible_to_players_remaining_in_old_room(self):
        # A move records source-room departure and destination-room arrival.
        self.gateway.join("Patrick")
        self.gateway.join("Vera")
        self.gateway.command("Vera", "north", "move-1")
        events = self.gateway.feed("Patrick", after=0)["events"]
        self.assertTrue(any(e.get("kind") == "leave" and e.get("actor") == "Vera" for e in events))


if __name__ == "__main__":
    unittest.main()
