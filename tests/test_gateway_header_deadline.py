"""Actual loopback clients cannot occupy gateway workers by trickling headers."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer


class GatewayHeaderDeadlineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "world.sqlite3", world_id="header_deadline")
        self.token = self.gateway.credentials.provision("HeaderTester")
        self.server = GatewayServer(
            self.gateway, (), port=0, max_active_requests=1,
            socket_idle_timeout_seconds=1.1, body_deadline_seconds=1.0,
            header_deadline_seconds=0.3,
        )
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
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

    def _connect(self):
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        client.settimeout(2)
        self.clients.append(client)
        return client

    def _status(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            client.settimeout(2)
            client.sendall(
                b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n"
            )
            first = client.recv(512).split(b"\r\n", 1)[0]
        self.assertTrue(first.startswith(b"HTTP/1.1 "), first)
        return int(first.split()[1])

    def _eventual_status(self, wanted, *, seconds=0.9):
        deadline = time.monotonic() + seconds
        observed = []
        while time.monotonic() < deadline:
            code = self._status()
            observed.append(code)
            if code == wanted:
                return
            time.sleep(0.02)
        self.fail(f"Expected HTTP {wanted}; observed {observed!r}")

    def test_slow_request_line_reclaims_worker_before_socket_idle_timeout(self):
        slow = self._connect()
        slow.sendall(b"GET /api/v1/health HTTP/1.1")
        self._eventual_status(503, seconds=0.18)
        beginning = time.monotonic()
        for _ in range(8):
            time.sleep(0.07)
            try:
                slow.sendall(b"x")
            except OSError:
                break
        self._eventual_status(200, seconds=0.35)
        self.assertLess(time.monotonic() - beginning, 0.9)

    def test_slow_header_line_cannot_extend_absolute_deadline(self):
        slow = self._connect()
        slow.sendall(b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Trickle: ")
        self._eventual_status(503, seconds=0.18)
        for _ in range(9):
            time.sleep(0.065)
            try:
                slow.sendall(b"!")
            except OSError:
                break
        self._eventual_status(200, seconds=0.35)

    def test_truncated_headers_at_deadline_never_dispatch_health(self):
        client = self._connect()
        client.sendall(
            b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"X-Unfinished: not-terminated"
        )
        time.sleep(0.45)
        try:
            response = client.recv(1024)
        except OSError:
            response = b""
        self.assertNotIn(b"HTTP/1.1 200 OK", response)
        self._eventual_status(200, seconds=0.45)

    def test_header_deadline_ends_after_headers_not_after_command_body(self):
        client = self._connect()
        body = json.dumps({"player": "HeaderTester"}).encode()
        client.sendall((
            "POST /api/v1/join HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\nAuthorization: Bearer {self.token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode())
        time.sleep(0.43)
        client.sendall(body)
        response = client.recv(2048).split(b"\r\n", 1)[0]
        self.assertEqual(b"HTTP/1.1 200 OK", response)

    def test_keepalive_idle_time_does_not_consume_next_header_budget(self):
        client = self._connect()
        client.sendall(
            b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Connection: keep-alive\r\n\r\n"
        )
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(client.recv(512))
        start, _, rest = bytes(response).partition(b"\r\n\r\n")
        self.assertIn(b"HTTP/1.1 200 OK", start)
        length = int(next(
            line.split(b":", 1)[1].strip()
            for line in start.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        ))
        while len(rest) < length:
            rest += client.recv(512)
        time.sleep(0.43)  # longer than header deadline, shorter than idle limit
        client.sendall(
            b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertEqual(b"HTTP/1.1 200 OK", client.recv(512).split(b"\r\n", 1)[0])

    def test_header_deadline_validation_is_bounded(self):
        for invalid in (0, -1.0, True, float("nan"), float("inf"), 121):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                GatewayServer(self.gateway, (), port=0, header_deadline_seconds=invalid)


if __name__ == "__main__":
    unittest.main()
