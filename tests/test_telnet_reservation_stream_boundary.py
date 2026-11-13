"""Adversarial local Telnet NPC reservation under partial-stream traffic.

All credentials are synthetic, all SQLite files disposable and sockets loopback.
The repeated input bytes never form gameplay commands.
"""
from __future__ import annotations

import socket
import sqlite3
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


def receive_until(sock: socket.socket, target: bytes) -> bytes:
    pending = bytearray()
    while target not in pending:
        part = sock.recv(4096)
        if not part:
            raise AssertionError(f"Connection closed before {target!r}: {pending!r}")
        pending.extend(part)
    return bytes(pending)


class StreamBoundaryReservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gw = Gateway(Path(temp.name) / "game.sqlite3", world_id="stream_boundary")
        self.gw.join("Moss")
        self.token = self.gw.credentials.provision("Moss")
        self.server = None

    def connect(self, *, player_bound: bool):
        server = LocalTelnetServer(
            self.gw.engine, None if player_bound else "synthetic-local-secret",
            port=0, player_credentials=player_bound,
        )
        self.server = server
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        sock = socket.create_connection(server.server_address, timeout=4)
        sock.settimeout(4)
        self.addCleanup(sock.close)
        receive_until(sock, b"Player token: " if player_bound else b"Token: ")
        sock.sendall((self.token if player_bound else "synthetic-local-secret").encode() + b"\r\n")
        if not player_bound:
            receive_until(sock, b"Name: ")
            sock.sendall(b"mOsS\r\n")  # Alias should resolve to the same host principal.
        receive_until(sock, b"REALM> ")
        self.assertTrue(server.is_name_active("Moss"))
        return sock

    @staticmethod
    def dribble(sock: socket.socket) -> None:
        # Repeated data arrival prevents any recv() idle timeout until the
        # server closes. No complete command is ever submitted.
        for _ in range(11):
            try:
                sock.sendall(b"x")
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
            time.sleep(0.24)

    def assert_disconnected_while_stream_active(self, name="Moss") -> None:
        # At the end of a 2.64-second uninterrupted byte stream, do not
        # allow a further receive-idle timeout to rescue a broken
        # implementation. A small scheduling allowance is acceptable.
        deadline = time.monotonic() + 0.16
        while self.server.is_name_active(name) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(
            self.server.is_name_active(name),
            "reserved or untrusted socket still claimed after continuous input",
        )

    def test_player_bound_idle_reservation_ejects_during_partial_bytes(self):
        sock = self.connect(player_bound=True)
        self.assertTrue(reserve_npc(self.gw.engine, "Moss"))
        self.dribble(sock)
        self.assert_disconnected_while_stream_active()

    def test_mixed_case_shared_name_ejects_during_partial_bytes(self):
        sock = self.connect(player_bound=False)
        self.assertTrue(reserve_npc(self.gw.engine, "Moss"))
        self.dribble(sock)
        self.assert_disconnected_while_stream_active("mOsS")

    def test_role_store_failure_closes_shared_session_during_partial_bytes(self):
        sock = self.connect(player_bound=False)
        def broken_store(_name):
            raise sqlite3.OperationalError("disposable forced role read failure")
        self.server.is_reserved_name = broken_store
        self.dribble(sock)
        self.assert_disconnected_while_stream_active()

    def test_unreserved_player_stays_connected_under_partial_bytes(self):
        sock = self.connect(player_bound=True)
        for _ in range(6):
            sock.sendall(b"x")
            time.sleep(0.24)
        self.assertTrue(self.server.is_name_active("Moss"))


if __name__ == "__main__":
    unittest.main()
