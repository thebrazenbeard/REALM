"""Hostile SQLite-clock ship history custody checks (not remote exploits)."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway
from realm.geography import Geography
from realm.world_clock import WorldClock

START = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
ARRIVAL = START + timedelta(seconds=50)


class ShipHistoryUtcCustodyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        Gateway(self.path, world_id="ship_history_custody")
        self.geo = Geography(self.path)
        self.geo.add_place("universe", "Universe", "universe")
        self.geo.add_place("system", "System", "system", "universe")
        self.geo.add_place("earth", "Earth", "planet", "system")
        self.geo.add_place("moon", "Moon", "planet", "system")
        self.geo.link("earth", "moon", "ship", seconds=50)
        self.geo.register_ship("voyager", "Voyager", "Captain", "earth")
        self.geo.depart("voyager", "moon", actor="Captain",
                        now=int(START.timestamp()), action_id="depart-1")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.clock.schedule("ship_arrival_voyager", due_at=ARRIVAL,
                            kind="ship_arrival", payload={"ship_id": "voyager"})

    def _arrive(self, db, event):
        if event.kind == "ship_arrival":
            return self.geo.arrive_in_transaction(
                db, event.payload["ship_id"],
                now=int(event.due_at.timestamp()), action_id=event.event_id,
            )

    def _sql(self, statement):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(statement)

    def _rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return (
                db.execute("SELECT * FROM geo_ship_events ORDER BY id").fetchall(),
                db.execute("SELECT * FROM geo_ship_receipts ORDER BY ship_id,actor,action_id").fetchall(),
            )

    def _assert_rejected_and_retry(self, trigger_name):
        before = self._rows()
        with self.assertRaisesRegex((ValueError, RuntimeError), "[Ss]hip"):
            self.clock.tick(ARRIVAL, self._arrive)
        self.assertEqual(before, self._rows())
        self.assertEqual("in_transit", self.geo.ship_status("voyager")["state"])
        self.assertIsNone(self.clock.lookup("ship_arrival_voyager").processed_at)
        self._sql(f"DROP TRIGGER {trigger_name};")
        self.assertEqual(["ship_arrival_voyager"],
                         [e.event_id for e in self.clock.tick(ARRIVAL, self._arrive)])
        self.assertEqual("docked", self.geo.ship_status("voyager")["state"])
        self.assertEqual([], self.clock.tick(ARRIVAL, self._arrive))
        self.assertEqual(2, len(self._rows()[0]))

    def test_late_clock_trigger_forged_arrival_and_raw_marker_rolls_back(self):
        self._sql("""
            CREATE TRIGGER forge_ship_arrival AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='ship_arrival' AND NEW.processed_utc IS NOT NULL
            BEGIN
              INSERT INTO geo_ship_events(occurred_at,ship_id,actor,kind,payload_json)
              VALUES(1794236450,'voyager','Forged','arrive',
                     '{"synthetic_secret_marker":"not_real_secret"}');
            END;
        """)
        self._assert_rejected_and_retry("forge_ship_arrival")

    def test_late_clock_trigger_rewrites_old_departure(self):
        self._sql("""
            CREATE TRIGGER rewrite_ship_depart AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='ship_arrival' AND NEW.processed_utc IS NOT NULL
            BEGIN
              UPDATE geo_ship_events SET payload_json='{"from":"forged"}' WHERE kind='depart';
            END;
        """)
        self._assert_rejected_and_retry("rewrite_ship_depart")

    def test_late_clock_trigger_deletes_old_departure(self):
        self._sql("""
            CREATE TRIGGER delete_ship_depart AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='ship_arrival' AND NEW.processed_utc IS NOT NULL
            BEGIN DELETE FROM geo_ship_events WHERE kind='depart'; END;
        """)
        self._assert_rejected_and_retry("delete_ship_depart")

    def test_late_clock_trigger_forges_system_receipt(self):
        self._sql("""
            CREATE TRIGGER forge_ship_receipt AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='ship_arrival' AND NEW.processed_utc IS NOT NULL
            BEGIN
              INSERT INTO geo_ship_receipts(ship_id,actor,action_id,command,response_json)
              VALUES('voyager','system','forged_arrival','arrive','{}');
            END;
        """)
        self._assert_rejected_and_retry("forge_ship_receipt")

    def test_late_clock_trigger_rewrites_ship_owner(self):
        self._sql("""
            CREATE TRIGGER rewrite_ship_owner AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='ship_arrival' AND NEW.processed_utc IS NOT NULL
            BEGIN UPDATE geo_ships SET owner='Forged' WHERE id='voyager'; END;
        """)
        self._assert_rejected_and_retry("rewrite_ship_owner")

    def test_late_clock_trigger_forges_low_id_or_rival_ship_event(self):
        self.geo.register_ship("rival", "Rival", "Other", "earth")
        self._sql("""
            CREATE TRIGGER forge_rival_event AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='ship_arrival' AND NEW.processed_utc IS NOT NULL
            BEGIN
              INSERT INTO geo_ship_events(id,occurred_at,ship_id,actor,kind,payload_json)
              VALUES(-11,1794236450,'rival','Forged','arrive','{}');
            END;
        """)
        self._assert_rejected_and_retry("forge_rival_event")

    def test_late_clock_trigger_modifies_prior_ship_receipt(self):
        self._sql("""
            CREATE TRIGGER tamper_ship_receipt AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='ship_arrival' AND NEW.processed_utc IS NOT NULL
            BEGIN
              UPDATE geo_ship_receipts SET response_json='{"forged":true}'
              WHERE command LIKE 'depart:%';
            END;
        """)
        self._assert_rejected_and_retry("tamper_ship_receipt")

    def test_followup_audit_tick_must_preserve_prior_arrival_ledger(self):
        self.clock.tick(ARRIVAL, self._arrive)
        self.clock.schedule("later_audit", due_at=ARRIVAL + timedelta(seconds=1),
                            kind="audit", payload={})
        self._sql("""
            CREATE TRIGGER corrupt_prior_arrival AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='audit' AND NEW.processed_utc IS NOT NULL
            BEGIN UPDATE geo_ship_events SET actor='Forged' WHERE kind='arrive'; END;
        """)
        before = self._rows()
        with self.assertRaisesRegex((ValueError, RuntimeError), "[Ss]hip"):
            self.clock.tick(ARRIVAL + timedelta(seconds=1), lambda db, e: None)
        self.assertEqual(before, self._rows())
        self.assertIsNone(self.clock.lookup("later_audit").processed_at)
        self._sql("DROP TRIGGER corrupt_prior_arrival;")
        self.assertEqual(["later_audit"], [e.event_id for e in self.clock.tick(
            ARRIVAL + timedelta(seconds=1), lambda db, e: None)])

    def test_idle_tick_trigger_must_not_mutate_old_ship_history(self):
        self.clock.tick(ARRIVAL, self._arrive)
        before = self._rows()
        self._sql("""
            CREATE TRIGGER corrupt_ship_on_idle AFTER UPDATE OF last_utc ON clock_state
            BEGIN DELETE FROM geo_ship_events WHERE kind='depart'; END;
        """)
        with self.assertRaisesRegex((ValueError, RuntimeError), "[Ss]hip"):
            self.clock.tick(ARRIVAL + timedelta(seconds=1), lambda db, e: None)
        self.assertEqual(before, self._rows())
        self._sql("DROP TRIGGER corrupt_ship_on_idle;")
        self.assertEqual([], self.clock.tick(ARRIVAL + timedelta(seconds=1),
                                             lambda db, e: None))

    def test_later_audit_cannot_rebind_rival_history_into_owner_feed(self):
        self.geo.register_ship("rival", "Rival", "Other", "earth")
        self.geo.link("earth", "moon", "ship", seconds=50)
        self.geo.depart("rival", "moon", actor="Other",
                        now=int(START.timestamp()), action_id="rival-depart")
        self.clock.tick(ARRIVAL, self._arrive)
        self.clock.schedule("later_audit", due_at=ARRIVAL + timedelta(seconds=1),
                            kind="audit", payload={})
        self._sql("""
            CREATE TRIGGER rebind_rival_history AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.kind='audit' AND NEW.processed_utc IS NOT NULL
            BEGIN UPDATE geo_ship_events SET ship_id='voyager',
                  payload_json='{"synthetic_rival_private_marker":true}'
                  WHERE ship_id='rival'; END;
        """)
        before = self._rows()
        with self.assertRaisesRegex(ValueError, "Ship UTC"):
            self.clock.tick(ARRIVAL + timedelta(seconds=1), lambda db, e: None)
        self.assertEqual(before, self._rows())
        self._sql("DROP TRIGGER rebind_rival_history;")
        self.assertEqual(["later_audit"], [e.event_id for e in self.clock.tick(
            ARRIVAL + timedelta(seconds=1), lambda db, e: None)])

    def test_two_scheduled_ship_arrivals_have_exact_two_effects(self):
        self.geo.register_ship("rival", "Rival", "Other", "earth")
        self.geo.depart("rival", "moon", actor="Other",
                        now=int(START.timestamp()), action_id="rival-depart")
        self.clock.schedule("ship_arrival_rival", due_at=ARRIVAL,
                            kind="ship_arrival", payload={"ship_id": "rival"})
        processed = self.clock.tick(ARRIVAL, self._arrive)
        self.assertEqual({"ship_arrival_voyager", "ship_arrival_rival"},
                         {e.event_id for e in processed})
        events, receipts = self._rows()
        self.assertEqual(4, len(events))
        self.assertEqual(4, len(receipts))
        self.assertEqual(2, sum(row[4] == "arrive" for row in events))
        self.assertEqual([], self.clock.tick(ARRIVAL, self._arrive))

    def test_host_departure_must_enqueue_exact_future_arrival(self):
        from realm.ship_clock_dispatch import depart_and_schedule_in_transaction
        self.geo.register_ship("rival", "Rival", "Other", "earth")
        self.clock.schedule("host_depart_rival", due_at=START,
                            kind="ship_departure",
                            payload={"ship_id": "rival", "destination": "moon"})
        def permitted(db, event):
            return depart_and_schedule_in_transaction(
                self.clock, self.geo, db, event.payload["ship_id"],
                event.payload["destination"], actor="Other",
                now=int(event.due_at.timestamp()), action_id=event.event_id,
            )
        self.assertEqual(["host_depart_rival"], [e.event_id for e in
                         self.clock.tick(START, permitted)])
        self.assertEqual("in_transit", self.geo.ship_status("rival")["state"])
        self.assertEqual(1, sum(row[4] == "depart" and row[2] == "rival"
                                for row in self._rows()[0]))
        arrived = self.clock.tick(ARRIVAL, self._arrive)
        self.assertEqual(2, len(arrived))
        self.assertIn("ship_arrival_voyager", {e.event_id for e in arrived})
        self.assertEqual("docked", self.geo.ship_status("rival")["state"])

    def test_ship_arrival_without_authorized_callback_cannot_ack(self):
        before = self._rows()
        with self.assertRaisesRegex(ValueError, "Ship UTC"):
            self.clock.tick(ARRIVAL, lambda db, e: None)
        self.assertEqual(before, self._rows())
        self.assertIsNone(self.clock.lookup("ship_arrival_voyager").processed_at)

    def test_unrelated_audit_event_and_nonship_say_are_allowed(self):
        self.clock.schedule("later_audit", due_at=ARRIVAL,
                            kind="audit", payload={})
        def handler(db, event):
            if event.kind == "audit":
                db.execute("INSERT INTO main.events(occurred_at,room_id,actor,kind,payload_json) "
                           "VALUES(?,?,?,?,?)", (int(ARRIVAL.timestamp()), "main", "Captain", "say", "{}"))
            else:
                self._arrive(db, event)
        # The selected event IDs are deterministic but not necessarily sorted by kind.
        self.assertEqual(2, len(self.clock.tick(ARRIVAL, handler)))
        self.assertEqual(2, len(self._rows()[0]))


if __name__ == "__main__":
    unittest.main()
