"""A CLI listener must not return while an accepted socket can still write."""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer, main as gateway_main


class GatewayCliShutdownTests(unittest.TestCase):
    def _exercise_shutdown_with_keepalive(self, *, fail_server_close: bool):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "world.sqlite3"
            gateway = Gateway(db_path, world_id="cli_quiescence")
            token = gateway.credentials.provision("CliTester")
            headers = {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Connection": "keep-alive",
            }
            bound = []
            listener_ready = threading.Event()
            outcome = []
            serve_forever = GatewayServer.serve_forever

            def observe_listener(server, *args, **kwargs):
                bound.append(server)
                if fail_server_close:
                    original_close = server.server_close

                    def close_then_raise():
                        original_close()
                        raise OSError("synthetic server_close failure")

                    server.server_close = close_then_raise
                listener_ready.set()
                return serve_forever(server, *args, **kwargs)

            def foreground():
                try:
                    outcome.append(gateway_main([
                        "--db", str(db_path), "--world-id", "cli_quiescence",
                        "--port", "0",
                    ]))
                except BaseException as error:
                    outcome.append(error)

            conn = None
            with patch.object(GatewayServer, "serve_forever", observe_listener):
                thread = threading.Thread(target=foreground, daemon=True)
                thread.start()
                try:
                    self.assertTrue(listener_ready.wait(2), "CLI did not construct its listener")
                    server = bound[0]
                    for _ in range(100):
                        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                        try:
                            conn.request("POST", "/api/v1/join",
                                         json.dumps({"player": "CliTester"}), headers)
                            response = conn.getresponse()
                            self.assertEqual(200, response.status)
                            response.read()
                            break
                        except OSError:
                            conn.close()
                            time.sleep(0.01)
                    else:
                        self.fail("HTTP session was not accepted")
                    self.assertIsNotNone(conn.sock, "Test needs an accepted keep-alive session")

                    server.shutdown()
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive(), "CLI failed to return after shutdown")
                    if fail_server_close:
                        self.assertEqual(1, len(outcome))
                        self.assertIsInstance(outcome[0], OSError)
                        self.assertIn("synthetic server_close failure", str(outcome[0]))
                    else:
                        self.assertEqual([0], outcome)

                    try:
                        conn.request("POST", "/api/v1/command", json.dumps({
                            "player": "CliTester", "command": "say after-stop",
                            "action_id": "cli-after-stop",
                        }), headers)
                        reply = conn.getresponse()
                        status = reply.status
                        reply.read()
                    except (OSError, HTTPException):
                        status = None
                    self.assertNotEqual(200, status, "A stopped CLI accepted a late command")
                    with closing(sqlite3.connect(db_path)) as db:
                        post_stop_speech = db.execute(
                            "SELECT payload_json FROM events WHERE kind='say' AND actor='CliTester'"
                        ).fetchall()
                    self.assertEqual([], post_stop_speech, "World mutated after CLI host return")
                finally:
                    if conn is not None:
                        conn.close()
                    if bound and thread.is_alive():
                        bound[0].shutdown()
                    thread.join(timeout=3)

    def test_cli_shutdown_revokes_existing_authenticated_keepalive(self):
        self._exercise_shutdown_with_keepalive(fail_server_close=False)

    def test_cli_shutdown_closes_existing_session_even_if_listener_close_raises(self):
        self._exercise_shutdown_with_keepalive(fail_server_close=True)

    def test_cli_startup_output_error_does_not_leave_listening_port(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "world.sqlite3"
            bound = []
            original_init = GatewayServer.__init__

            def observe_init(server, *args, **kwargs):
                original_init(server, *args, **kwargs)
                bound.append(server)

            try:
                with patch.object(GatewayServer, "__init__", observe_init):
                    with patch("builtins.print", side_effect=OSError("synthetic stdout failure")):
                        with self.assertRaisesRegex(OSError, "synthetic stdout failure"):
                            gateway_main(["--db", str(db_path), "--world-id", "print_fail", "--port", "0"])
                self.assertEqual(1, len(bound))
                with self.assertRaises(OSError):
                    with closing(socket.create_connection(
                            ("127.0.0.1", bound[0].server_port), timeout=0.2)):
                        pass
            finally:
                for server in bound:
                    server.server_close()

    def test_cli_session_custody_initialization_failure_closes_port(self):
        from realm import local_host_runner

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "world.sqlite3"
            bound = []
            original_init = GatewayServer.__init__

            def observe_init(server, *args, **kwargs):
                original_init(server, *args, **kwargs)
                bound.append(server)

            try:
                with patch.object(GatewayServer, "__init__", observe_init):
                    with patch.object(local_host_runner, "_AcceptedSessions",
                                      side_effect=RuntimeError("synthetic custody init failure")):
                        with self.assertRaisesRegex(RuntimeError, "synthetic custody init failure"):
                            gateway_main([
                                "--db", str(db_path), "--world-id", "custody_fail", "--port", "0",
                            ])
                self.assertEqual(1, len(bound))
                with self.assertRaises(OSError):
                    with closing(socket.create_connection(
                            ("127.0.0.1", bound[0].server_port), timeout=0.2)):
                        pass
            finally:
                for server in bound:
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
