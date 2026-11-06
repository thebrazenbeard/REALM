"""Loopback gateway integration/security tests using only Python stdlib."""

import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "world.sqlite3"
        self.gateway = Gateway(self.db, world_id="sandbox")
        self.server = GatewayServer(
            self.gateway, "unit-test-secret", ("http://127.0.0.1:8000",), port=0
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def send(self, path, obj=None, *, token=True, origin=None, method=None):
        data = json.dumps(obj).encode() if obj is not None else None
        headers = {}
        if obj is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer unit-test-secret"
        if origin:
            headers["Origin"] = origin
        req = request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read() or b"{}")
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_and_authentication(self):
        self.assertEqual((200, {"ok": True, "world_id": "sandbox"}), self.send("/api/v1/health", token=False))
        status, body = self.send("/api/v1/feed?player=Patrick", token=False)
        self.assertEqual(401, status)
        self.assertFalse(body["ok"])

    def test_exact_origin_and_preflight(self):
        status, _ = self.send("/api/v1/health", token=False, origin="https://evil.example")
        self.assertEqual(403, status)
        status, _ = self.send("/api/v1/health", token=False, origin="http://127.0.0.1:8000")
        self.assertEqual(200, status)
        status, _ = self.send("/api/v1/feed", origin="https://evil.example")
        self.assertEqual(403, status)
        status, _ = self.send("/api/v1/feed", origin="http://127.0.0.1:8000", method="OPTIONS")
        self.assertEqual(204, status)

    def test_join_command_and_incremental_room_feed(self):
        status, joined = self.send("/api/v1/join", {"player": "Patrick"})
        self.assertEqual(200, status)
        self.assertIn("Welcome", joined["text"])
        self.send("/api/v1/join", {"player": "Vera"})
        after = joined["cursor"]
        status, spoken = self.send(
            "/api/v1/command",
            {"player": "Vera", "command": "say Testing shared worlds.", "action_id": "say-1"},
        )
        self.assertEqual(200, status)
        self.assertIn("You say", spoken["text"])
        query = parse.urlencode({"player": "Patrick", "after": after})
        status, feed = self.send(f"/api/v1/feed?{query}")
        self.assertEqual(200, status)
        self.assertTrue(any("Testing shared worlds" in event["text"] for event in feed["events"]))
        self.assertTrue(all(event["room"] == "atrium" for event in feed["events"]))
        status, refreshed = self.send("/api/v1/room?player=Patrick")
        self.assertEqual(200, status)
        self.assertIn("Atrium", refreshed["text"])
        status, repeated = self.send(
            "/api/v1/command",
            {"player": "Vera", "command": "say Testing shared worlds.", "action_id": "say-1"},
        )
        self.assertEqual(200, status)
        self.assertEqual(spoken["text"], repeated["text"])
        # The replay did not append another speech event.
        _, repeated_feed = self.send(f"/api/v1/feed?{query}")
        matching = [
            event for event in repeated_feed["events"]
            if "Testing shared worlds" in event["text"]
        ]
        self.assertEqual(1, len(matching))

    def test_feed_player_scope_and_invalid_inputs(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        self.send("/api/v1/join", {"player": "Vera"})
        self.send("/api/v1/command", {"player": "Vera", "command": "north"})
        self.send("/api/v1/command", {"player": "Vera", "command": "say Hidden corridor."})
        _, feed = self.send("/api/v1/feed?player=Patrick&after=0")
        self.assertFalse(any("Hidden corridor" in event["text"] for event in feed["events"]))
        status, _ = self.send("/api/v1/feed?player=NotJoined&after=0")
        self.assertEqual(400, status)
        status, _ = self.send("/api/v1/feed?player=Patrick&after=-1")
        self.assertEqual(400, status)
        status, _ = self.send("/api/v1/command", {"player": "Patrick", "command": "a" * 513})
        self.assertEqual(400, status)
        status, _ = self.send("/api/v1/join", {"player": "../admin"})
        self.assertEqual(400, status)

    def test_restart_reads_same_persisted_state(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        self.send("/api/v1/command", {"player": "Patrick", "command": "north"})
        other = Gateway(self.db)
        self.assertIn("Corridor", other.room("Patrick")["text"])

    def test_concurrent_first_join_one_identity_is_stable(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: Gateway(self.db).join("Patrick"), range(2)))
        self.assertEqual(2, len(results))
        self.assertEqual(1, sum("Welcome, Patrick." in item["text"] for item in results))
        self.assertEqual(1, sum("Welcome back, Patrick." in item["text"] for item in results))


if __name__ == "__main__":
    unittest.main()
