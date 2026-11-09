"""Adversarial room-entry privacy for text history over real loopback transports."""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer
from realm.telnet import LocalTelnetServer


class TextHistoryPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = Gateway(Path(self.temp.name) / "privacy.sqlite3", world_id="privacy")
        self.tokens = {
            actor: self.gateway.credentials.provision(actor)
            for actor in ("Alice", "Bob", "Cara")
        }
        self.http_server = GatewayServer(self.gateway, (), port=0)
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.http_thread.start()
        self.addCleanup(self._stop_http)

    def _stop_http(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_thread.join(timeout=2)

    def http(self, actor: str, path: str, body: dict | None = None,
             *, bearer: str | None = None) -> tuple[int, dict]:
        headers = {"Authorization": "Bearer " + (bearer or self.tokens[actor])}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = request.Request(
            f"http://127.0.0.1:{self.http_server.server_port}{path}",
            headers=headers, data=data,
        )
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def join(self, actor: str) -> None:
        status, body = self.http(actor, "/api/v1/join", {"player": actor})
        self.assertEqual(200, status, body)

    def command(self, actor: str, command: str, action_id: str) -> tuple[int, str]:
        status, body = self.http(actor, "/api/v1/command", {
            "player": actor, "command": command, "action_id": action_id,
        })
        return status, body.get("text", body.get("error", ""))

    def assert_private(self, actor: str, secret: str, *, action_prefix: str) -> None:
        for index, command in enumerate(("listen", "history", "history 50", "history 1")):
            status, text = self.command(actor, command, f"{action_prefix}-{index}")
            self.assertEqual(200, status, (command, text))
            self.assertNotIn(secret, text, command)

    def test_new_arrival_cannot_query_preentry_speech_through_any_alias(self) -> None:
        self.join("Alice")
        self.assertEqual(200, self.command("Alice", "say PRIVATE_BEFORE_BOB", "alice-before")[0])
        self.join("Bob")
        self.assert_private("Bob", "PRIVATE_BEFORE_BOB", action_prefix="bob-first")
        status, text = self.command("Alice", "say PUBLIC_AFTER_BOB", "alice-after")
        self.assertEqual(200, status, text)
        status, text = self.command("Bob", "history 50", "bob-after")
        self.assertEqual(200, status)
        self.assertIn("PUBLIC_AFTER_BOB", text)
        self.assertNotIn("PRIVATE_BEFORE_BOB", text)

    def test_reentry_invalidates_old_speech_and_cached_read_receipts(self) -> None:
        self.join("Alice")
        self.join("Bob")
        self.assertEqual(200, self.command("Alice", "say FIRST_SESSION", "first")[0])
        status, text = self.command("Bob", "listen", "stable-read-id")
        self.assertEqual(200, status)
        self.assertIn("FIRST_SESSION", text)
        self.assertEqual(200, self.command("Bob", "north", "bob-north")[0])
        status, text = self.command("Bob", "listen", "stable-read-id")
        self.assertEqual(200, status)
        self.assertNotIn("FIRST_SESSION", text)
        self.assertEqual(200, self.command("Alice", "say ABSENT_PRIVATE", "during-absence")[0])
        self.assertEqual(200, self.command("Bob", "south", "bob-south")[0])
        self.assert_private("Bob", "ABSENT_PRIVATE", action_prefix="bob-reentry")
        status, text = self.command("Bob", "listen", "stable-read-id")
        self.assertEqual(200, status)
        self.assertNotIn("FIRST_SESSION", text)
        self.assertNotIn("ABSENT_PRIVATE", text)

    def test_deleted_latest_return_arrival_fails_closed_even_with_old_join(self) -> None:
        self.join("Alice")
        self.join("Bob")
        self.command("Bob", "north", "move-out")
        self.command("Alice", "say DURING_ABSENCE", "secret")
        self.command("Bob", "south", "move-in")
        with sqlite3.connect(self.gateway.engine.db_path) as db:
            deleted = db.execute(
                "DELETE FROM events WHERE actor='Bob' AND kind='move' AND room_id='atrium'"
            ).rowcount
        self.assertEqual(1, deleted)
        for index, command in enumerate(("listen", "history 50")):
            status, text = self.command("Bob", command, f"tampered-{index}")
            self.assertEqual(400, status)
            self.assertNotIn("DURING_ABSENCE", text)

    def test_no_entry_event_fails_closed_and_worlds_are_isolated(self) -> None:
        self.join("Alice")
        self.command("Alice", "say PRIVATE_WORLD_ONE", "one-secret")
        self.join("Bob")
        other = Gateway(Path(self.temp.name) / "other.sqlite3", world_id="other")
        other.join("Bob")
        self.assertNotIn("PRIVATE_WORLD_ONE", other.engine.act("Bob", "history 50"))
        status, text = self.http("Bob", "/api/v1/command", {
            "player": "Alice", "command": "history 50", "action_id": "wrong-actor",
        })
        self.assertEqual(403, status)
        self.assertNotIn("PRIVATE_WORLD_ONE", json.dumps(text))
        status, text = self.http("Bob", "/api/v1/command", {
            "player": "Bob", "command": "history 50", "action_id": "forged-bearer",
        }, bearer="invalid-bearer")
        self.assertEqual(401, status)
        self.assertNotIn("PRIVATE_WORLD_ONE", json.dumps(text))
        with sqlite3.connect(self.gateway.engine.db_path) as db:
            db.execute("DELETE FROM events WHERE actor='Bob' AND kind='join'")
        status, text = self.command("Bob", "history 50", "unproved-join")
        self.assertEqual(400, status)
        self.assertNotIn("PRIVATE_WORLD_ONE", text)

    def _start_telnet(self) -> LocalTelnetServer:
        server = LocalTelnetServer(self.gateway.engine, "private-test-token", 0)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (
            server.shutdown(), server.server_close(), thread.join(timeout=2)
        ))
        return server

    def _receive(self, conn: socket.socket, marker: bytes) -> str:
        data = bytearray()
        for _ in range(20):
            data.extend(conn.recv(4096))
            if marker in data:
                return data.decode("utf-8", errors="replace")
        self.fail(f"Telnet response did not reach {marker!r}: {data!r}")

    def _telnet_login(self, server: LocalTelnetServer, actor: str) -> socket.socket:
        conn = socket.create_connection(server.server_address, timeout=3)
        conn.settimeout(3)
        self.addCleanup(conn.close)
        self._receive(conn, b"Token: ")
        conn.sendall(b"private-test-token\r\n")
        self._receive(conn, b"Name: ")
        conn.sendall(actor.encode() + b"\r\n")
        self._receive(conn, b"REALM> ")
        return conn

    def _telnet_command(self, conn: socket.socket, command: str) -> str:
        conn.sendall(command.encode() + b"\r\n")
        return self._receive(conn, b"REALM> ")

    def test_telnet_aliases_respect_entry_floor_and_reentry(self) -> None:
        server = self._start_telnet()
        alice = self._telnet_login(server, "Alice")
        self.assertIn("TELNET_SECRET_BEFORE", self._telnet_command(alice, "say TELNET_SECRET_BEFORE"))
        bob = self._telnet_login(server, "Bob")
        for command in ("listen", "history", "history 50"):
            self.assertNotIn("TELNET_SECRET_BEFORE", self._telnet_command(bob, command))
        self.assertIn("TELNET_PUBLIC_AFTER", self._telnet_command(alice, "say TELNET_PUBLIC_AFTER"))
        self.assertIn("TELNET_PUBLIC_AFTER", self._telnet_command(bob, "history 50"))
        self._telnet_command(bob, "north")
        self._telnet_command(alice, "say TELNET_ABSENT_SECRET")
        self._telnet_command(bob, "south")
        for command in ("listen", "history 50"):
            result = self._telnet_command(bob, command)
            self.assertNotIn("TELNET_ABSENT_SECRET", result)
            self.assertNotIn("TELNET_PUBLIC_AFTER", result)


if __name__ == "__main__":
    unittest.main()
