"""Host-owned offline builder and separate SQLite instance acceptance tests."""

from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.hosted_builder import HostedWorlds, WorldAccessError
from realm.worldpack import WorldpackError, read_worldpack


class HostedBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = HostedWorlds(self.root / "host")

    def create(self, world_id="one", *, host=None):
        return (host or self.host).create(
            world_id=world_id, name="World " + world_id,
            start_room="square", room_name="Old Square",
            description="An original, privately authored room.",
        )

    def test_offline_owned_worlds_are_distinct_instances(self):
        a = self.create("one")
        b = self.create("two")
        self.assertNotEqual(a, b)
        self.host.add_room("one", capability=a, room_id="orchard",
                           name="Orchard", description="Fruit trees.")
        self.host.link_rooms("one", capability=a, source="square",
                             direction="north", destination="orchard", back="south")
        self.assertIn("orchard", self.host.describe("one", capability=a))
        self.assertNotIn("orchard", self.host.describe("two", capability=b))
        a_result = self.host.provision_instance("one", capability=a)
        b_result = self.host.provision_instance("two", capability=b)
        self.assertEqual(a_result["world_id"], "one")
        self.assertEqual(b_result["world_id"], "two")
        a_db = self.host.instance_path("one", capability=a)
        b_db = self.host.instance_path("two", capability=b)
        self.assertNotEqual(a_db, b_db)
        ea, eb = Engine(a_db), Engine(b_db)
        self.assertIn("Orchard", ea.join("SameName") + ea.act("SameName", "north"))
        self.assertIn("Old Square", eb.join("SameName"))
        self.assertIn("No exit", eb.act("SameName", "north"))
        with sqlite3.connect(a_db) as db_a, sqlite3.connect(b_db) as db_b:
            self.assertNotEqual(
                db_a.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone(),
                db_b.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone(),
            )
            self.assertEqual(1, db_a.execute("SELECT COUNT(*) FROM events WHERE kind='move'").fetchone()[0])
            self.assertEqual(0, db_b.execute("SELECT COUNT(*) FROM events WHERE kind='move'").fetchone()[0])

    def test_cross_world_capability_denied_before_any_edit_or_import(self):
        a, b = self.create("one"), self.create("two")
        one_pack = self.root / "host" / "one" / "worldpack.json"
        two_pack = self.root / "host" / "two" / "worldpack.json"
        old_a, old_b = one_pack.read_bytes(), two_pack.read_bytes()
        denied = [
            lambda: self.host.add_room("one", capability=b, room_id="secret", name="Secret", description="No."),
            lambda: self.host.link_rooms("two", capability=a, source="square", direction="north", destination="square"),
            lambda: self.host.add_item("two", capability="guessed", item_id="stolen", name="Item", description="No.", room_id="square"),
            lambda: self.host.describe("one", capability=b),
            lambda: self.host.provision_instance("two", capability=a),
            lambda: self.host.instance_path("one", capability=b),
            lambda: self.host.describe("does_not_exist", capability=a),
        ]
        for operation in denied:
            with self.subTest(operation=operation), self.assertRaisesRegex(
                WorldAccessError, "Host-owned world capability required",
            ):
                operation()
        self.assertEqual(old_a, one_pack.read_bytes())
        self.assertEqual(old_b, two_pack.read_bytes())
        self.assertFalse((self.root / "host" / "one" / "world.sqlite3").exists())
        self.assertFalse((self.root / "host" / "two" / "world.sqlite3").exists())

    def test_existing_world_is_not_silently_replaced_by_later_edits(self):
        token = self.create()
        self.host.provision_instance("one", capability=token)
        before = (self.root / "host" / "one" / "worldpack.json").read_bytes()
        with self.assertRaisesRegex(WorldpackError, "already provisioned"):
            self.host.add_room("one", capability=token, room_id="later",
                               name="Later", description="Not live OLC.")
        self.assertEqual(before, (self.root / "host" / "one" / "worldpack.json").read_bytes())
        repeat = self.host.provision_instance("one", capability=token)
        self.assertTrue(repeat["unchanged"])

    def test_owners_survive_manager_restart_and_are_not_stored_as_plaintext(self):
        token = self.create()
        metadata = (self.root / "host" / "one" / "owner.json").read_text()
        self.assertNotIn(token, metadata)
        self.assertNotIn("World one", metadata)
        restarted = HostedWorlds(self.root / "host")
        self.assertIn("Old Square", restarted.describe("one", capability=token))
        self.assertFalse((self.root / "host" / "one" / "world.sqlite3").exists())

    def test_path_traversal_and_unfinished_or_corrupted_registration_fail_closed(self):
        for bad in ("../outside", "two/../one", "a\\b", "a.b", "UPPER", "", "."):
            with self.subTest(bad=bad), self.assertRaises(WorldAccessError):
                self.create(bad)
        abandoned = self.root / "host" / "abandoned"
        abandoned.mkdir()
        (abandoned / "worldpack.json").write_text("{}")
        with self.assertRaises(WorldAccessError):
            self.host.describe("abandoned", capability="anything")
        token = self.create("one")
        metadata = self.root / "host" / "one" / "owner.json"
        parsed = json.loads(metadata.read_text())
        parsed["world_id"] = "two"
        metadata.write_text(json.dumps(parsed))
        with self.assertRaises(WorldAccessError):
            self.host.describe("one", capability=token)

    def test_symlink_pack_escape_is_rejected(self):
        token = self.create()
        pack = self.root / "host" / "one" / "worldpack.json"
        outside = self.root / "outside-worldpack.json"
        outside.write_bytes(pack.read_bytes())
        pack.unlink()
        try:
            pack.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable on this host")
        with self.assertRaises(WorldAccessError):
            self.host.add_room("one", capability=token, room_id="escape",
                               name="No", description="No")
        self.assertEqual("one", read_worldpack(outside)[0]["world_id"])
        self.assertEqual(1, len(read_worldpack(outside)[0]["rooms"]))

    def test_same_named_worlds_on_separate_hosts_have_separate_databases(self):
        other = HostedWorlds(self.root / "other-host")
        a, b = self.create("shared"), self.create("shared", host=other)
        self.assertNotEqual(a, b)
        self.host.add_item("shared", capability=a, item_id="token",
                           name="Brass token", description="Independent.", room_id="square")
        other.add_item("shared", capability=b, item_id="token",
                       name="Brass token", description="Independent.", room_id="square")
        self.host.provision_instance("shared", capability=a)
        other.provision_instance("shared", capability=b)
        e1 = Engine(self.host.instance_path("shared", capability=a))
        e2 = Engine(other.instance_path("shared", capability=b))
        e1.join("SamePlayer")
        self.assertIn("Welcome, SamePlayer", e2.join("SamePlayer"))
        self.assertIn("You take", e1.act("SamePlayer", "take token"))
        self.assertIn("You take", e2.act("SamePlayer", "take token"))


if __name__ == "__main__":
    unittest.main()
