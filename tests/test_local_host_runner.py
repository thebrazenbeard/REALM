"""Real loopback clients sharing a bounded opt-in host UTC runner."""
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
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.gateway import Gateway, GatewayServer
from realm.telnet import LocalTelnetServer
from realm.world_clock import WorldClock
from realm.local_host_runner import run_local_host


UTC = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


class LocalHostRunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "one-world.sqlite3"
        self.gateway = Gateway(self.db_path, world_id="local_runner")
        self.clock = WorldClock(self.db_path)
        self.clock.enable()

    def servers(self, db_path=None):
        gateway = self.gateway if db_path is None else Gateway(db_path, world_id="other_world")
        http = GatewayServer(gateway, ("http://127.0.0.1:8080",), port=0)
        telnet = LocalTelnetServer(Engine(gateway.engine.db_path), "local-test-only", port=0)
        self.addCleanup(http.server_close)
        self.addCleanup(telnet.server_close)
        return http, telnet

    @staticmethod
    def read_until(sock, marker: bytes):
        received = bytearray()
        while marker not in received:
            chunk = sock.recv(4096)
            if not chunk:
                raise AssertionError("Telnet socket closed before expected prompt")
            received.extend(chunk)
        return bytes(received)

    def http_json(self, server, path, *, token=None, body=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        req = request.Request(
            f"http://127.0.0.1:{server.server_port}" + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers,
        )
        with request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)

    def test_two_live_clients_receive_one_trusted_utc_event_and_stop_closes_ports(self):
        http, telnet = self.servers()
        token = self.gateway.credentials.provision("Captain")
        stop = threading.Event()
        handled = threading.Event()
        result = []
        failures = []

        def trusted_handler(db, event):
            db.execute(
                "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) VALUES(?,?,?,?,?)",
                (UTC.isoformat(), "ClockBot", "atrium", "say", json.dumps({"text": "Host UTC tick"})),
            )
            handled.set()

        def foreground():
            try:
                result.append(run_local_host(
                    http, telnet, self.clock, {"host_say": trusted_handler},
                    pulses=100, interval_seconds=0.05, now=lambda: UTC, stop_event=stop,
                ))
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=foreground, name="test-foreground")
        thread.start()
        try:
            for _ in range(100):
                try:
                    status, body = self.http_json(http, "/api/v1/health")
                    break
                except OSError:
                    time.sleep(0.01)
            else:
                self.fail("HTTP endpoint never became ready")
            self.assertEqual(200, status)
            self.assertEqual("local_runner", body["world_id"])
            with closing(socket.create_connection(("127.0.0.1", telnet.server_address[1]), timeout=3)) as sock:
                sock.settimeout(3)
                self.read_until(sock, b"Token: ")
                sock.sendall(b"local-test-only\n")
                self.read_until(sock, b"Name: ")
                sock.sendall(b"Navigator\n")
                self.assertIn(b"REALM>", self.read_until(sock, b"REALM>"))
                status, joined = self.http_json(http, "/api/v1/join", token=token, body={"player": "Captain"})
                self.assertEqual(200, status)
                self.assertTrue(joined["ok"])
                sock.sendall(b"say Navigator online\n")
                self.assertIn(b"You say", self.read_until(sock, b"REALM>"))
                self.clock.schedule("host-once", due_at=UTC, kind="host_say", payload={})
                self.assertTrue(handled.wait(timeout=3), "UTC pump never dispatched scheduled event")
                for _ in range(100):
                    status, feed = self.http_json(http, "/api/v1/feed?player=Captain&after=0", token=token)
                    utterances = [event["text"] for event in feed["events"]]
                    if "ClockBot: Host UTC tick" in utterances:
                        break
                    time.sleep(0.01)
                self.assertEqual(200, status)
                self.assertIn("Navigator: Navigator online", utterances)
                self.assertEqual(1, utterances.count("ClockBot: Host UTC tick"))
        finally:
            stop.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "Foreground host did not shut down")
        self.assertFalse(failures, failures)
        self.assertEqual(("host-once",), result[0].event_ids)
        self.assertEqual("stopped", result[0].reason)
        self.assertIsNotNone(self.clock.lookup("host-once").processed_at)
        for port in (http.server_port, telnet.server_address[1]):
            with self.assertRaises(OSError):
                with closing(socket.create_connection(("127.0.0.1", port), timeout=0.25)):
                    pass

    def test_failed_callback_rolls_back_and_is_retryable(self):
        self.clock.schedule("retry-once", due_at=UTC, kind="host_action", payload={})

        def failing(db, event):
            db.execute("UPDATE world_meta SET value='tampered' WHERE key='world_id'")
            raise RuntimeError("synthetic host handler crash")

        http, telnet = self.servers()
        with self.assertRaisesRegex(RuntimeError, "synthetic host handler crash"):
            run_local_host(http, telnet, self.clock, {"host_action": failing},
                           pulses=1, now=lambda: UTC)
        self.assertIsNone(self.clock.lookup("retry-once").processed_at)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual("local_runner", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
        for _ in range(2):
            http, telnet = self.servers()
            report = run_local_host(http, telnet, self.clock,
                                    {"host_action": lambda db, event: None},
                                    pulses=1, now=lambda: UTC)
            self.assertEqual(("retry-once",) if _ == 0 else (), report.event_ids)
        self.assertIsNotNone(self.clock.lookup("retry-once").processed_at)

    def test_wrong_world_rejected_before_dispatch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other_path = Path(tmp.name) / "other.sqlite3"
        http, telnet = self.servers(db_path=other_path)
        with self.assertRaisesRegex(ValueError, "same world"):
            run_local_host(http, telnet, self.clock, {"host_action": lambda db, e: None},
                           pulses=1, now=lambda: UTC)

    def test_pre_stopped_host_closes_ports_without_processing(self):
        self.clock.schedule("still-due", due_at=UTC, kind="host_action", payload={})
        http, telnet = self.servers()
        stop = threading.Event()
        stop.set()
        report = run_local_host(http, telnet, self.clock,
                                {"host_action": lambda db, event: self.fail("unexpected")},
                                pulses=2, now=lambda: UTC, stop_event=stop)
        self.assertEqual((0, (), "stopped"), (report.pulses, report.event_ids, report.reason))
        self.assertIsNone(self.clock.lookup("still-due").processed_at)
        for port in (http.server_port, telnet.server_address[1]):
            with self.assertRaises(OSError):
                with closing(socket.create_connection(("127.0.0.1", port), timeout=0.25)):
                    pass

    def test_invalid_bounds_and_empty_registry(self):
        for opts in ({"pulses": 0}, {"pulses": 1001},
                     {"pulses": 1, "interval_seconds": float("inf")},
                     {"pulses": 1000, "batch_limit": 1000}):
            with self.subTest(opts=opts):
                http, telnet = self.servers()
                with self.assertRaises(ValueError):
                    run_local_host(http, telnet, self.clock,
                                   {"host_action": lambda db, event: None},
                                   now=lambda: UTC, **opts)
        http, telnet = self.servers()
        with self.assertRaises(ValueError):
            run_local_host(http, telnet, self.clock, {}, pulses=1, now=lambda: UTC)


if __name__ == "__main__":
    unittest.main()
