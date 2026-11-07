"""Shared-token Telnet must not bypass player-bound HTTP world identity.

All worlds are disposable; these tests do not make Telnet publicly secure.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.gateway import Gateway
from realm.telnet import LocalTelnetServer


class TransportAuthBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"

    def test_http_claim_prevents_telnet_shared_token_character_impersonation(self):
        gateway = Gateway(self.path, world_id="test")
        token = gateway.credentials.provision("Alice")
        gateway.join("Alice")
        telnet = LocalTelnetServer(Engine(self.path), "shared-demo-token", 0)
        self.addCleanup(telnet.server_close)
        with self.assertRaisesRegex(ValueError, "(?i)transport|auth"):
            telnet.join_exclusive("Alice")
        self.assertEqual("Alice", gateway.credentials.authenticate(token))

    def test_telnet_claim_prevents_later_explicit_gateway_adoption(self):
        telnet = LocalTelnetServer(Engine(self.path), "shared-demo-token", 0)
        self.addCleanup(telnet.server_close)
        self.assertIsNotNone(telnet.join_exclusive("Alice"))
        with self.assertRaisesRegex(ValueError, "(?i)transport|auth"):
            Gateway(self.path, world_id="test", adopt_unbound_world=True)
        with sqlite3.connect(self.path) as db:
            self.assertIsNone(db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'"
            ).fetchone())

    def test_deleted_transport_marker_does_not_make_http_credentials_telnet_claimable(self):
        gateway = Gateway(self.path, world_id="test")
        gateway.credentials.provision("Alice")
        gateway.join("Alice")
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM world_meta WHERE key='transport_auth_mode'")
        with self.assertRaisesRegex(ValueError, "(?i)transport|auth|credential"):
            Engine(self.path).join("Alice", transport_auth_mode="shared-token-demo")

    def test_concurrent_adapter_claims_have_one_winner(self):
        barrier = threading.Barrier(2)

        def telnet_claim():
            engine = Engine(self.path)
            server = LocalTelnetServer(engine, "shared-demo-token", 0)
            try:
                barrier.wait(timeout=3)
                server.join_exclusive("Alice")
                return "telnet"
            except ValueError:
                return "denied"
            finally:
                server.server_close()

        def gateway_claim():
            barrier.wait(timeout=3)
            try:
                Gateway(self.path, world_id="test", adopt_unbound_world=True)
                return "http"
            except ValueError:
                return "denied"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda f: f(), (telnet_claim, gateway_claim)))
        self.assertEqual(1, results.count("denied"), results)
        self.assertEqual(1, len([x for x in results if x in {"http", "telnet"}]), results)


if __name__ == "__main__":
    unittest.main()
