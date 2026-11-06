"""Loopback gateway integration/security tests using only Python stdlib."""

import json
import http.client
import sqlite3
from contextlib import closing
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
        self.player_tokens = {
            name: self.gateway.credentials.provision(name)
            for name in ("Patrick", "Vera")
        }
        self.server = GatewayServer(
            self.gateway, ("http://127.0.0.1:8000",), port=0
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

    def send(self, path, obj=None, *, token=True, origin=None, method=None, token_player=None):
        data = json.dumps(obj).encode() if obj is not None else None
        headers = {}
        if obj is not None:
            headers["Content-Type"] = "application/json"
        if token:
            requested = token_player
            if requested is None and isinstance(obj, dict):
                requested = obj.get("player")
            if requested is None and "?" in path:
                requested = parse.parse_qs(parse.urlsplit(path).query).get("player", [None])[0]
            requested = requested if requested in self.player_tokens else "Patrick"
            headers["Authorization"] = "Bearer " + self.player_tokens[requested]
        if origin:
            headers["Origin"] = origin
        req = request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read() or b"{}")
        except error.HTTPError as exc:
            with closing(exc):
                return exc.code, json.loads(exc.read())

    def test_health_and_authentication(self):
        self.assertEqual((200, {"ok": True, "world_id": "sandbox"}), self.send("/api/v1/health", token=False))
        status, body = self.send("/api/v1/feed?player=Patrick", token=False)
        self.assertEqual(401, status)
        self.assertFalse(body["ok"])

    def test_denied_post_closes_connection_before_unread_body_can_be_reused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        body = json.dumps({"player": "Patrick"})
        conn.request("POST", "/api/v1/join", body=body, headers={
            "Authorization": "Bearer invalid-token",
            "Content-Type": "application/json",
        })
        denied = conn.getresponse()
        self.assertEqual(401, denied.status)
        self.assertEqual("close", denied.getheader("Connection"))
        self.assertFalse(json.loads(denied.read())["ok"])
        # A subsequent valid request must not parse the previous JSON body as
        # an HTTP request line on a pooled keep-alive connection.
        conn.request("POST", "/api/v1/join", body=body, headers={
            "Authorization": "Bearer " + self.player_tokens["Patrick"],
            "Content-Type": "application/json",
        })
        accepted = conn.getresponse()
        self.assertEqual(200, accepted.status)
        self.assertTrue(json.loads(accepted.read())["ok"])

    def _install_generic_map(self):
        geo = self.gateway.geography
        geo.add_place("demo_universe", "Demo universe", "universe")
        geo.add_place("demo_system", "Demo system", "system", "demo_universe")
        geo.add_place("demo_planet", "Demo planet", "planet", "demo_system")
        geo.add_place("demo_region", "Demo region", "region", "demo_planet")
        geo.add_place("demo_town", "Demo town", "settlement", "demo_region")
        geo.add_place("atrium", "Demo atrium", "room", "demo_town", x=0, y=0)
        geo.add_place("corridor", "Demo corridor", "room", "demo_town", x=2, y=0)
        geo.set_map("demo_town", [".^.", "..~"])

    def test_map_endpoint_does_not_invent_unconfigured_world(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        status, payload = self.send("/api/v1/map?player=Patrick", token=False)
        self.assertEqual(401, status)
        status, payload = self.send("/api/v1/map?player=Patrick")
        self.assertEqual(404, status)
        self.assertIn("No map is configured", payload["error"])
        status, payload = self.send("/api/v1/map?player=NotJoined")
        self.assertEqual(403, status)

    def test_map_endpoint_reads_actual_player_room_and_moves(self):
        self._install_generic_map()
        self.send("/api/v1/join", {"player": "Patrick"})
        status, payload = self.send("/api/v1/map?player=Patrick")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("demo_town", payload["space"]["id"])
        self.assertEqual({"x": 0, "y": 0}, payload["player"])
        self.assertEqual("forest", payload["tiles"][1]["terrain"])
        self.send("/api/v1/command", {"player": "Patrick", "command": "north"})
        status, payload = self.send("/api/v1/map?player=Patrick")
        self.assertEqual(200, status)
        self.assertEqual({"x": 2, "y": 0}, payload["player"])
        self.assertEqual(["atrium", "corridor"], [item["id"] for item in payload["markers"]])

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
        self.assertEqual(403, status)
        status, _ = self.send("/api/v1/feed?player=Patrick&after=-1")
        self.assertEqual(400, status)
        status, _ = self.send("/api/v1/command", {"player": "Patrick", "command": "a" * 513})
        self.assertEqual(400, status)
        status, _ = self.send("/api/v1/join", {"player": "../admin"})
        self.assertEqual(403, status)

    def test_restart_reads_same_persisted_state(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        self.send("/api/v1/command", {"player": "Patrick", "command": "north"})
        other = Gateway(self.db, world_id="sandbox")
        self.assertIn("Corridor", other.room("Patrick")["text"])

    def test_concurrent_first_join_one_identity_is_stable(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: Gateway(self.db, world_id="sandbox").join("Patrick"), range(2)))
        self.assertEqual(2, len(results))
        self.assertEqual(1, sum("Welcome, Patrick." in item["text"] for item in results))
        self.assertEqual(1, sum("Welcome back, Patrick." in item["text"] for item in results))

    def test_player_bound_token_denies_cross_player_commands(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        self.send("/api/v1/join", {"player": "Vera"})
        status, result = self.send(
            "/api/v1/command",
            {"player": "Vera", "command": "say Impersonated", "action_id": "wrong-actor"},
            token_player="Patrick",
        )
        self.assertEqual(403, status)
        self.assertFalse(result["ok"])
        _, feed = self.send("/api/v1/feed?player=Vera&after=0", token_player="Vera")
        self.assertFalse(any("Impersonated" in e["text"] for e in feed["events"]))

    def test_player_bound_token_denies_cross_player_feed(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        self.send("/api/v1/join", {"player": "Vera"})
        self.send("/api/v1/command", {"player": "Vera", "command": "north"})
        self.send("/api/v1/command", {"player": "Vera", "command": "say Private room"})
        status, _ = self.send("/api/v1/feed?player=Vera&after=0", token_player="Patrick")
        self.assertEqual(403, status)
        _, private = self.send("/api/v1/feed?player=Vera&after=0", token_player="Vera")
        self.assertTrue(any("Private room" in e["text"] for e in private["events"]))

    def test_movement_notifies_old_and_new_room_audiences(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        self.send("/api/v1/join", {"player": "Vera"})
        _, initial = self.send("/api/v1/join", {"player": "Patrick"})
        self.send("/api/v1/command", {"player": "Vera", "command": "north"})
        _, old_room = self.send(f"/api/v1/feed?player=Patrick&after={initial['cursor']}")
        self.assertTrue(any(e["actor"] == "Vera" and e["kind"] == "leave" for e in old_room["events"]))
        _, new_room = self.send("/api/v1/feed?player=Vera&after=0")
        self.assertTrue(any(e["actor"] == "Vera" and e["kind"] == "move" for e in new_room["events"]))

    def test_credential_rotation_revoke_and_no_plaintext_storage(self):
        first = self.player_tokens["Patrick"]
        with closing(sqlite3.connect(self.db)) as connection, connection:
            stored = connection.execute("SELECT token_digest FROM player_credentials WHERE player_name='Patrick'").fetchone()[0]
        self.assertNotEqual(first, stored)
        self.assertNotIn(first, stored)
        with self.assertRaises(ValueError):
            self.gateway.credentials.provision("Patrick")
        rotated = self.gateway.credentials.rotate("patrick")
        self.assertIsNone(Gateway(self.db, world_id="sandbox").credentials.authenticate(first))
        self.assertEqual("Patrick", Gateway(self.db, world_id="sandbox").credentials.authenticate(rotated))
        self.assertTrue(self.gateway.credentials.revoke("PATRICK"))
        self.assertIsNone(Gateway(self.db, world_id="sandbox").credentials.authenticate(rotated))

    def test_shared_process_secret_no_longer_authorizes_gameplay(self):
        self.send("/api/v1/join", {"player": "Patrick"})
        headers = {"Authorization": "Bearer unit-test-secret", "Content-Type": "application/json"}
        req = request.Request(
            self.base + "/api/v1/command",
            data=json.dumps({"player": "Patrick", "command": "say forged"}).encode(),
            headers=headers,
        )
        with self.assertRaises(error.HTTPError) as raised:
            request.urlopen(req, timeout=3)
        self.assertEqual(401, raised.exception.code)
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()

