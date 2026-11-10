"""Actual socket/HTTP parity over one independent SQLite world instance.

The browser calls this gateway via HTTP and a classic MUD client speaks the
Telnet adapter.  These tests verify their shared authoritative engine effects,
not merely that each adapter works in isolation.
"""

import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.gateway import Gateway, GatewayServer
from realm.telnet import LocalTelnetServer


class CrossTransportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "shared.sqlite3"

        self.gateway = Gateway(db, world_id="cross-transport-test")
        self.browser_token = self.gateway.credentials.provision("Browser")
        self.http = GatewayServer(
            self.gateway, ("http://127.0.0.1:8000",), port=0
        )
        self.telnet = LocalTelnetServer(Engine(db), "telnet-local-secret", 0)

        self.http_thread = threading.Thread(
            target=self.http.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.telnet_thread = threading.Thread(
            target=self.telnet.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.http_thread.start()
        self.telnet_thread.start()
        self.addCleanup(self._stop_servers)

    def _stop_servers(self):
        for server, thread in (
            (self.http, self.http_thread),
            (self.telnet, self.telnet_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def api(self, endpoint, payload=None):
        headers = {"Authorization": "Bearer " + self.browser_token}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        req = request.Request(
            f"http://127.0.0.1:{self.http.server_port}{endpoint}",
            data=body,
            headers=headers,
        )
        with request.urlopen(req, timeout=3) as response:
            self.assertEqual(200, response.status)
            return json.loads(response.read())

    def read_to(self, conn, marker):
        data = bytearray()
        for _ in range(20):
            data.extend(conn.recv(4096))
            if marker in data:
                return data.decode("utf-8", errors="replace")
        self.fail(f"Telnet did not send {marker!r}; got {data!r}")

    def telnet_login(self, name):
        conn = socket.create_connection(self.telnet.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        self.read_to(conn, b"Token: ")
        conn.sendall(b"telnet-local-secret\r\n")
        self.read_to(conn, b"Name: ")
        conn.sendall(name.encode("utf-8") + b"\r\n")
        self.read_to(conn, b"REALM> ")
        return conn

    def telnet_command(self, conn, command):
        conn.sendall(command.encode("utf-8") + b"\r\n")
        return self.read_to(conn, b"REALM> ")

    def test_speech_and_room_visibility_cross_browser_and_telnet(self):
        browser = self.api("/api/v1/join", {"player": "Browser"})
        telnet = self.telnet_login("Terminal")

        sent = self.api("/api/v1/command", {
            "player": "Browser", "command": "say From the browser gateway"
        })
        self.assertIn("You say", sent["text"])
        self.assertIn("Browser said: From the browser gateway", self.telnet_command(telnet, "listen"))

        self.assertIn("You say", self.telnet_command(telnet, "say From the terminal adapter"))
        query = parse.urlencode({"player": "Browser", "after": browser["cursor"]})
        feed = self.api(f"/api/v1/feed?{query}")
        self.assertTrue(any(
            event["actor"] == "Terminal" and "From the terminal adapter" in event["text"]
            for event in feed["events"]
        ))

        moved = self.api("/api/v1/command", {"player": "Browser", "command": "north"})
        self.assertIn("Corridor", moved["text"])
        self.assertIn("You say", self.telnet_command(telnet, "say Atrium-only speech"))
        query = parse.urlencode({"player": "Browser", "after": feed["cursor"]})
        after_move = self.api(f"/api/v1/feed?{query}")
        self.assertFalse(any("Atrium-only speech" in event["text"] for event in after_move["events"]))

    def test_item_state_is_shared_across_transports(self):
        self.api("/api/v1/join", {"player": "Browser"})
        telnet = self.telnet_login("Terminal")
        self.assertIn("You take Brass token.", self.telnet_command(telnet, "take token"))
        denied = self.api("/api/v1/command", {"player": "Browser", "command": "take token"})
        self.assertIn("That item is not here.", denied["text"])
        self.assertIn("Brass token", self.telnet_command(telnet, "inventory"))

    def test_telnet_to_http_give_shares_persisted_inventory_and_room_event(self):
        browser = self.api("/api/v1/join", {"player": "Browser"})
        terminal = self.telnet_login("Terminal")
        self.assertIn("You take Brass token.", self.telnet_command(terminal, "take token"))
        self.assertIn("You give Brass token to Browser.",
                      self.telnet_command(terminal, "give token to Browser"))
        bag = self.api("/api/v1/command", {"player": "Browser", "command": "inventory"})
        self.assertEqual("You carry: Brass token", bag["text"])
        self.assertIn("You carry: nothing", self.telnet_command(terminal, "inventory"))
        query = parse.urlencode({"player": "Browser", "after": browser["cursor"]})
        feed = self.api(f"/api/v1/feed?{query}")
        self.assertEqual(1, sum(event["kind"] == "give" for event in feed["events"]))


if __name__ == "__main__":
    unittest.main()
