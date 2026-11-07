"""Red integrity acceptance for an already-bound, externally damaged world.

This test deliberately edits ONLY disposable temporary SQLite fixtures. It
does not model an ordinary SQLite crash or demonstrate a remote attack.
After a bound world's authored rooms disappear through corruption or local
tampering, a routine Engine restart must not silently re-seed generic demo
rooms under the old identity; a saved worldpack SHA cannot certify that
the authored rows are still present.
"""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.worldpack import import_worldpack


PACK = Path(__file__).resolve().parents[1] / "worlds" / "flagship" / "siltglass-reach" / "worldpack.v1.json"


class BoundWorldDamageAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "damaged-world.sqlite3"
        initial = import_worldpack(self.path, PACK)
        self.assertFalse(initial["unchanged"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(24, db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0])
            self.assertEqual("siltglass-reach", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'"
            ).fetchone()[0])

        # Simulate external damage or local tampering, not an API action. Keep
        # the identity/hash authority rows while erasing authored topology.
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DELETE FROM exits")
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM rooms")
        self.assertEqual(0, self._room_count())

    def _room_count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]

    def test_normal_engine_restart_must_fail_closed_not_reseed_demo_rooms(self):
        with self.assertRaisesRegex(Exception, "(?i)bound|integrity|missing|corrupt|damaged"):
            Engine(self.path)
        self.assertEqual(
            0, self._room_count(),
            "A routine restart silently overwrote evidence of a damaged bound world",
        )

    def test_same_hash_import_must_not_report_unchanged_or_repair_without_authority(self):
        with self.assertRaisesRegex(Exception, "(?i)bound|integrity|missing|corrupt|damaged"):
            import_worldpack(self.path, PACK)
        self.assertEqual(
            0, self._room_count(),
            "A same-hash retry changed a damaged live world without explicit recovery",
        )

    def test_damaged_preextension_world_does_not_mutate_schema_before_refusal(self):
        # The privacy outbox is an additive extension absent from older REALM
        # worlds. An already-damaged bound world must still fail closed BEFORE
        # Engine auto-installs that extension (or alters any schema metadata).
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE event_deliveries")
            before = tuple(db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall())
        with self.assertRaisesRegex(Exception, "(?i)bound|integrity|missing|corrupt|damaged"):
            Engine(self.path)
        with sqlite3.connect(self.path) as db:
            after = tuple(db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall())
        self.assertEqual(before, after, "A failed restart changed damaged-world schema")


if __name__ == "__main__":
    unittest.main()
