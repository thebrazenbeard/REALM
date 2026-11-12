"""Source-only scheduler change-budget fast-path and hostile fallback tests.

These are synthetic host-installed SQLite triggers, not remote-player attack
evidence. Timings are deliberately not asserted: the architectural bound is
on full world-row snapshots, independent of the test runner's CPU load.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway
from realm.world_clock import WorldClock, _clock_budget_eligible
import realm.world_clock as clock_module


NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


class ChangeBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "world.sqlite3"
        Gateway(self.path, world_id="budget_test")
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(statement, args)

    def value(self, statement, args=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(statement, args).fetchone()[0]

    def schedule(self, key="audit0", at=NOW):
        self.clock.schedule(key, due_at=at, kind="audit", payload={})

    def test_noop_50_utc_events_scan_full_world_twice_not_each_event(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            room = db.execute("SELECT id FROM rooms LIMIT 1").fetchone()[0]
            db.executemany(
                "INSERT INTO items(id,name,description,room_id) VALUES(?,?,?,?)",
                ((f"inert{i}", "Inert", "", room) for i in range(2000)),
            )
        for i in range(50):
            self.schedule(f"audit{i}")
        with patch.object(clock_module, "_nonclock_world_state",
                          wraps=clock_module._nonclock_world_state) as snapshots:
            result = self.clock.tick(NOW, lambda db, event: None, limit=50)
        self.assertEqual(50, len(result))
        self.assertLessEqual(snapshots.call_count, 2)
        self.assertEqual(50, self.value(
            "SELECT count(*) FROM clock_events WHERE processed_utc IS NOT NULL"))
        self.assertEqual([], self.clock.tick(NOW, lambda db, event: None))

    def test_selected_handler_say_and_future_schedule_still_allowed(self):
        self.schedule()

        def trusted(db, event):
            db.execute("INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                       "VALUES(?,?,?,?,?)", (NOW.isoformat(), "Ada", "shore", "say",
                                            '{"message":"Hello"}'))
            db.execute("INSERT INTO clock_events(event_id,due_utc,kind,payload_json) "
                       "VALUES(?,?,?,?)", ("future", (NOW+timedelta(hours=1)).isoformat(
                           timespec="microseconds"), "audit", "{}"))

        self.assertEqual(1, len(self.clock.tick(NOW, trusted)))
        self.assertEqual(1, self.value("SELECT count(*) FROM events WHERE kind='say'"))
        self.assertIsNone(self.clock.lookup("future").processed_at)

    def test_main_after_receipt_trigger_world_mutation_denied_with_retry(self):
        self.schedule()
        self.sql("""CREATE TRIGGER main.spoof AFTER UPDATE OF processed_utc ON clock_events
              BEGIN UPDATE world_meta SET value='poisoned' WHERE key='world_id'; END""")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(_clock_budget_eligible(db))
        with self.assertRaisesRegex(ValueError, "ungranted world effects"):
            self.clock.tick(NOW, lambda db, event: None)
        self.assertEqual("budget_test", self.value(
            "SELECT value FROM world_meta WHERE key='world_id'"))
        self.assertIsNone(self.clock.lookup("audit0").processed_at)
        self.sql("DROP TRIGGER main.spoof")
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))
        self.assertEqual([], self.clock.tick(NOW, lambda db, event: None))

    def test_before_trigger_raise_ignore_is_not_a_successful_receipt(self):
        self.schedule()
        self.sql("""CREATE TRIGGER main.before_spoof BEFORE UPDATE ON clock_events
              BEGIN UPDATE world_meta SET value='poisoned' WHERE key='world_id';
              SELECT RAISE(IGNORE); END""")
        with self.assertRaisesRegex(RuntimeError, "clock receipt changed"):
            self.clock.tick(NOW, lambda db, event: None)
        self.assertEqual("budget_test", self.value(
            "SELECT value FROM world_meta WHERE key='world_id'"))
        self.assertIsNone(self.clock.lookup("audit0").processed_at)
        self.sql("DROP TRIGGER main.before_spoof")
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_receipt_trigger_replace_world_meta_cannot_hide_in_change_budget(self):
        self.schedule()
        self.sql("""CREATE TRIGGER main.replace_world AFTER UPDATE ON clock_events
            BEGIN INSERT OR REPLACE INTO world_meta(key,value)
                 VALUES('world_id','impostor'); END""")
        with self.assertRaisesRegex(ValueError, "ungranted world effects"):
            self.clock.tick(NOW, lambda db, event: None)
        self.assertEqual("budget_test", self.value(
            "SELECT value FROM world_meta WHERE key='world_id'"))
        self.assertIsNone(self.clock.lookup("audit0").processed_at)
        self.sql("DROP TRIGGER main.replace_world")
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_receipt_trigger_deletes_its_own_processed_clock_event(self):
        self.schedule()
        self.sql("""CREATE TRIGGER main.erase_after_ack AFTER UPDATE ON clock_events
            BEGIN DELETE FROM clock_events WHERE event_id=NEW.event_id; END""")
        with self.assertRaisesRegex(RuntimeError, "deletion postcondition"):
            self.clock.tick(NOW, lambda db, event: None)
        self.assertIsNone(self.clock.lookup("audit0").processed_at)
        self.sql("DROP TRIGGER main.erase_after_ack")
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_receipt_trigger_forces_maxint_speech_and_allocator_rollback(self):
        self.schedule()
        self.sql("""CREATE TRIGGER main.maxint_say AFTER UPDATE ON clock_events
            BEGIN INSERT INTO events(id,occurred_at,actor,room_id,kind,payload_json)
                 VALUES(9223372036854775807,'2026-11-09T12:00:00+00:00',
                        'Ghost','missing','say','{}'); END""")
        with self.assertRaisesRegex(ValueError, "ungranted event"):
            self.clock.tick(NOW, lambda db, event: None)
        self.assertEqual(0, self.value("SELECT count(*) FROM events"))
        self.sql("DROP TRIGGER main.maxint_say")
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))
        self.sql("""INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
                   VALUES('2026-11-09T12:00:00+00:00','Ada','shore','say','{}')""")
        self.assertLess(self.value("SELECT max(id) FROM events"), 100)

    def test_trigger_installed_by_handler_switches_to_full_guard(self):
        self.schedule()

        def install(db, event):
            # SQLite trigger-body UPDATE targets must be unqualified.
            db.execute("""CREATE TEMP TRIGGER late AFTER UPDATE ON main.clock_events
                BEGIN UPDATE world_meta SET value='poisoned' WHERE key='world_id';
                END""")

        with self.assertRaisesRegex(ValueError, "ungranted world effects"):
            self.clock.tick(NOW, install)
        self.assertEqual("budget_test", self.value(
            "SELECT value FROM world_meta WHERE key='world_id'"))
        self.assertIsNone(self.clock.lookup("audit0").processed_at)
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_clock_state_trigger_after_watermark_denied_and_retry(self):
        self.schedule()
        self.sql("""CREATE TRIGGER main.watermark_spoof AFTER INSERT ON clock_state
              BEGIN INSERT INTO events(occurred_at,actor,room_id,kind,payload_json)
              VALUES('2026-11-09T12:00:00+00:00','Ghost','missing','say','{}'); END""")
        with self.assertRaisesRegex(ValueError, "ungranted event"):
            self.clock.tick(NOW, lambda db, event: None)
        self.assertEqual(0, self.value("SELECT count(*) FROM events"))
        self.assertIsNone(self.clock.lookup("audit0").processed_at)
        self.sql("DROP TRIGGER main.watermark_spoof")
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_before_watermark_trigger_raise_ignore_with_side_effect_rejected(self):
        self.assertEqual([], self.clock.tick(NOW, lambda db, event: None))
        self.schedule("later", NOW + timedelta(seconds=1))
        self.sql("""CREATE TRIGGER main.block_watermark BEFORE UPDATE ON clock_state
            BEGIN UPDATE world_meta SET value='impostor' WHERE key='world_id';
                  SELECT RAISE(IGNORE); END""")
        with self.assertRaisesRegex(ValueError, "watermark introduced ungranted world"):
            self.clock.tick(NOW + timedelta(seconds=1), lambda db, event: None)
        self.assertEqual("budget_test", self.value(
            "SELECT value FROM world_meta WHERE key='world_id'"))
        self.assertIsNone(self.clock.lookup("later").processed_at)
        self.sql("DROP TRIGGER main.block_watermark")
        self.assertEqual(1, len(self.clock.tick(
            NOW + timedelta(seconds=1), lambda db, event: None)))

    def test_before_watermark_raise_ignore_without_side_effect_is_not_an_ack(self):
        self.assertEqual([], self.clock.tick(NOW, lambda db, event: None))
        self.schedule("later", NOW + timedelta(seconds=1))
        self.sql("""CREATE TRIGGER main.block_watermark BEFORE UPDATE ON clock_state
            BEGIN SELECT RAISE(IGNORE); END""")
        with self.assertRaisesRegex(RuntimeError, "UTC watermark changed"):
            self.clock.tick(NOW + timedelta(seconds=1), lambda db, event: None)
        self.assertIsNone(self.clock.lookup("later").processed_at)
        self.sql("DROP TRIGGER main.block_watermark")
        self.assertEqual(1, len(self.clock.tick(
            NOW + timedelta(seconds=1), lambda db, event: None)))

    def test_handler_schema_ddl_without_dml_counter_is_not_ignored(self):
        self.schedule()

        def change_schema(db, event):
            db.execute("CREATE TABLE illicit_world_table (id INTEGER)")

        with self.assertRaisesRegex(ValueError, "Host UTC handler changed main-world schema"):
            self.clock.tick(NOW, change_schema)
        self.assertEqual(0, self.value(
            "SELECT count(*) FROM sqlite_schema WHERE name='illicit_world_table'"))
        self.assertIsNone(self.clock.lookup("audit0").processed_at)
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_unknown_clock_shape_and_virtual_table_disable_shortcut(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.assertTrue(_clock_budget_eligible(db))
        self.sql("ALTER TABLE clock_events ADD COLUMN extra TEXT")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(_clock_budget_eligible(db))
        self.schedule()
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_virtual_table_disables_shortcut_without_claiming_hook_coverage(self):
        try:
            self.sql("CREATE VIRTUAL TABLE host_search USING fts5(content)")
        except sqlite3.OperationalError:
            self.skipTest("Host SQLite is not built with FTS5")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(_clock_budget_eligible(db))
        self.schedule()
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_unknown_clock_index_disables_shortcut(self):
        self.sql("CREATE INDEX extra_clock_index ON clock_events(processed_utc)")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(_clock_budget_eligible(db))
        self.schedule()
        self.assertEqual(1, len(self.clock.tick(NOW, lambda db, event: None)))

    def test_custom_fk_on_updated_watermark_column_disables_shortcut(self):
        self.assertEqual([], self.clock.tick(NOW, lambda db, event: None))
        self.sql("CREATE UNIQUE INDEX uq_clock_stamp ON clock_state(last_utc)")
        self.sql("""CREATE TABLE observer (
            stamp TEXT REFERENCES clock_state(last_utc) ON UPDATE CASCADE)""")
        self.sql("INSERT INTO observer(stamp) SELECT last_utc FROM clock_state")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(_clock_budget_eligible(db))
        self.schedule("next", NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "UTC watermark introduced ungranted world"):
            self.clock.tick(NOW + timedelta(seconds=1), lambda db, event: None)
        self.assertIsNone(self.clock.lookup("next").processed_at)


if __name__ == "__main__":
    unittest.main()
