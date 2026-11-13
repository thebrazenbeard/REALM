"""Admitted Telnet sessions must relinquish newly reserved NPC names while idle.

These are real local TCP sockets backed by a disposable world. No credentials,
player state, listeners or services outside this test are touched.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway
from realm.principal_roles import reserve_npc
from realm.telnet import LocalTelnetServer


class IdleReservedNPCSocketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gateway = Gateway(Path(temp.name) / "world.sqlite3", world_id="idle_probe")
        self.gateway.join("Moss")
        self.player_token = self.gateway.credentials.provision("Moss")
        self.server = None

    def launch(self, *, player_credentials: bool):
        self.server = LocalTelnetServer(
            self.gateway.engine,
            None if player_credentials else "local-shared-secret",
            port=0, player_credentials=player_credentials,
        )
        thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        thread.start()

        def shutdown():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.addCleanup(shutdown)
        conn = socket.create_connection(self.server.server_address, timeout=5)
        conn.settimeout(5)
        self.addCleanup(conn.close)
        self.receive_until(
            conn, b"Player token: " if player_credentials else b"Token: "
        )
        if player_credentials:
            conn.sendall(self.player_token.encode("ascii") + b"\r\n")
        else:
            conn.sendall(b"local-shared-secret\r\n")
            self.receive_until(conn, b"Name: ")
            conn.sendall(b"Moss\r\n")
        self.receive_until(conn, b"REALM> ")
        self.assertTrue(self.server.is_name_active("Moss"))
        return conn

    @staticmethod
    def receive_until(conn, marker: bytes):
        data = bytearray()
        while marker not in data:
            part = conn.recv(4096)
            if not part:
                raise AssertionError(f"Telnet closed before {marker!r}: {data!r}")
            data.extend(part)
        return bytes(data)

    def assert_idle_revoked(self, conn):
        reserve_npc(self.gateway.engine, "Moss")
        # No new client input after reservation. That is the regression:
        # pre-fix _line() blocks on recv for 90 seconds.
        conn.settimeout(4)
        self.assertIn(b"Access revoked", self.receive_until(conn, b"Access revoked"))
        deadline = time.monotonic() + 3
        while self.server.is_name_active("Moss") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.server.is_name_active("Moss"))

    def test_idle_shared_token_telnet_revoked_without_new_client_line(self):
        conn = self.launch(player_credentials=False)
        self.assert_idle_revoked(conn)

    def test_idle_player_bearer_telnet_revoked_without_new_client_line(self):
        conn = self.launch(player_credentials=True)
        self.assert_idle_revoked(conn)

    def test_unreserved_player_socket_remains_usable_after_poll_interval(self):
        conn = self.launch(player_credentials=True)
        time.sleep(1.3)
        self.assertTrue(self.server.is_name_active("Moss"))
        conn.sendall(b"say still a human\r\n")
        text = self.receive_until(conn, b"REALM> ")
        self.assertIn(b"still a human", text)
        self.assertFalse(self.gateway.engine._connect().in_transaction)


if __name__ == "__main__":
    unittest.main()
