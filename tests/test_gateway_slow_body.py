"""Real socket checks that slow/aborted bodies cannot exhaust unbounded workers."""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer


class GatewaySlowBodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        gateway = Gateway(Path(self.tmp.name) / "world.sqlite3", world_id="slow-body")
        self.token = gateway.credentials.provision("SlowAlice")
        self.server = GatewayServer(
            gateway, (), port=0, max_active_requests=2,
            socket_idle_timeout_seconds=0.4, body_deadline_seconds=0.4,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        self.clients = []
        self.addCleanup(self._close_clients)

    def _close_clients(self):
        for client in self.clients:
            try:
                client.close()
            except OSError:
                pass

    def _slow(self):
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        client.settimeout(2)
        client.sendall((
            f"POST /api/v1/join HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Content-Type: application/json\r\nContent-Length: 4096\r\n\r\n"
        ).encode("ascii"))
        self.clients.append(client)
        return client

    def _status(self) -> int:
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            client.settimeout(2)
            client.sendall(b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            response = bytearray()
            while True:
                part = client.recv(1024)
                if not part:
                    break
                response.extend(part)
            first = bytes(response).split(b"\r\n", 1)[0]
        self.assertTrue(first.startswith(b"HTTP/1.1 "), first)
        return int(first.split()[1])

    def test_hung_post_bodies_are_bounded_and_reclaimed(self):
        a, b = self._slow(), self._slow()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self._status() == 503:
                break
            time.sleep(0.015)
        else:
            self.fail("Two slow POST bodies failed to exhaust a two-worker cap")
        self.assertEqual(503, self._status())
        # The incomplete bodies have a hard deadline; after expiry, workers
        # should be reclaimed and an independent request admitted again.
        time.sleep(0.6)
        self.assertEqual(200, self._status())
        for client in (a, b):
            try:
                data = client.recv(512)
            except (ConnectionResetError, BrokenPipeError):
                data = b""
            self.assertTrue(data == b"" or b"408 Request Timeout" in data, data)

    def test_slow_trickle_cannot_extend_body_deadline(self):
        client = self._slow()
        beginning = time.monotonic()
        for _ in range(9):
            time.sleep(0.08)
            try:
                client.sendall(b" ")
            except OSError:
                break
        try:
            wire = client.recv(1024)
        except OSError:
            wire = b""
        self.assertLess(time.monotonic() - beginning, 1.4)
        self.assertTrue(wire == b"" or b"408 Request Timeout" in wire, wire)
        self.assertEqual(200, self._status())

    def test_limit_and_timeout_config_are_type_and_range_checked(self):
        gateway = self.server.gateway
        for kw in (
            {"max_active_requests": 0},
            {"max_active_requests": True},
            {"max_active_requests": 10001},
            {"socket_idle_timeout_seconds": 0},
            {"socket_idle_timeout_seconds": float("nan")},
            {"body_deadline_seconds": False},
            {"body_deadline_seconds": -1.0},
        ):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                GatewayServer(gateway, (), port=0, **kw)

    def test_oversized_decimal_content_length_returns_bounded_413(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            client.settimeout(2)
            length = "9" * 10000
            client.sendall((
                f"POST /api/v1/join HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {self.token}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n"
            ).encode("ascii"))
            response = client.recv(512)
            self.assertIn(b"413", response.split(b"\r\n", 1)[0])
        self.assertEqual(200, self._status())


if __name__ == "__main__":
    unittest.main()
