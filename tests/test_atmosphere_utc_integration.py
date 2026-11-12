"""Optional host UTC → atmosphere transaction tests, all synthetic world data."""
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


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def sample():
    return AtmosphereModel(
        compartments={
            "a": {"volume_l": 1000, "pressure_pa": 100000,
                  "oxygen_ppm": 210000, "temperature_mk": 293150},
            "b": {"volume_l": 1000, "pressure_pa": 0,
                  "oxygen_ppm": 0, "temperature_mk": 293150},
        },
        hatches={"h": {"a": "a", "b": "b", "operators": ["pilot"],
                        "open": True, "operational": True}},
        occupants={})


class AtmosphereUTCIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "synthetic.sqlite3"
        Gateway(self.path, world_id="synthetic")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.ledger = AtmosphereLedger(self.path)
        self.ledger.enable(world_id="synthetic", model=sample())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE audit(marker TEXT NOT NULL)")

    def schedule(self):
        self.clock.schedule("a_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 0})
        self.clock.schedule("z_audit", due_at=NOW, kind="audit", payload={})

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {
                "receipts": db.execute("SELECT action_id FROM atm_receipts ORDER BY action_id").fetchall(),
                "events": db.execute("SELECT event_id,processed_utc FROM clock_events ORDER BY event_id").fetchall(),
                "audit": db.execute("SELECT marker FROM audit").fetchall(),
                "version": db.execute("SELECT version FROM atm_state WHERE id=1").fetchone()[0],
            }

    def handler(self):
        def audit(db, event):
            if event.kind != "audit":
                raise ValueError("Unexpected host event")
            db.execute("INSERT INTO audit(marker) VALUES('benign_event')")
        return AtmosphereUTCHandler(self.ledger, world_id="synthetic", other_handler=audit)

    def test_later_host_callback_cannot_delete_atmosphere_receipt_then_ack(self):
        self.schedule()
        baseline = self.rows()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER hostile_late AFTER INSERT ON audit
                BEGIN DELETE FROM atm_receipts WHERE action_id='a_atmosphere'; END""")

        handler = self.handler()
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, handler)
        self.assertEqual(baseline, self.rows())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER hostile_late")
        self.assertEqual(["a_atmosphere", "z_audit"],
            [event.event_id for event in self.clock.tick(NOW, handler)])
        self.assertEqual(1, self.ledger.status(world_id="synthetic")["version"])
        self.assertEqual([("a_atmosphere",)], self.rows()["receipts"])
        self.assertEqual([], WorldClock(self.path).tick(NOW, self.handler()))

    def test_benign_audit_and_two_atmosphere_ticks_commit_once(self):
        self.clock.schedule("a_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 0})
        self.clock.schedule("b_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 1})
        self.clock.schedule("z_audit", due_at=NOW, kind="audit", payload={})
        processed = self.clock.tick(NOW, self.handler())
        self.assertEqual(["a_atmosphere", "b_atmosphere", "z_audit"],
                         [e.event_id for e in processed])
        self.assertEqual(2, self.ledger.status(world_id="synthetic")["version"])
        self.assertEqual([("a_atmosphere",), ("b_atmosphere",)], self.rows()["receipts"])
        self.assertEqual([("benign_event",)], self.rows()["audit"])
        self.assertEqual([], WorldClock(self.path).tick(NOW, self.handler()))

    def test_unrelated_say_history_remains_permitted(self):
        self.schedule()

        def say_handler(db, event):
            self.assertEqual("audit", event.kind)
            db.execute(
                "INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                "VALUES(?,?,?,?,?)",
                (NOW.isoformat(), "host", "atrium", "say", '{"text":"hello"}'),
            )

        self.clock.tick(NOW, AtmosphereUTCHandler(
            self.ledger, world_id="synthetic", other_handler=say_handler))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([("say",)], db.execute(
                "SELECT kind FROM main.events WHERE kind='say'").fetchall())
        self.assertEqual(1, self.ledger.status(world_id="synthetic")["version"])

    def test_early_audit_tamper_cannot_be_accepted_as_atmosphere_baseline(self):
        self.clock.schedule("a_audit", due_at=NOW, kind="audit", payload={})
        self.clock.schedule("z_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 0})
        baseline = self.rows()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER early_drift AFTER INSERT ON audit
                BEGIN UPDATE atm_state SET version=9 WHERE id=1; END""")
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, self.handler())
        self.assertEqual(baseline, self.rows())

    def test_wrong_world_ledger_and_invalid_payload_roll_back(self):
        other_path = self.path.parent / "other.sqlite3"
        Gateway(other_path, world_id="synthetic")
        other_ledger = AtmosphereLedger(other_path)
        other_ledger.enable(world_id="synthetic", model=sample())
        self.clock.schedule("a_atmosphere", due_at=NOW,
            kind="atmosphere_tick", payload={"expected_version": 0})
        baseline = self.rows()
        with self.assertRaises(RuntimeError):
            self.clock.tick(NOW, AtmosphereUTCHandler(other_ledger, world_id="synthetic"))
        self.assertEqual(baseline, self.rows())
        # A different type of invalid host schedule never silently ACKs.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE clock_events SET payload_json='{}' WHERE event_id='a_atmosphere'")
        with self.assertRaises(ValueError):
            self.clock.tick(NOW, self.handler())
        self.assertEqual(0, self.ledger.status(world_id="synthetic")["version"])

    def test_legacy_world_uses_original_clock_without_atmosphere(self):
        path = self.path.parent / "legacy.sqlite3"
        Gateway(path, world_id="legacy")
        clock = WorldClock(path)
        clock.enable()
        clock.schedule("say", due_at=NOW, kind="audit", payload={})
        self.assertEqual(["say"], [e.event_id for e in clock.tick(NOW, lambda db, event: None)])
        with closing(sqlite3.connect(path)) as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='atm_state'").fetchone())


if __name__ == "__main__":
    unittest.main()
