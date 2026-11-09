"""Cross-UTC-batch atmosphere custody; disposable synthetic SQLite only."""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.atmosphere import AtmosphereModel
from realm.atmosphere_clock import AtmosphereUTCHandler
from realm.atmosphere_ledger import AtmosphereLedger
from realm.gateway import Gateway
from realm.world_clock import WorldClock


NOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)


class CrossBatchCustodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "world.sqlite3"
        Gateway(self.path, world_id="synthetic")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        model = AtmosphereModel(
            compartments={"room": {"volume_l": 1000, "pressure_pa": 100000,
                                   "oxygen_ppm": 210000, "temperature_mk": 293150}},
            hatches={}, occupants={})
        self.ledger = AtmosphereLedger(self.path)
        self.ledger.enable(world_id="synthetic", model=model)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE audit(marker TEXT NOT NULL)")

    def handler(self):
        def audit(db, event):
            if event.kind != "audit":
                raise ValueError("Unexpected event")
            db.execute("INSERT INTO audit(marker) VALUES('ordinary')")
        return AtmosphereUTCHandler(self.ledger, world_id="synthetic", other_handler=audit)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return (
                db.execute("SELECT version FROM atm_state WHERE id=1").fetchone(),
                db.execute("SELECT action_id FROM atm_receipts ORDER BY action_id").fetchall(),
                db.execute("SELECT event_id,processed_utc FROM clock_events ORDER BY event_id").fetchall(),
                db.execute("SELECT marker FROM audit ORDER BY marker").fetchall(),
            )

    def test_later_audit_only_batch_cannot_erase_prior_atmosphere_receipt(self):
        self.clock.schedule("a_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 0})
        self.clock.schedule("z_audit", due_at=NOW, kind="audit", payload={})
        handler = self.handler()
        self.assertEqual(["a_atmosphere"],
            [e.event_id for e in self.clock.tick(NOW, handler, limit=1)])
        before = self.snapshot()
        self.assertEqual(("a_atmosphere",), before[1][0])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER later_erase AFTER INSERT ON audit
                BEGIN DELETE FROM atm_receipts WHERE action_id='a_atmosphere'; END""")
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, handler)
        self.assertEqual(before, self.snapshot())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER later_erase")
        self.assertEqual(["z_audit"], [e.event_id for e in self.clock.tick(NOW, handler)])
        self.assertEqual([("a_atmosphere",)], self.snapshot()[1])
        self.assertEqual([], WorldClock(self.path).tick(NOW, self.handler()))

    def test_idle_clock_receipt_trigger_cannot_corrupt_prior_atmosphere(self):
        self.clock.schedule("a_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 0})
        handler = self.handler()
        self.clock.tick(NOW, handler)
        before = self.snapshot()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER idle_erase AFTER UPDATE ON clock_state
                BEGIN DELETE FROM atm_receipts WHERE action_id='a_atmosphere'; END""")
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, handler)
        self.assertEqual(before, self.snapshot())

    def test_benign_second_batch_and_legacy_uninitialized_world(self):
        self.clock.schedule("a_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 0})
        self.clock.schedule("z_audit", due_at=NOW, kind="audit", payload={})
        handler = self.handler()
        self.clock.tick(NOW, handler, limit=1)
        self.assertEqual(["z_audit"], [e.event_id for e in self.clock.tick(NOW, handler)])
        self.assertEqual((1,), self.snapshot()[0])
        self.assertEqual([("a_atmosphere",)], self.snapshot()[1])
        self.assertEqual([("ordinary",)], self.snapshot()[3])

        legacy = self.path.parent / "legacy.sqlite3"
        Gateway(legacy, world_id="legacy")
        clock = WorldClock(legacy)
        clock.enable()
        clock.schedule("ordinary", due_at=NOW, kind="audit", payload={})
        self.assertEqual(["ordinary"], [e.event_id for e in
            clock.tick(NOW, lambda db, event: None)])
        with closing(sqlite3.connect(legacy)) as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='atm_state'").fetchone())


if __name__ == "__main__":
    unittest.main()
