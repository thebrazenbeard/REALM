"""REALM security acceptance probes for the provisional HTTP gateway.

The expected failures below are documented blockers, NOT successful security
qualification. Remove ``expectedFailure`` only after identity-bound sessions
and audience-correct movement events are implemented and tested independently.

Designed to run against the PR #2 gateway branch with Python stdlib only.
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
        self.server = GatewayServer(
            self.gateway,
            "review-shared-secret",
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

    def _call(self, route, body=None, *, token=True, origin=None):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer review-shared-secret"
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

    @unittest.expectedFailure
    def test_player_session_cannot_command_as_another_player(self):
        # RED: the same gateway bearer currently lets a caller select any name.
        self._call("/api/v1/join", {"player": "Patrick"})
        self._call("/api/v1/join", {"player": "Vera"})
        status, _ = self._call(
            "/api/v1/command",
            {"player": "Vera", "command": "say Forged by another client.", "action_id": "forged-1"},
        )
        self.assertIn(status, (401, 403))

    @unittest.expectedFailure
    def test_player_session_cannot_observe_another_players_room(self):
        # RED: caller possession of a world-wide token grants feed access for
        # any known player name, including rooms that caller is not in.
        self._call("/api/v1/join", {"player": "Patrick"})
        self._call("/api/v1/join", {"player": "Vera"})
        self._call("/api/v1/command", {"player": "Vera", "command": "north"})
        self._call("/api/v1/command", {"player": "Vera", "command": "say Private room"})
        status, response = self._call("/api/v1/feed?player=Vera&after=0")
        self.assertIn(status, (401, 403))
        self.assertFalse(any("Private room" in e.get("text", "") for e in response.get("events", [])))

    @unittest.expectedFailure
    def test_departure_is_visible_to_players_remaining_in_old_room(self):
        # RED: only destination arrival is recorded. A stale presence display
        # will not know the moving player left without a separate fresh look.
        self.gateway.join("Patrick")
        self.gateway.join("Vera")
        self.gateway.command("Vera", "north", "move-1")
        events = self.gateway.feed("Patrick", after=0)["events"]
        self.assertTrue(any(e.get("kind") == "leave" and e.get("actor") == "Vera" for e in events))


if __name__ == "__main__":
    unittest.main()
