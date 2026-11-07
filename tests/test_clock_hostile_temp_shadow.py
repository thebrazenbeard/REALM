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
