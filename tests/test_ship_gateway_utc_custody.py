"""Hostile installed-SQLite-trigger checks for the authenticated ship writer.

The triggers require privileged host/database access; no remote exploit is
claimed. All HTTP calls use a disposable loopback server and synthetic world.
"""
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing

import test_ship_owner_events_http as owner_module


class ShipGatewayCustodyTests(unittest.TestCase):
    def setUp(self):
        self.api = owner_module.ShipOwnerEventHTTPTests(
            "test_owner_reads_durable_depart_arrive_cursor_and_restart"
        )
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)

    def _sql(self, script):
        with closing(sqlite3.connect(self.api.db_path)) as db:
            db.executescript(script)

    def _ships(self):
        with closing(sqlite3.connect(self.api.db_path)) as db:
            return (
                db.execute("SELECT * FROM geo_ships ORDER BY id").fetchall(),
                db.execute("SELECT * FROM geo_ship_events ORDER BY id").fetchall(),
                db.execute("SELECT * FROM geo_ship_receipts ORDER BY ship_id,actor,action_id").fetchall(),
                db.execute("SELECT * FROM clock_events ORDER BY event_id").fetchall(),
            )

    def _post(self, action_id="custody_depart"):
        return self.api._request(
            "/api/v1/ship/depart", self.api.captain, method="POST",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": action_id},
        )

    def _deny_retry(self, trigger_name):
        before = self._ships()
        status, body = self._post()
        self.assertNotEqual(200, status, body)
        self.assertEqual(before, self._ships())
        status, owner = self.api._request(
            "/api/v1/ship/events?ship_id=vesta&after=0", self.api.captain,
        )
        self.assertEqual(200, status)
        self.assertFalse(any("synthetic_marker" in e["payload"] for e in owner["events"]))
        self._sql(f"DROP TRIGGER {trigger_name};")
        status, body = self._post()
        self.assertEqual(200, status, body)
        self.assertTrue(body["ok"])
        status, repeat = self._post()
        self.assertEqual(200, status, repeat)
        self.assertEqual(body, repeat)
        with closing(sqlite3.connect(self.api.db_path)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM geo_ship_events WHERE ship_id='vesta'").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM geo_ship_receipts WHERE ship_id='vesta'").fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM clock_events WHERE kind='ship_arrival' "
                "AND payload_json LIKE '%vesta%'",
            ).fetchone()[0])

    def test_forged_extra_depart_event_is_rejected_and_retry_once(self):
        self._sql("""
            CREATE TRIGGER forge_depart AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              INSERT INTO geo_ship_events(occurred_at,ship_id,actor,kind,payload_json)
              VALUES(NEW.occurred_at,NEW.ship_id,'Forged','depart',
                     '{"synthetic_marker":"not_real_secret"}');
            END;
        """)
        self._deny_retry("forge_depart")

    def test_extra_negative_id_event_is_rejected(self):
        self._sql("""
            CREATE TRIGGER forge_low_depart AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              INSERT INTO geo_ship_events(id,occurred_at,ship_id,actor,kind,payload_json)
              VALUES(-11,NEW.occurred_at,NEW.ship_id,'Forged','depart','{}');
            END;
        """)
        self._deny_retry("forge_low_depart")

    def test_new_departure_timestamp_rewrite_is_rejected(self):
        self._sql("""
            CREATE TRIGGER forge_depart_time AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              UPDATE geo_ship_events SET occurred_at=NEW.occurred_at-1 WHERE id=NEW.id;
            END;
        """)
        self._deny_retry("forge_depart_time")

    def test_extra_ungranted_receipt_is_rejected(self):
        self._sql("""
            CREATE TRIGGER forge_depart_receipt AFTER INSERT ON geo_ship_receipts
            WHEN NEW.actor='Captain' BEGIN
              INSERT INTO geo_ship_receipts(ship_id,actor,action_id,command,response_json)
              VALUES('vesta','Forged','extra_receipt','depart:moon','{}');
            END;
        """)
        self._deny_retry("forge_depart_receipt")

    def test_departure_receipt_deleted_after_insert_is_rejected(self):
        self._sql("""
            CREATE TRIGGER delete_depart_receipt AFTER INSERT ON geo_ship_receipts
            WHEN NEW.actor='Captain' BEGIN
              DELETE FROM geo_ship_receipts WHERE ship_id=NEW.ship_id AND actor=NEW.actor;
            END;
        """)
        self._deny_retry("delete_depart_receipt")

    def test_ship_owner_rewrite_is_rejected(self):
        self._sql("""
            CREATE TRIGGER forge_depart_owner AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              UPDATE geo_ships SET owner='Forged' WHERE id='vesta';
            END;
        """)
        self._deny_retry("forge_depart_owner")

    def test_prior_rival_departure_history_cannot_rebind_to_owner(self):
        status, _ = self.api._request(
            "/api/v1/ship/depart", self.api.rival, method="POST",
            payload={"ship_id": "rival", "destination": "moon", "action_id": "rival_depart"},
        )
        self.assertEqual(200, status)
        self._sql("""
            CREATE TRIGGER rebind_rival_depart AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              UPDATE geo_ship_events SET ship_id='vesta',
                  payload_json='{"synthetic_marker":"rival_private"}'
              WHERE ship_id='rival';
            END;
        """)
        self._deny_retry("rebind_rival_depart")

    def test_prior_rival_departure_history_cannot_be_deleted(self):
        status, _ = self.api._request(
            "/api/v1/ship/depart", self.api.rival, method="POST",
            payload={"ship_id": "rival", "destination": "moon", "action_id": "rival_depart"},
        )
        self.assertEqual(200, status)
        self._sql("""
            CREATE TRIGGER delete_rival_depart AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              DELETE FROM geo_ship_events WHERE ship_id='rival';
            END;
        """)
        self._deny_retry("delete_rival_depart")

    def test_unrelated_say_event_on_same_writer_is_permitted(self):
        self._sql("""
            CREATE TRIGGER permitted_say AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              INSERT INTO events(occurred_at,room_id,actor,kind,payload_json)
              VALUES(NEW.occurred_at,'main','Captain','say','{}');
            END;
        """)
        status, body = self._post()
        self.assertEqual(200, status, body)
        with closing(sqlite3.connect(self.api.db_path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say' AND actor='Captain'",
            ).fetchone()[0])
        self.assertEqual(200, self._post()[0])


if __name__ == "__main__":
    unittest.main()
