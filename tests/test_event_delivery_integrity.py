"""Event-delivery extension must not silently lose previously issued grants.

All mutations here operate only on disposable SQLite databases, modeling local
damage rather than a remote player attack or native SQLite corruption.
"""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine, WorldIntegrityError
from realm.gateway import Gateway
from realm.worldpack import import_worldpack


class EventDeliveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "w.sqlite3"
        self.gateway = Gateway(self.db_path, world_id="sandbox")
        self.gateway.join("Early")
        self.gateway.join("Late")
        self.gateway.command("Early", "say AUDIENCE_MUST_SURVIVE", "audience-seed-1")
        self.assertTrue(any(
            "AUDIENCE_MUST_SURVIVE" in event["text"]
            for event in self.gateway.feed("Late", 0)["events"]
        ))

    def test_missing_delivery_table_after_play_fails_closed_on_restart(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("DROP TABLE event_deliveries")
        with self.assertRaisesRegex(WorldIntegrityError, "(?i)delivery|integrity"):
            Engine(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='event_deliveries'"
            ).fetchone(), "Failed startup must not fabricate an empty grant ledger")

    def test_missing_delivery_index_after_play_fails_closed_on_restart(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("DROP INDEX idx_event_deliveries_event")
        with self.assertRaisesRegex(WorldIntegrityError, "(?i)delivery|integrity"):
            Engine(self.db_path)

    def test_modified_delivery_table_after_play_fails_closed_on_restart(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("ALTER TABLE event_deliveries ADD COLUMN forged TEXT")
        with self.assertRaisesRegex(WorldIntegrityError, "(?i)delivery|integrity"):
            Engine(self.db_path)

    def test_delivery_schema_marker_is_written_only_when_event_grant_commits(self):
        with sqlite3.connect(self.db_path) as db:
            marker = db.execute(
                "SELECT value FROM world_meta WHERE key='event_delivery_schema_version'"
            ).fetchone()
        self.assertEqual(("realm.event_delivery.v1",), marker)

    def test_pre_marker_existing_grants_gain_marker_without_reissuing_access(self):
        # Simulate a legitimate earlier extension build with grants but no
        # version marker. Startup recognizes intact schema and existing rows.
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM world_meta WHERE key='event_delivery_schema_version'")
        Engine(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            value = db.execute(
                "SELECT value FROM world_meta WHERE key='event_delivery_schema_version'"
            ).fetchone()
        self.assertEqual(("realm.event_delivery.v1",), value)
        self.assertTrue(any(
            "AUDIENCE_MUST_SURVIVE" in event["text"]
            for event in self.gateway.feed("Late", 0)["events"]
        ))

    def test_partial_extension_without_marker_is_not_treated_as_fresh_legacy(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM world_meta WHERE key='event_delivery_schema_version'")
            db.execute("DROP INDEX idx_event_deliveries_event")
        with self.assertRaisesRegex(WorldIntegrityError, "(?i)delivery|integrity"):
            Engine(self.db_path)

    def test_pristine_world_has_no_marker_before_first_grant_or_worldpack_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fresh.sqlite3"
            Engine(path)
            with sqlite3.connect(path) as db:
                self.assertIsNone(db.execute(
                    "SELECT value FROM world_meta WHERE key='event_delivery_schema_version'"
                ).fetchone())
            pack = Path(__file__).resolve().parents[1] / "worlds/flagship/siltglass-reach/worldpack.v1.json"
            imported = import_worldpack(path, pack)
            self.assertFalse(imported["unchanged"])


if __name__ == "__main__":
    unittest.main()
