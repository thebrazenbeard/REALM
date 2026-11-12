"""Real socket checks that slow/aborted bodies cannot exhaust unbounded workers."""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def _status(self, *, allow_reset: bool = False) -> int | None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            client.settimeout(2)
            response = bytearray()
            try:
                client.sendall(
                    b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Connection: close\r\n\r\n"
                )
                while True:
                    part = client.recv(1024)
                    if not part:
                        break
                    response.extend(part)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # On Windows, closing an overloaded socket with unread GET
                # bytes may reset it before a best-effort 503 is delivered.
                if not allow_reset:
                    raise
            if not response and allow_reset:
                return None
            first = bytes(response).split(b"\r\n", 1)[0]
        self.assertTrue(first.startswith(b"HTTP/1.1 "), first)
        return int(first.split()[1])

    def _assert_busy_preheader_503(self):
        """An empty request has no unread GET bytes to cause a Windows reset."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            client.settimeout(2)
            try:
                response = client.recv(512)
            except (ConnectionAbortedError, ConnectionResetError) as error:
                self.fail(f"A busy preheader probe reset without HTTP 503: {error}")
        self.assertTrue(
            response.startswith(b"HTTP/1.1 503 "),
            f"A busy gateway must attempt a real 503, not silently close clients: {response!r}",
        )

    def test_hung_post_bodies_are_bounded_and_reclaimed(self):
        # Leave ample scheduling margin for Windows while still enforcing a
        # finite body/idle deadline and bounded worker admission.
        self.server.socket_idle_timeout_seconds = 1.0
        self.server.body_deadline_seconds = 1.0
        entered = 0
        entered_lock = threading.Lock()
        original_worker = self.server.process_request_thread

        def track_worker(request, address):
            nonlocal entered
            with entered_lock:
                entered += 1
            return original_worker(request, address)

        self.server.process_request_thread = track_worker
        try:
            a, b = self._slow(), self._slow()
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                with entered_lock:
                    if entered == 2:
                        break
                time.sleep(0.005)
            with entered_lock:
                self.assertEqual(2, entered, "Both held POST bodies must occupy workers")
            self._assert_busy_preheader_503()
            # Overloaded normal GET may deliver 503 or reset on Windows;
            # neither may dispatch a third worker or return successful HTTP.
            self.assertIn(self._status(allow_reset=True), (503, None))
            with entered_lock:
                self.assertEqual(2, entered, "Rejected GETs must not allocate workers")
        finally:
            self.server.process_request_thread = original_worker
        # The incomplete bodies have a hard deadline; after expiry, workers
        # should be reclaimed and an independent request admitted again.
        time.sleep(1.15)
        self.assertEqual(200, self._status())
        for client in (a, b):
            try:
                data = client.recv(512)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                data = b""
            self.assertTrue(data == b"" or b"408 Request Timeout" in data, data)

    def test_busy_preheader_probe_rejects_silent_close_only_implementation(self):
        self.assertTrue(self.server._worker_slots.acquire(blocking=False))
        self.assertTrue(self.server._worker_slots.acquire(blocking=False))
        try:
            with patch.object(self.server, "verify_request", return_value=False):
                with self.assertRaises(AssertionError):
                    self._assert_busy_preheader_503()
        finally:
            self.server._worker_slots.release()
            self.server._worker_slots.release()

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
