"""A trickle-fed local model must not occupy a host turn indefinitely."""

from __future__ import annotations

import socketserver
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.npc_local_model import LocalChatConfig, LocalChatProposer


class _TrickleHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        incoming = bytearray()
        while b"\r\n\r\n" not in incoming and len(incoming) < 8192:
            packet = self.request.recv(2048)
            if not packet:
                return
            incoming.extend(packet)
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 1000\r\n"
            b"Connection: close\r\n\r\n"
        )
        for _ in range(80):
            try:
                self.request.sendall(b" ")
            except OSError:
                return
            time.sleep(0.04)


class LocalModelDeadlineTests(unittest.TestCase):
    def test_trickle_cannot_extend_local_response_forever(self):
        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TrickleHandler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            proposer = LocalChatProposer(LocalChatConfig(
                endpoint=f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                model="fixture", timeout_seconds=0.2,
            ))
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                proposer.propose("Only host-selected scene text")
            self.assertLess(time.monotonic() - started, 1.2)


if __name__ == "__main__":
    unittest.main()
