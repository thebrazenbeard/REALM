"""Transport aborts during header parsing must not leak handler tracebacks."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import GatewayHandler


class HeaderAbortTests(unittest.TestCase):
    @staticmethod
    def handler():
        handler = object.__new__(GatewayHandler)
        handler.rfile = SimpleNamespace(peek=lambda size: b"G")
        handler.server = SimpleNamespace(header_deadline_seconds=10)
        handler.close_connection = False
        return handler

    def test_disconnected_peer_during_header_parse_closes_without_uncaught_error(self):
        handler = self.handler()
        with patch.object(BaseHTTPRequestHandler, "handle_one_request",
                          side_effect=ConnectionAbortedError("peer dropped headers")):
            handler.handle_one_request()
        self.assertTrue(handler.close_connection)

    def test_application_abort_after_valid_headers_must_still_propagate(self):
        handler = self.handler()

        def fail_after_headers():
            handler._finish_header_guard()
            raise ConnectionAbortedError("application failed")

        with patch.object(BaseHTTPRequestHandler, "handle_one_request",
                          side_effect=fail_after_headers):
            with self.assertRaisesRegex(ConnectionAbortedError, "application failed"):
                handler.handle_one_request()


if __name__ == "__main__":
    unittest.main()
