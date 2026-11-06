"""Regression tests for the persistent database-to-world identity boundary.

Each REALM database is a single world in the current gateway architecture.
`world_id` must therefore be durable rather than a cosmetic HTTP label: a
second gateway must not be able to relabel an already-populated world.
"""

import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway
from realm.engine import Engine
from realm.auth import CredentialStore


class GatewayWorldBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "shared.sqlite3"

    def test_reopening_a_bound_database_with_another_world_id_is_denied(self):
        alpha = Gateway(self.db, world_id="alpha")
        alpha.join("Alice")
        alpha.command("Alice", "say alpha confidential", action_id="alpha-say-1")

        # Regression: the old implementation reported world_id='beta' in
        # /health while returning Alice and her speech from world 'alpha'.
        # A gateway instance is constructed before it can serve HTTP, so
        # reject identity confusion at construction, including restart.
        with self.assertRaisesRegex(ValueError, "world"):
            Gateway(self.db, world_id="beta")

    def test_reopening_a_bound_database_with_same_world_id_preserves_state(self):
        first = Gateway(self.db, world_id="alpha")
        first.join("Alice")
        first.command("Alice", "say alpha continuity", action_id="alpha-say-2")

        restarted = Gateway(self.db, world_id="alpha")
        self.assertEqual("alpha", restarted.world_id)
        self.assertIn("Atrium", restarted.room("Alice")["text"])
        self.assertTrue(
            any("alpha continuity" in event["text"] for event in restarted.feed("Alice", 0)["events"])
        )

    def test_empty_database_is_also_bound_to_its_world_id(self):
        Gateway(self.db, world_id="alpha")
        with self.assertRaisesRegex(ValueError, "world"):
            Gateway(self.db, world_id="beta")

    def test_unbound_existing_play_is_not_silently_relabelled(self):
        old_world = Engine(self.db)
        old_world.join("Alice")
        with self.assertRaisesRegex(ValueError, "adoption"):
            Gateway(self.db, world_id="alpha")
        adopted = Gateway(self.db, world_id="alpha", adopt_unbound_world=True)
        self.assertIn("Atrium", adopted.room("Alice")["text"])
        with self.assertRaisesRegex(ValueError, "world"):
            Gateway(self.db, world_id="beta", adopt_unbound_world=True)

    def test_two_initial_host_identities_cannot_race_into_one_database(self):
        def open_world(world_id):
            try:
                Gateway(self.db, world_id=world_id)
            except ValueError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = list(pool.map(open_world, ("alpha", "beta")))
        self.assertEqual(1, sum(accepted))

    def test_unbound_credentials_cannot_be_silently_reused_under_new_world(self):
        old_engine = Engine(self.db)
        token = CredentialStore(old_engine).provision("Alice")
        with self.assertRaisesRegex(ValueError, "adoption"):
            Gateway(self.db, world_id="beta")
        adopted = Gateway(self.db, world_id="alpha", adopt_unbound_world=True)
        self.assertEqual("Alice", adopted.credentials.authenticate(token))

    def test_unbound_authored_room_cannot_be_silently_relabelled(self):
        Engine(self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE rooms SET description='private text' WHERE id='atrium'")
        with self.assertRaisesRegex(ValueError, "adoption"):
            Gateway(self.db, world_id="beta")


if __name__ == "__main__":
    unittest.main()
