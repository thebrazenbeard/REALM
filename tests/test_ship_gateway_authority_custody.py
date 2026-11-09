"""Hostile installed-trigger checks for Gateway ship departure authority rows.

No remote exploit: all triggers are privileged host-side SQLite setup and
every request is made to a disposable loopback world.
"""
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

import test_ship_owner_events_http as owner_module


class ShipGatewayAuthorityCustodyTests(unittest.TestCase):
    def setUp(self):
        self.api = owner_module.ShipOwnerEventHTTPTests(
            "test_owner_reads_durable_depart_arrive_cursor_and_restart"
        )
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)

    def _sql(self, script):
        with closing(sqlite3.connect(self.api.db_path)) as db:
            db.executescript(script)

    def _state(self):
        tables = (
            "world_meta", "clock_events", "clock_state",
            "npc_roster_utc_attestations", "scoped_auth_guard",
            "scoped_player_credentials", "player_credentials", "players",
            "geo_ships", "geo_ship_events", "geo_ship_receipts",
        )
        with closing(sqlite3.connect(self.api.db_path)) as db:
            return {
                table: db.execute(f"SELECT * FROM {table}").fetchall()
                for table in tables
            }

    def _post(self, action_id="authority_depart"):
        return self.api._request(
            "/api/v1/ship/depart", self.api.captain, method="POST",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": action_id},
        )

    def _deny_retry(self, trigger_name):
        before = self._state()
        status, result = self._post()
        self.assertNotEqual(200, status, result)
        self.assertEqual(before, self._state())
        self._sql(f"DROP TRIGGER {trigger_name};")
        status, result = self._post()
        self.assertEqual(200, status, result)
        self.assertTrue(result["ok"])
        status, repeated = self._post()
        self.assertEqual(200, status, repeated)
        self.assertEqual(result, repeated)
        with closing(sqlite3.connect(self.api.db_path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM clock_events WHERE kind='ship_arrival' "
                "AND payload_json LIKE '%vesta%'",
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM geo_ship_events WHERE kind='depart' AND ship_id='vesta'",
            ).fetchone()[0])

    def test_world_identity_rebind_trigger_must_rollback(self):
        self._sql("""
            CREATE TRIGGER rewrite_world_identity AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              UPDATE world_meta SET value='forged_world' WHERE key='world_id';
            END;
        """)
        self._deny_retry("rewrite_world_identity")

    def test_extra_ungoverned_ship_arrival_queue_must_rollback(self):
        self._sql("""
            CREATE TRIGGER forge_extra_clock_event AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              INSERT INTO clock_events(event_id,due_utc,kind,payload_json)
              VALUES('forged_extra_arrival','2026-11-09T13:00:00.000000+00:00',
                     'ship_arrival','{"ship_id":"vesta"}');
            END;
        """)
        self._deny_retry("forge_extra_clock_event")

    def test_privilege_expansion_in_scoped_credentials_must_rollback(self):
        self._sql("""
            CREATE TRIGGER forge_navigator_capability AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              UPDATE scoped_player_credentials
              SET capabilities_json='["ship:depart","feed:read"]'
              WHERE player_name='Navigator';
            END;
        """)
        self._deny_retry("forge_navigator_capability")

    def test_clock_watermark_mutation_must_rollback(self):
        self.api.clock.tick(datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc),
                            lambda db, event: None)
        self._sql("""
            CREATE TRIGGER forge_clock_watermark AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              UPDATE clock_state SET last_utc='2020-01-01T00:00:00.000000+00:00';
            END;
        """)
        self._deny_retry("forge_clock_watermark")

    def test_preexisting_unrelated_clock_event_delete_must_rollback(self):
        self.api.clock.schedule(
            "pending_audit", due_at=datetime.now(timezone.utc) + timedelta(hours=1),
            kind="audit", payload={},
        )
        self._sql("""
            CREATE TRIGGER delete_prior_clock_row AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              DELETE FROM clock_events WHERE event_id='pending_audit';
            END;
        """)
        self._deny_retry("delete_prior_clock_row")

    def test_existing_credential_revoked_by_trigger_must_rollback(self):
        self._sql("""
            CREATE TRIGGER revoke_navigator AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              UPDATE scoped_player_credentials SET revoked_us=1234
              WHERE player_name='Navigator';
            END;
        """)
        self._deny_retry("revoke_navigator")

    def test_legitimate_room_say_and_one_arrival_schedule_remain_allowed(self):
        self._sql("""
            CREATE TRIGGER permitted_say_on_depart AFTER INSERT ON geo_ship_events
            WHEN NEW.kind='depart' AND NEW.ship_id='vesta' BEGIN
              INSERT INTO events(occurred_at,room_id,actor,kind,payload_json)
              VALUES(NEW.occurred_at,'main','Captain','say','{}');
            END;
        """)
        self.assertEqual(200, self._post()[0])
        self.assertEqual(200, self._post()[0])
        with closing(sqlite3.connect(self.api.db_path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say' AND actor='Captain'",
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM clock_events WHERE kind='ship_arrival'",
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
