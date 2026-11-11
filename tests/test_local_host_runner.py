"""Real loopback clients sharing a bounded opt-in host UTC runner."""
from __future__ import annotations

import errno
import json
from http.client import HTTPConnection, HTTPException
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.gateway import Gateway, GatewayHandler, GatewayServer
from realm.telnet import LocalTelnetServer
from realm.world_clock import WorldClock
from realm.local_host_runner import _AcceptedSessions, run_local_host


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

    def test_shutdown_socket_abort_reporting_is_narrowly_scoped(self):
        """An intentional Windows disconnect must not look like a host crash.

        Unexpected handler exceptions and aborts during active service remain
        visible through socketserver's normal error handler.
        """
        http, _ = self.servers()
        reported = []
        http.handle_error = lambda request, address: reported.append(sys.exc_info()[1])
        custody = _AcceptedSessions(http)

        def deliver(error, tracked_request):
            try:
                raise error
            except Exception:
                http.handle_error(tracked_request, ("127.0.0.1", 1))

        def winerror(code):
            error = OSError(code, "synthetic Windows socket abort")
            error.winerror = code
            return error

        chosen, peer = socket.socketpair()
        unrelated = socket.socket()
        self.addCleanup(unrelated.close)
        self.addCleanup(peer.close)
        self.addCleanup(chosen.close)
        with custody._condition:
            custody._active.add(chosen)
        for code in (10038, 10053):
            deliver(winerror(code), chosen)
        self.assertEqual([10038, 10053], [e.winerror for e in reported])

        stopper = threading.Thread(target=custody.quiesce)
        stopper.start()
        try:
            self.assertTrue(custody._quiescing.wait(2), "Shutdown never began")
            for code in (10038, 10053):
                deliver(winerror(code), chosen)
            self.assertEqual(2, len(reported), "Only selected shutdown sockets become quiet")

            deliver(winerror(10053), unrelated)
            self.assertEqual(3, len(reported), "Unrelated socket abort must still be reported")
            deliver(winerror(10054), chosen)
            deliver(RuntimeError("unexpected worker failure"), chosen)
            deliver(OSError(errno.EBADF, "application bad file descriptor"), chosen)
            self.assertEqual(6, len(reported), "Unexpected application errors remain visible")
        finally:
            with custody._condition:
                custody._active.discard(chosen)
                custody._condition.notify_all()
            stopper.join(timeout=2)
        self.assertFalse(stopper.is_alive(), "Quiesce did not finish")
        deliver(winerror(10053), chosen)
        self.assertEqual(7, len(reported), "Revoked-socket suppression must end with quiescence")

    def test_posix_response_write_bad_fd_requires_exact_revoked_socket(self):
        """Only the selected HTTP transport write failure is suppressible."""
        http, _ = self.servers()
        custody = _AcceptedSessions(http)
        selected, peer = socket.socketpair()
        unrelated = socket.socket()
        self.addCleanup(selected.close)
        self.addCleanup(peer.close)
        self.addCleanup(unrelated.close)

        handler = object.__new__(GatewayHandler)
        handler.server = http
        handler.connection = selected
        handler._headers = Mock(side_effect=OSError(errno.EBADF, "closed response descriptor"))
        with self.assertRaises(OSError, msg="Normal HTTP response failures stay visible"):
            GatewayHandler._send(handler, 200, {"ok": True})

        with custody._condition:
            custody._active.add(selected)
        stopper = threading.Thread(target=custody.quiesce)
        stopper.start()
        try:
            self.assertTrue(custody._quiescing.wait(2))
            handler.close_connection = False
            GatewayHandler._send(handler, 200, {"ok": True})
            self.assertTrue(handler.close_connection)

            handler.connection = unrelated
            with self.assertRaises(OSError, msg="Unrelated connection failure must propagate"):
                GatewayHandler._send(handler, 200, {"ok": True})
        finally:
            with custody._condition:
                custody._active.discard(selected)
                custody._condition.notify_all()
            stopper.join(timeout=2)
        self.assertFalse(stopper.is_alive())
        handler.connection = selected
        with self.assertRaises(OSError, msg="Suppression must end after quiescence"):
            GatewayHandler._send(handler, 200, {"ok": True})

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

    def test_existing_http_and_telnet_sessions_cannot_mutate_after_host_returns(self):
        """Closing only listening ports leaves keep-alive handler threads alive."""
        http, telnet = self.servers()
        token = self.gateway.credentials.provision("Captain")
        stop = threading.Event()
        outcomes = []
        errors = []

        def foreground():
            try:
                outcomes.append(run_local_host(
                    http, telnet, self.clock, {"unused": lambda db, event: None},
                    pulses=100, interval_seconds=0.05, now=lambda: UTC,
                    stop_event=stop,
                ))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=foreground)
        thread.start()
        telnet_socket = None
        conn = HTTPConnection("127.0.0.1", http.server_port, timeout=2)
        try:
            for _ in range(100):
                try:
                    self.http_json(http, "/api/v1/health")
                    break
                except OSError:
                    time.sleep(0.01)
            else:
                self.fail("No running HTTP listener")
            conn.request("POST", "/api/v1/join", json.dumps({"player": "Captain"}),
                         {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            joined = conn.getresponse()
            self.assertEqual(200, joined.status)
            joined.read()
            self.assertIsNotNone(conn.sock, "HTTP persistent connection was not retained")

            telnet_socket = socket.create_connection(("127.0.0.1", telnet.server_address[1]), timeout=2)
            telnet_socket.settimeout(2)
            self.read_until(telnet_socket, b"Token: ")
            telnet_socket.sendall(b"local-test-only\n")
            self.read_until(telnet_socket, b"Name: ")
            telnet_socket.sendall(b"Navigator\n")
            self.read_until(telnet_socket, b"REALM>")

            stop.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "Runner must quiesce sessions before returning")
            self.assertFalse(errors, errors)
            self.assertEqual("stopped", outcomes[0].reason)

            try:
                telnet_socket.sendall(b"say forbidden after host return\n")
                telnet_socket.recv(4096)
            except OSError:
                pass  # Closed/revoked session is the expected transport behavior.
            try:
                conn.request("POST", "/api/v1/command",
                             json.dumps({"player": "Captain", "command": "say forbidden http"}),
                             {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                response = conn.getresponse()
                response.read()
            except (OSError, HTTPException):
                pass
            with closing(sqlite3.connect(self.db_path)) as db:
                durable = db.execute(
                    "SELECT actor,payload_json FROM events WHERE kind='say' AND actor IN ('Captain','Navigator')"
                ).fetchall()
            self.assertEqual([], durable, "No accepted session may mutate after host shutdown")
        finally:
            stop.set()
            thread.join(timeout=3)
            conn.close()
            if telnet_socket is not None:
                telnet_socket.close()

    def test_noninterruptible_trusted_http_action_must_finish_before_host_returns(self):
        """A failed shutdown timeout cannot leave a live durable writer behind."""
        http, telnet = self.servers()
        unhandled_http_errors = []
        http.handle_error = lambda request, address: unhandled_http_errors.append(sys.exc_info()[1])
        token = self.gateway.credentials.provision("Captain")
        stop = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        outcomes = []
        original_command = self.gateway.command

        def deliberately_blocked_command(*args, **kwargs):
            entered.set()
            try:
                if not release.wait(timeout=12):
                    raise RuntimeError("Host test action unexpectedly held too long")
                return original_command(*args, **kwargs)
            finally:
                finished.set()

        self.gateway.command = deliberately_blocked_command

        def foreground():
            try:
                outcomes.append(("returned", run_local_host(
                    http, telnet, self.clock, {"unused": lambda db, event: None},
                    pulses=100, interval_seconds=0.05, now=lambda: UTC,
                    stop_event=stop,
                )))
            except BaseException as exc:
                outcomes.append(("raised", exc))

        host_thread = threading.Thread(target=foreground)
        host_thread.start()
        sender = None
        conn = HTTPConnection("127.0.0.1", http.server_port, timeout=2)
        try:
            for _ in range(100):
                try:
                    self.http_json(http, "/api/v1/health")
                    break
                except OSError:
                    time.sleep(0.01)
            else:
                self.fail("No HTTP listener")
            headers = {"Authorization": "Bearer " + token,
                       "Content-Type": "application/json"}
            conn.request("POST", "/api/v1/join", json.dumps({"player": "Captain"}), headers)
            response = conn.getresponse()
            self.assertEqual(200, response.status)
            response.read()

            def send_while_shutdown():
                try:
                    conn.request("POST", "/api/v1/command",
                                 json.dumps({"player": "Captain", "command": "say late host action"}),
                                 headers)
                    reply = conn.getresponse()
                    reply.read()
                except (OSError, HTTPException):
                    pass  # Socket closure is expected during host shutdown.

            sender = threading.Thread(target=send_while_shutdown)
            sender.start()
            self.assertTrue(entered.wait(timeout=2), "Trusted HTTP action never entered")
            stop.set()
            # The prior 5-second cleanup watchdog returned with a live writer.
            # Here the host MUST still own the blocked handler after that limit.
            host_thread.join(timeout=5.3)
            self.assertTrue(host_thread.is_alive(), "Host returned while an accepted handler could still commit")
            self.assertEqual([], outcomes)
            release.set()
            self.assertTrue(finished.wait(timeout=3))
            host_thread.join(timeout=3)
            self.assertFalse(host_thread.is_alive())
            self.assertEqual(1, len(outcomes))
            self.assertEqual("returned", outcomes[0][0], outcomes)
            self.assertEqual("stopped", outcomes[0][1].reason)
            with closing(sqlite3.connect(self.db_path)) as db:
                rows = db.execute(
                    "SELECT payload_json FROM events WHERE kind='say' AND actor='Captain'"
                ).fetchall()
            self.assertEqual([{"text": "late host action"}], [json.loads(row[0]) for row in rows])
            self.assertEqual([], unhandled_http_errors,
                             "Expected revoked-socket response failure must not escape to socketserver")
        finally:
            release.set()
            if sender is not None:
                sender.join(timeout=3)
            stop.set()
            host_thread.join(timeout=3)
            conn.close()

    def test_application_bad_fd_stays_reported_on_revoked_http_socket(self):
        """A trusted command's EBADF is not an ignorable response transport error."""
        http, telnet = self.servers()
        token = self.gateway.credentials.provision("Captain")
        stop = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        errors = []
        outcomes = []
        http.handle_error = lambda request, address: errors.append(sys.exc_info()[1])

        def broken_application_command(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=8):
                raise RuntimeError("Application test was never released")
            raise OSError(errno.EBADF, "application opened an invalid descriptor")

        self.gateway.command = broken_application_command

        def foreground():
            try:
                outcomes.append(run_local_host(
                    http, telnet, self.clock, {"unused": lambda db, event: None},
                    pulses=100, interval_seconds=0.05, now=lambda: UTC,
                    stop_event=stop,
                ))
            except BaseException as error:
                outcomes.append(error)

        host_thread = threading.Thread(target=foreground)
        host_thread.start()
        sender = None
        conn = HTTPConnection("127.0.0.1", http.server_port, timeout=2)
        try:
            for _ in range(100):
                try:
                    self.http_json(http, "/api/v1/health")
                    break
                except OSError:
                    time.sleep(0.01)
            else:
                self.fail("HTTP host did not begin serving")
            headers = {"Authorization": "Bearer " + token,
                       "Content-Type": "application/json"}
            conn.request("POST", "/api/v1/join", json.dumps({"player": "Captain"}), headers)
            reply = conn.getresponse()
            self.assertEqual(200, reply.status)
            reply.read()

            def issue_command():
                try:
                    conn.request("POST", "/api/v1/command", json.dumps({
                        "player": "Captain", "command": "say must not commit"
                    }), headers)
                    conn.getresponse().read()
                except (OSError, HTTPException):
                    pass

            sender = threading.Thread(target=issue_command)
            sender.start()
            self.assertTrue(entered.wait(timeout=2), "Trusted command never entered")
            stop.set()
            revoked = getattr(http, "_realm_local_host_revoked", None)
            self.assertIsNotNone(revoked)
            self.assertTrue(revoked.__self__._quiescing.wait(timeout=3),
                            "Socket not selected for host quiescence")
            release.set()
            host_thread.join(timeout=3)
            self.assertFalse(host_thread.is_alive())
            self.assertEqual("stopped", outcomes[0].reason)
            self.assertEqual(1, len(errors), errors)
            self.assertIsInstance(errors[0], OSError)
            self.assertEqual(errno.EBADF, errors[0].errno)
            with closing(sqlite3.connect(self.db_path)) as db:
                self.assertEqual(0, db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='say' AND actor='Captain'"
                ).fetchone()[0])
        finally:
            release.set()
            stop.set()
            if sender is not None:
                sender.join(timeout=3)
            host_thread.join(timeout=3)
            conn.close()

    def test_wrong_world_rejected_before_dispatch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other_path = Path(tmp.name) / "other.sqlite3"
        http, telnet = self.servers(db_path=other_path)
        with self.assertRaisesRegex(ValueError, "same world"):
            run_local_host(http, telnet, self.clock, {"host_action": lambda db, e: None},
                           pulses=1, now=lambda: UTC)
        self.assert_ports_released(http, telnet)

    def assert_ports_released(self, http, telnet):
        """A failed foreground admission must not leave hidden listeners."""
        ports = (http.server_port, telnet.server_address[1])
        for port in ports:
            with self.assertRaises(OSError):
                with closing(socket.create_connection(("127.0.0.1", port), timeout=0.25)):
                    pass
        # Confirm ordinary retry can immediately bind both exact old ports.
        with ExitStack() as cleanup:
            rebound_http = GatewayServer(http.gateway, ("http://127.0.0.1:8080",), ports[0])
            cleanup.callback(rebound_http.server_close)
            rebound_telnet = LocalTelnetServer(Engine(telnet.engine.db_path),
                                              "fresh-test-token", ports[1])
            cleanup.callback(rebound_telnet.server_close)

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
                self.assert_ports_released(http, telnet)
        http, telnet = self.servers()
        with self.assertRaises(ValueError):
            run_local_host(http, telnet, self.clock, {}, pulses=1, now=lambda: UTC)
        self.assert_ports_released(http, telnet)


if __name__ == "__main__":
    unittest.main()
