"""Socket-level qualification of the local-only terminal adapter."""

import socket
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def wait_for_inactive_name(self, name):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not self.server.is_name_active(name):
                return
            time.sleep(0.01)
        self.fail(f"Name {name!r} was not released after disconnect")

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
        self.wait_for_inactive_name("EVE")
        self.assertIsNotNone(self.login("Eve"))

    def test_casefolded_name_is_exclusive_until_quit(self):
        alice = self.login("Alice")
        self.assertTrue(self.server.is_name_active("ALICE"))
        other = self.connect()
        self.receive_until(other, b"Token: ")
        other.sendall(b"test-secret\r\n")
        self.receive_until(other, b"Name: ")
        other.sendall(b"aLiCe\r\n")
        self.assertIn("already connected", self.receive_until(other, b"already connected"))
        self.assertEqual(b"", other.recv(1024))
        alice.sendall(b"quit\r\n")
        self.receive_until(alice, b"position is saved")
        self.wait_for_inactive_name("Alice")
        rejoined = self.login("aLiCe")
        rejoined.sendall(b"where\r\n")
        self.assertIn("The Atrium", self.receive_until(rejoined, b"REALM> "))

    def test_abrupt_disconnect_releases_name(self):
        player = self.login("Vince")
        self.assertTrue(self.server.is_name_active("vince"))
        player.shutdown(socket.SHUT_RDWR)
        player.close()
        self.wait_for_inactive_name("VINCE")
        rejoined = self.login("Vince")
        rejoined.sendall(b"look\r\n")
        self.assertIn("The Atrium", self.receive_until(rejoined, b"REALM> "))

    def test_two_socket_clients_compete_for_one_persistent_item(self):
        alice = self.login("Alice")
        bob = self.login("Bob")
        barrier = threading.Barrier(2)

        def attempt_take(conn):
            barrier.wait(timeout=2)
            conn.sendall(b"take token\r\n")
            return self.receive_until(conn, b"REALM> ")

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(attempt_take, (alice, bob)))
        self.assertEqual(1, sum("You take Brass token." in reply for reply in responses))
        self.assertEqual(1, sum("That item is not here." in reply for reply in responses))
        alice.sendall(b"inventory\r\n")
        a_inventory = self.receive_until(alice, b"REALM> ")
        bob.sendall(b"inventory\r\n")
        b_inventory = self.receive_until(bob, b"REALM> ")
        self.assertEqual(1, sum("Brass token" in inv for inv in (a_inventory, b_inventory)))


if __name__ == "__main__":
    unittest.main()
