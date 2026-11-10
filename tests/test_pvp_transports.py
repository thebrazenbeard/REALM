"""PvP must remain identity-bound across real HTTP/Telnet sockets."""
from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import request, parse

from realm.auth import CredentialStore
from realm.combat import Combat
from realm.engine import Engine
from realm.event_stream import FeedWaitServer
from realm.gateway import Gateway, GatewayServer
from realm.telnet import LocalTelnetServer


class PvPTransportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        self.gateway = Gateway(self.path, world_id="pvp_transports", scoped_credentials=True,
                               scoped_guard_path=Path(tmp.name) / "guard.json")
        self.engine = self.gateway.engine
        for player in ("Ada", "Bob"):
            self.engine.join(player)
        self.tokens = {player: self.gateway.scoped_credentials.provision(player)
                       for player in ("Ada", "Bob")}
        self.http = GatewayServer(self.gateway, (), port=0)
        self.http_thread = threading.Thread(target=self.http.serve_forever,
                                            kwargs={"poll_interval": 0.02}, daemon=True)
        self.http_thread.start()
        self.addCleanup(self.stop_http)

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(timeout=2)

    def request(self, player: str, command: str, action_id: str, *, token: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            body = json.dumps({"player": player, "command": command, "action_id": action_id})
            conn.request("POST", "/api/v1/command", body=body, headers={
                "Authorization": "Bearer " + (token or self.tokens[player]),
                "Content-Type": "application/json",
            })
            result = conn.getresponse()
            return result.status, json.loads(result.read())
        finally:
            conn.close()

    def terminal(self, port: int, *, token: str, name: str | None = None):
        conn = socket.create_connection(("127.0.0.1", port), timeout=3)
        conn.settimeout(3)
        self.addCleanup(conn.close)
        self.receive(conn, b"Player token: " if name is None else b"Token: ")
        conn.sendall((token + "\r\n").encode())
        if name is not None:
            self.receive(conn, b"Name: ")
            conn.sendall((name + "\r\n").encode())
        self.receive(conn, b"REALM> ")
        return conn

    @staticmethod
    def receive(conn, marker: bytes) -> str:
        data = bytearray()
        for _ in range(24):
            block = conn.recv(4096)
            if not block:
                break
            data.extend(block)
            if marker in data:
                break
        return data.decode(errors="replace")

    def test_scoped_http_replays_one_attack_and_rejects_impersonation(self):
        Combat.configure(self.engine, safe_rooms=(), allow_offline_targets=True)
        self.assertEqual(200, self.request("Ada", "pvp on", "opt-a")[0])
        self.assertEqual(200, self.request("Bob", "pvp on", "opt-b")[0])
        self.assertEqual(403, self.request("Bob", "pvp off", "forge", token=self.tokens["Ada"])[0])
        def strike(_):
            return self.request("Ada", "attack Bob", "same-id")
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(strike, range(2)))
        self.assertEqual([200, 200], sorted(result[0] for result in responses))
        self.assertEqual(responses[0][1]["text"], responses[1][1]["text"])
        self.assertIn("HP 8/12", self.request("Bob", "pvp status", "fresh-status")[1]["text"])
        self.gateway.scoped_credentials.revoke("Ada")
        self.assertEqual(401, self.request("Ada", "attack Bob", "same-id")[0])

    def test_player_bound_telnet_permits_combat_but_shared_token_cannot(self):
        Combat.configure(self.engine, safe_rooms=(), allow_offline_targets=True)
        self.gateway.command_remote("Bob", "pvp on", "bob-opt", self.tokens["Bob"])
        legacy = LocalTelnetServer(self.engine, "legacy-shared", 0)
        legacy_thread = threading.Thread(target=legacy.serve_forever,
                                         kwargs={"poll_interval": 0.02}, daemon=True)
        legacy_thread.start()
        self.addCleanup(lambda: (legacy.shutdown(), legacy.server_close(), legacy_thread.join(timeout=2)))
        legacy_conn = self.terminal(legacy.server_address[1], token="legacy-shared", name="Ada")
        legacy_conn.sendall(b"pvp on\r\n")
        self.assertIn("Access revoked", self.receive(legacy_conn, b"Access revoked"))
        self.assertIn("PvP OFF", self.gateway.command_remote("Ada", "pvp status", None,
                                                            self.tokens["Ada"])["text"])

        player_auth = CredentialStore(self.engine)
        real_token = player_auth.provision("Ada")
        player_server = LocalTelnetServer(self.engine, None, 0, player_credentials=True)
        player_thread = threading.Thread(target=player_server.serve_forever,
                                         kwargs={"poll_interval": 0.02}, daemon=True)
        player_thread.start()
        self.addCleanup(lambda: (player_server.shutdown(), player_server.server_close(), player_thread.join(timeout=2)))
        authenticated = self.terminal(player_server.server_address[1], token=real_token)
        authenticated.sendall(b"pvp on\r\n")
        self.assertIn("now ON", self.receive(authenticated, b"REALM> "))
        authenticated.sendall(b"attack Bob\r\n")
        self.assertIn("4 damage", self.receive(authenticated, b"REALM> "))
        self.assertIn("HP 8/12", self.gateway.command_remote("Bob", "pvp status", None,
                                                             self.tokens["Bob"])["text"])

    def test_offline_default_denies_attack_in_both_transports(self):
        Combat.configure(self.engine, safe_rooms=())  # offline deny is explicit default
        self.assertEqual(200, self.request("Ada", "pvp on", "opt-a")[0])
        self.assertEqual(200, self.request("Bob", "pvp on", "opt-b")[0])
        status, payload = self.request("Ada", "attack Bob", "blocked")
        self.assertEqual(400, status)
        self.assertIn("presence", str(payload).lower())
        self.assertIn("HP 12/12", self.request("Bob", "pvp status", "check")[1]["text"])

    def test_longpoll_reaches_global_death_without_exposing_private_room(self):
        Combat.configure(self.engine, max_hp=4, damage=4, safe_rooms=(),
                         allow_offline_targets=True)
        self.engine.join("Cora")
        token = self.gateway.scoped_credentials.provision("Cora")
        feed_server = FeedWaitServer(self.gateway, (), port=0)
        thread = threading.Thread(target=feed_server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (feed_server.shutdown(), feed_server.server_close(), thread.join(timeout=2)))
        after = self.gateway.cursor()
        self.request("Ada", "pvp on", "opt-a")
        self.request("Bob", "pvp on", "opt-b")
        self.request("Ada", "north", "a-north")
        self.request("Bob", "north", "b-north")
        self.request("Ada", "attack Bob", "kill")
        for path in ("/api/v1/feed", "/api/v1/feed/wait"):
            query = parse.urlencode({"after": after, "wait_ms": 0})
            req = request.Request(f"http://127.0.0.1:{feed_server.server_port}{path}?{query}",
                                  headers={"Authorization": "Bearer " + token})
            with request.urlopen(req, timeout=3) as response:
                self.assertEqual(200, response.status)
                result = json.loads(response.read())
            notices = [entry for entry in result["events"] if entry["kind"] == "pvp_death"]
            self.assertEqual(1, len(notices), path)
            self.assertEqual("global", notices[0]["room"])
            self.assertNotIn("corridor", str(notices[0]).lower())

    def test_unread_global_pk_survives_room_moves_but_not_prejoin_history(self):
        Combat.configure(self.engine, max_hp=4, damage=4, safe_rooms=(),
                         allow_offline_targets=True)
        self.engine.join("Cora")
        self.tokens["Cora"] = self.gateway.scoped_credentials.provision("Cora")
        for player in ("Ada", "Bob"):
            self.assertEqual(200, self.request(player, "pvp on", "opt-" + player)[0])
            self.assertEqual(200, self.request(player, "north", "go-" + player)[0])
        self.assertEqual(200, self.request("Ada", "attack Bob", "fatal")[0])
        # This viewer has not fetched a feed since joining. Travel must not
        # erase public news while unrelated private room speech stays hidden.
        self.assertEqual(200, self.request("Ada", "say Private corridor", "private")[0])
        self.assertEqual(200, self.request("Cora", "north", "cora-north")[0])
        self.assertEqual(200, self.request("Cora", "south", "cora-south")[0])
        feed_server = FeedWaitServer(self.gateway, (), port=0)
        thread = threading.Thread(target=feed_server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (feed_server.shutdown(), feed_server.server_close(), thread.join(timeout=2)))
        for path in ("/api/v1/feed", "/api/v1/feed/wait"):
            query = parse.urlencode({"after": 0, "wait_ms": 0})
            req = request.Request(f"http://127.0.0.1:{feed_server.server_port}{path}?{query}",
                                  headers={"Authorization": "Bearer " + self.tokens["Cora"]})
            with request.urlopen(req, timeout=3) as response:
                self.assertEqual(200, response.status)
                payload = json.loads(response.read())
            news = [entry for entry in payload["events"] if entry["kind"] == "pvp_death"]
            self.assertEqual(1, len(news), path)
            self.assertEqual("global", news[0]["room"])
            self.assertNotIn("Private corridor", str(payload))

        self.engine.join("Dax")
        self.assertFalse(any(entry["kind"] == "pvp_death"
                             for entry in self.gateway.feed("Dax", 0)["events"]))

    def test_two_concurrent_lethal_attacks_emit_one_durable_death(self):
        Combat.configure(self.engine, max_hp=4, damage=4, safe_rooms=(),
                         allow_offline_targets=True)
        self.engine.join("Cora")
        self.tokens["Cora"] = self.gateway.scoped_credentials.provision("Cora")
        for player in ("Ada", "Bob", "Cora"):
            self.assertEqual(200, self.request(player, "pvp on", "opt-" + player)[0])
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.request, "Ada", "attack Bob", "independent-a")
            b = pool.submit(self.request, "Cora", "attack Bob", "independent-c")
            statuses = sorted([a.result()[0], b.result()[0]])
        self.assertEqual([200, 400], statuses)
        with closing(self.engine._connect()) as db:
            self.assertEqual(0, db.execute("SELECT hp FROM combatants WHERE player='Bob'").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT count(*) FROM events WHERE kind='pvp_death'").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT count(*) FROM combat_announcements").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
