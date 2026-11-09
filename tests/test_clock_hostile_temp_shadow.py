"""Hostile UTC clock TEMP shadow regressions on one bound world."""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway
from realm.world_clock import WorldClock, ClockSkewError

BASE = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)

class TempShadowClockTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "world.sqlite3"
        Gateway(self.path, world_id="utc_hostile")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.clock.schedule("due", due_at=BASE, kind="test", payload={})
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE host_effects (event_id TEXT PRIMARY KEY)")

    def test_temp_event_table_cannot_steal_durable_completion_receipt(self):
        def tamper(db, event):
            db.execute("CREATE TEMP TABLE clock_events (event_id TEXT PRIMARY KEY, processed_utc TEXT)")
            db.execute("INSERT INTO temp.clock_events VALUES (?,NULL)", (event.event_id,))
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        self.clock.tick(BASE, tamper)
        self.assertIsNotNone(self.clock.lookup("due").processed_at)

    def test_temp_clock_state_cannot_erase_monotonic_boundary(self):
        def tamper(db, event):
            db.execute("CREATE TEMP TABLE clock_state (id INTEGER PRIMARY KEY, last_utc TEXT)")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        self.clock.tick(BASE, tamper)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(BASE.isoformat(timespec="microseconds"),
                             db.execute("SELECT last_utc FROM main.clock_state WHERE id=1").fetchone()[0])
        with self.assertRaises(ClockSkewError):
            self.clock.tick(BASE - timedelta(microseconds=1), lambda db, event: None)

    def test_temp_receipt_trigger_cannot_silently_undo_main_receipt(self):
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER undo_receipt
                AFTER UPDATE OF processed_utc ON main.clock_events
                BEGIN
                    UPDATE clock_events SET processed_utc=NULL
                    WHERE event_id=NEW.event_id;
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "clock.*postcondition|receipt"):
            self.clock.tick(BASE, tamper)
        self.assertIsNone(self.clock.lookup("due").processed_at)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])
            self.assertIsNone(db.execute("SELECT last_utc FROM main.clock_state WHERE id=1").fetchone())
        self.assertEqual(["due"], [e.event_id for e in self.clock.tick(BASE, lambda db, e:
            db.execute("INSERT INTO main.host_effects VALUES (?)", (e.event_id,)))])
        self.assertEqual([], WorldClock(self.path).tick(BASE, lambda db, e: None))

    def test_temp_state_trigger_cannot_revert_last_utc_after_update(self):
        earlier = BASE - timedelta(microseconds=1)
        self.assertEqual([], self.clock.tick(earlier, lambda db, event: None))
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER undo_clock_state
                AFTER UPDATE OF last_utc ON main.clock_state
                BEGIN
                    UPDATE clock_state SET last_utc=OLD.last_utc WHERE id=NEW.id;
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "clock.*postcondition|state"):
            self.clock.tick(BASE, tamper)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(earlier.isoformat(timespec="microseconds"),
                             db.execute("SELECT last_utc FROM main.clock_state WHERE id=1").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])
        self.assertIsNone(self.clock.lookup("due").processed_at)
        self.assertEqual(1, len(WorldClock(self.path).tick(BASE, lambda db, event: None)))

    def test_temp_state_insert_trigger_cannot_corrupt_initial_watermark(self):
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER undo_first_watermark
                AFTER INSERT ON main.clock_state
                BEGIN
                    UPDATE clock_state SET last_utc='2000-01-01T00:00:00+00:00'
                    WHERE id=NEW.id;
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "Clock state postcondition"):
            self.clock.tick(BASE, tamper)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT last_utc FROM main.clock_state WHERE id=1").fetchone())
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])
        self.assertIsNone(self.clock.lookup("due").processed_at)
        self.assertEqual(1, len(WorldClock(self.path).tick(BASE, lambda db, event: None)))

    def test_later_event_trigger_cannot_erase_earlier_batch_receipt(self):
        self.clock.schedule("later", due_at=BASE, kind="test", payload={})
        def tamper(db, event):
            if event.event_id == "later":
                db.execute("""CREATE TEMP TRIGGER undo_first
                    AFTER UPDATE OF processed_utc ON main.clock_events
                    WHEN NEW.event_id='later'
                    BEGIN
                        UPDATE clock_events SET processed_utc=NULL WHERE event_id='due';
                    END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "clock.*postcondition|receipt"):
            self.clock.tick(BASE, tamper)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT count(*) FROM main.clock_events WHERE processed_utc IS NULL").fetchone()[0])
        retry = WorldClock(self.path)
        self.assertEqual(["due", "later"], [e.event_id for e in retry.tick(BASE, lambda db, event:
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,)))])
        self.assertEqual([], WorldClock(self.path).tick(BASE, lambda db, event: None))

    def test_trigger_cannot_premark_unselected_future_event(self):
        self.clock.schedule("future", due_at=BASE + timedelta(seconds=10), kind="test", payload={})
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER premake_future_receipt
                AFTER UPDATE OF processed_utc ON main.clock_events
                BEGIN
                    UPDATE clock_events SET processed_utc=NEW.processed_utc WHERE event_id='future';
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "Clock.*postcondition"):
            self.clock.tick(BASE, tamper)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT count(*) FROM main.clock_events WHERE processed_utc IS NULL").fetchone()[0])
        self.assertEqual(["due"], [e.event_id for e in self.clock.tick(BASE, lambda db, event:
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,)))])
        self.assertEqual(["future"], [e.event_id for e in WorldClock(self.path).tick(
            BASE + timedelta(seconds=10), lambda db, event:
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,)))])
        self.assertEqual([], WorldClock(self.path).tick(BASE + timedelta(seconds=10), lambda db, event: None))

    def test_trigger_cannot_reset_already_processed_receipt(self):
        earlier = BASE - timedelta(microseconds=1)
        self.clock.schedule("earlier", due_at=earlier, kind="test", payload={})
        self.assertEqual(["earlier"], [e.event_id for e in self.clock.tick(earlier, lambda db, event: None)])
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER replay_completed_receipt
                AFTER UPDATE OF processed_utc ON main.clock_events
                BEGIN
                    UPDATE clock_events SET processed_utc=NULL WHERE event_id='earlier';
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "Clock.*postcondition"):
            self.clock.tick(BASE, tamper)
        self.assertIsNone(self.clock.lookup("due").processed_at)
        self.assertEqual(earlier, self.clock.lookup("earlier").processed_at)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])

    def test_trigger_cannot_rewrite_unselected_future_event_metadata(self):
        later = BASE + timedelta(seconds=10)
        self.clock.schedule("future", due_at=later, kind="test", payload={"v": 1})
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER rewrite_future_deadline
                AFTER UPDATE OF processed_utc ON main.clock_events
                BEGIN
                    UPDATE clock_events SET due_utc='2099-01-01T00:00:00.000000+00:00'
                    WHERE event_id='future';
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "Clock.*postcondition"):
            self.clock.tick(BASE, tamper)
        self.assertEqual(later, self.clock.lookup("future").due_at)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])

    def test_trigger_cannot_rewrite_unselected_future_kind_and_payload(self):
        later = BASE + timedelta(seconds=10)
        self.clock.schedule("future", due_at=later, kind="test", payload={"v": 1})
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER rewrite_future_kind
                AFTER UPDATE OF processed_utc ON main.clock_events
                BEGIN
                    UPDATE clock_events SET kind='wrong', payload_json='{"v":2}'
                    WHERE event_id='future';
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "Clock event receipt/metadata postcondition"):
            self.clock.tick(BASE, tamper)
        self.assertEqual("test", self.clock.lookup("future").kind)
        self.assertEqual({"v": 1}, self.clock.lookup("future").payload)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])

    def test_trigger_cannot_delete_unselected_future_event(self):
        later = BASE + timedelta(seconds=10)
        self.clock.schedule("future", due_at=later, kind="test", payload={})
        def tamper(db, event):
            db.execute("""CREATE TEMP TRIGGER delete_future
                AFTER UPDATE OF processed_utc ON main.clock_events
                BEGIN
                    DELETE FROM clock_events WHERE event_id='future';
                END""")
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        with self.assertRaisesRegex(RuntimeError, "Clock event deletion postcondition"):
            self.clock.tick(BASE, tamper)
        self.assertIsNotNone(self.clock.lookup("future"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM main.host_effects").fetchone()[0])

    def test_trusted_handler_can_enqueue_new_pending_event(self):
        later = BASE + timedelta(seconds=10)
        def handler(db, event):
            db.execute("INSERT INTO main.clock_events "
                       "(event_id,due_utc,kind,payload_json) VALUES (?,?,?,?)",
                       ("newfuture", later.isoformat(timespec="microseconds"),
                        "test", "{}"))
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        self.assertEqual(["due"], [e.event_id for e in self.clock.tick(BASE, handler)])
        self.assertIsNone(self.clock.lookup("newfuture").processed_at)
        self.assertEqual(["newfuture"], [e.event_id for e in WorldClock(self.path).tick(
            later, lambda db, event: db.execute(
                "INSERT INTO main.host_effects VALUES (?)", (event.event_id,)))])
        self.assertEqual([], WorldClock(self.path).tick(later, lambda db, event: None))

    def test_shadowed_same_instant_batch_replays_zero_effects_after_restart(self):
        eastern = timezone(timedelta(hours=-5))
        self.clock.schedule("aa", due_at=BASE.astimezone(eastern), kind="test", payload={})
        self.clock.schedule("zz", due_at=BASE, kind="test", payload={})
        seen = []
        def tamper(db, event):
            if not seen:
                db.execute("CREATE TEMP TABLE clock_events (event_id TEXT PRIMARY KEY, processed_utc TEXT)")
                db.execute("CREATE TEMP TABLE clock_state (id INTEGER PRIMARY KEY, last_utc TEXT)")
            seen.append(event.event_id)
            db.execute("INSERT INTO main.host_effects VALUES (?)", (event.event_id,))
        self.clock.tick(BASE, tamper)
        self.assertEqual(["aa", "due", "zz"], seen)
        restarted = WorldClock(self.path)
        replayed = []
        restarted.tick(BASE, lambda db, event: replayed.append(event.event_id))
        self.assertEqual([], replayed)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(3, db.execute("SELECT count(*) FROM host_effects").fetchone()[0])
            self.assertEqual(3, db.execute("SELECT count(*) FROM main.clock_events WHERE processed_utc IS NOT NULL").fetchone()[0])
        with self.assertRaises(ClockSkewError):
            restarted.tick(BASE - timedelta(microseconds=1), lambda db, event: None)

if __name__ == "__main__":
    unittest.main()
