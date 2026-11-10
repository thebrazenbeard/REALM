"""Real socket tests for optional host-provisioned Telnet player identity."""

from __future__ import annotations

import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.auth import CredentialStore
from realm.engine import Engine
from realm.telnet import LocalTelnetServer


class BoundTelnetSocketTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "one-world.sqlite3"
        self.engine = Engine(self.db)
        self.store = CredentialStore(self.engine)
        self.alice = self.store.provision("Alice")
        self.bob = self.store.provision("Bob")
        self.server = LocalTelnetServer(self.engine, None, 0, player_credentials=True)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def connect(self):
        conn = socket.create_connection(self.server.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        return conn

    def read_until(self, conn, marker: bytes) -> str:
        data = bytearray()
        for _ in range(20):
            part = conn.recv(4096)
            if not part:
                break
            data.extend(part)
            if marker in data:
                return data.decode("utf-8", errors="replace")
        self.fail(f"Expected {marker!r}, received {data!r}")

    def login(self, token: str):
        conn = self.connect()
        self.read_until(conn, b"Player token: ")
        conn.sendall(token.encode("ascii") + b"\r\n")
        self.read_until(conn, b"REALM> ")
        return conn

    def events(self, kind: str, actor: str):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute(
                "SELECT payload_json FROM events WHERE kind=? AND actor=? ORDER BY id",
                (kind, actor),
            ).fetchall()

    def test_unknown_token_denied_without_client_selected_name(self):
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        conn = self.connect()
        prompt = self.read_until(conn, b"Player token: ")
        self.assertNotIn("Name:", prompt)
        conn.sendall(b"invalid\r\n")
        denial = self.read_until(conn, b"Access denied.")
        self.assertNotIn("Name:", denial)
        self.assertEqual([], self.events("join", "Alice"))

    def test_non_ascii_and_oversized_player_tokens_fail_closed(self):
        non_ascii = self.connect()
        self.read_until(non_ascii, b"Player token: ")
        non_ascii.sendall("caf\u00e9".encode("utf-8") + b"\r\n")
        self.assertIn("Access denied", self.read_until(non_ascii, b"Access denied"))
        too_long = self.connect()
        self.read_until(too_long, b"Player token: ")
        too_long.sendall(b"x" * 600 + b"\r\n")
        self.assertIn("Input rejected", self.read_until(too_long, b"Input rejected"))
        self.assertEqual([], self.events("join", "Alice"))

    def test_token_binds_canonical_actor_and_cannot_forge_another_name(self):
        alice = self.login(self.alice)
        bob = self.login(self.bob)
        bob.sendall(b"say message from Bob\r\n")
        self.assertIn("You say: message from Bob", self.read_until(bob, b"REALM> "))
        alice.sendall(b"listen\r\n")
        self.assertIn("Bob said: message from Bob", self.read_until(alice, b"REALM> "))
        self.assertEqual(1, len(self.events("say", "Bob")))
        self.assertEqual(0, len(self.events("say", "Alice")))

    def test_same_player_cannot_open_second_active_socket(self):
        first = self.login(self.alice)
        second = self.connect()
        self.read_until(second, b"Player token: ")
        second.sendall(self.alice.encode("ascii") + b"\r\n")
        self.assertIn("already connected", self.read_until(second, b"already connected"))
        first.sendall(b"quit\r\n")
        self.read_until(first, b"position is saved")
        deadline = time.monotonic() + 2
        while self.server.is_name_active("aLiCe") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.server.is_name_active("ALICE"))
        self.assertIsNotNone(self.login(self.alice))

    def test_revoked_active_socket_cannot_commit_future_commands(self):
        conn = self.login(self.alice)
        self.assertTrue(self.store.revoke("Alice"))
        conn.sendall(b"say forbidden after revocation\r\n")
        self.assertIn("Access revoked", self.read_until(conn, b"Access revoked"))
        self.assertEqual([], self.events("say", "Alice"))

    def test_other_world_player_token_is_not_valid_here(self):
        other_db = self.db.parent / "other-world.sqlite3"
        foreign = CredentialStore(Engine(other_db)).provision("Wanderer")
        conn = self.connect()
        self.read_until(conn, b"Player token: ")
        conn.sendall(foreign.encode("ascii") + b"\r\n")
        self.assertIn("Access denied", self.read_until(conn, b"Access denied"))
        self.assertEqual([], self.events("join", "Wanderer"))

    def test_rotation_between_precheck_and_writer_cannot_commit(self):
        conn = self.login(self.alice)
        router = self.server.router
        actual = router.act

        def rotate_before_writer(*args, **kwargs):
            self.store.rotate("Alice")
            return actual(*args, **kwargs)

        router.act = rotate_before_writer
        conn.sendall(b"say forbidden by writer guard\r\n")
        self.assertIn("Access revoked", self.read_until(conn, b"Access revoked"))
        self.assertEqual([], self.events("say", "Alice"))

    def test_rotation_between_login_precheck_and_join_cannot_commit(self):
        actual = self.server.join_exclusive

        def rotate_before_join(*args, **kwargs):
            self.store.rotate("Alice")
            return actual(*args, **kwargs)

        self.server.join_exclusive = rotate_before_join
        conn = self.connect()
        self.read_until(conn, b"Player token: ")
        conn.sendall(self.alice.encode("ascii") + b"\r\n")
        self.assertIn("Access revoked", self.read_until(conn, b"Access revoked"))
        self.assertEqual([], self.events("join", "Alice"))

    def test_secure_mode_routes_crafting_through_shared_command_router(self):
        self.server.router.seed_demo_crafting()
        conn = self.login(self.alice)
        conn.sendall(b"gather stone\r\n")
        self.assertIn("stone", self.read_until(conn, b"REALM> ").lower())
        conn.sendall(b"resources\r\n")
        self.assertIn("stone", self.read_until(conn, b"REALM> ").lower())


if __name__ == "__main__":
    unittest.main()
