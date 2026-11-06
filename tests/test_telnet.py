"""Socket-level qualification of the local-only terminal adapter."""

import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.telnet import _TelnetFilter, LocalTelnetServer


class TelnetFilterTests(unittest.TestCase):
    def test_negotiations_do_not_leak_into_commands(self):
        filt = _TelnetFilter()
        clean, reply = filt.feed(b"\xff\xfb\x01Pat\xff\xfd\x03rick\r\n")
        self.assertEqual(b"Patrick\r\n", clean)
        self.assertEqual(b"\xff\xfe\x01\xff\xfc\x03", reply)

    def test_subnegotiation_crosses_chunks(self):
        filt = _TelnetFilter()
        self.assertEqual((b"a", b""), filt.feed(b"a\xff\xfa\x18abc\xff"))
        self.assertEqual((b"bc", b""), filt.feed(b"\xf0bc"))


class TerminalServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Path(self.tempdir.name) / "test.sqlite3"
        self.engine = Engine(self.db)
        self.server = LocalTelnetServer(self.engine, "test-secret", 0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def connect(self):
        conn = socket.create_connection(self.server.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        return conn

    def receive_until(self, conn, marker):
        data = bytearray()
        for _ in range(20):
            data.extend(conn.recv(4096))
            if marker in data:
                return data.decode("utf-8", errors="replace")
        self.fail(f"Server did not send {marker!r}; got {data!r}")

    def login(self, name):
        conn = self.connect()
        self.receive_until(conn, b"Token: ")
        conn.sendall(b"test-secret\r\n")
        self.receive_until(conn, b"Name: ")
        conn.sendall(name.encode() + b"\r\n")
        self.receive_until(conn, b"REALM> ")
        return conn

    def test_loopback_only_and_denies_unknown_token(self):
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        conn = self.connect()
        self.receive_until(conn, b"Token: ")
        conn.sendall(b"wrong\r\n")
        self.assertIn("Access denied", self.receive_until(conn, b"Access denied."))
        self.assertEqual(b"", conn.recv(1024))

    def test_two_players_share_actions_and_persist_after_disconnect(self):
        alice = self.login("Alice")
        bob = self.login("Bob")
        alice.sendall(b"say Hello from Alice\r\n")
        self.assertIn("You say: Hello from Alice", self.receive_until(alice, b"REALM> "))
        bob.sendall(b"listen\r\n")
        self.assertIn("Alice said: Hello from Alice", self.receive_until(bob, b"REALM> "))
        alice.sendall(b"north\r\n")
        self.assertIn("The Corridor", self.receive_until(alice, b"REALM> "))
        alice.sendall(b"quit\r\n")
        self.assertIn("position is saved", self.receive_until(alice, b"position is saved"))
        rejoined = self.login("Alice")
        rejoined.sendall(b"where\r\n")
        self.assertIn("The Corridor", self.receive_until(rejoined, b"REALM> "))

    def test_telnet_iac_and_cr_nul(self):
        conn = self.connect()
        self.receive_until(conn, b"Token: ")
        conn.sendall(b"\xff\xfb\x01test-secret\r\x00")
        self.receive_until(conn, b"Name: ")
        conn.sendall(b"Ari\r\x00")
        self.receive_until(conn, b"REALM> ")
        conn.sendall(b"\xff\xfd\x03look\r\x00")
        self.assertIn("The Atrium", self.receive_until(conn, b"REALM> "))

    def test_invalid_name_and_oversized_input(self):
        conn = self.connect()
        self.receive_until(conn, b"Token: ")
        conn.sendall(b"test-secret\r\n")
        self.receive_until(conn, b"Name: ")
        conn.sendall(b"../admin\r\n")
        self.assertIn("Invalid player name", self.receive_until(conn, b"Invalid player name"))

        other = self.login("Eve")
        other.sendall(b"x" * 513 + b"\r\n")
        self.assertIn("Input rejected", self.receive_until(other, b"Input rejected"))


if __name__ == "__main__":
    unittest.main()
