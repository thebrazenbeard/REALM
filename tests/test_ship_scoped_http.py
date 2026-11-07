"""Scoped ship departure must commit under the same writer as credential auth."""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from unittest.mock import patch

from realm.ship_command_auth import AuthenticatedShipCommands
import unittest
from contextlib import closing
from pathlib import Path

from realm.gateway import Gateway, GatewayServer
from realm.world_clock import WorldClock


class ScopedShipHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "world.sqlite3"
        self.gateway = Gateway(
            self.db_path, world_id="ship_scoped", scoped_credentials=True
        )
        self.gateway.join("Captain")
        self.gateway.join("Intruder")
        geo = self.gateway.geography
        geo.add_place("universe", "Universe", "universe")
        geo.add_place("system", "System", "system", "universe")
        geo.add_place("earth", "Earth", "planet", "system")
        geo.add_place("moon", "Moon", "planet", "system")
        geo.link("earth", "moon", "ship", seconds=30)
        geo.register_ship("vesta", "Vesta", "Captain", "earth")
        self.clock = WorldClock(self.db_path)
        self.clock.enable()
        self.server = GatewayServer(self.gateway, (), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, 5)

    def request(self, token, *, action_id="ship-action", extra=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        payload = {"ship_id": "vesta", "destination": "moon", "action_id": action_id}
        payload.update(extra or {})
        body = json.dumps(payload).encode("utf-8")
        try:
            connection.request(
                "POST", "/api/v1/ship/depart", body,
                {"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
            )
            result = connection.getresponse()
            status = result.status
            payload = json.loads(result.read())
            return status, payload
        finally:
            connection.close()

    def test_capable_owner_departure_is_one_atomic_scheduled_event(self):
        token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        status, payload = self.request(token)
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual("Captain", payload["principal"])
        self.assertEqual("in_transit", self.gateway.geography.ship_status("vesta")["state"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM main.geo_ship_events WHERE kind='depart'"
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM main.clock_events WHERE kind='ship_arrival'"
            ).fetchone()[0])
    def test_command_capability_cannot_depart_ship(self):
        token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("command:write",)
        )
        status, _ = self.request(token)
        self.assertEqual(401, status)
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])

    def test_revoke_after_http_headers_before_writer_denies_ship_effect(self):
        token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        entered, release = threading.Event(), threading.Event()
        original = AuthenticatedShipCommands.depart

        def delay_before_writer(pilot, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Timed out waiting for revoke")
            return original(pilot, *args, **kwargs)

        with patch.object(AuthenticatedShipCommands, "depart", delay_before_writer):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.request, token)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertTrue(self.gateway.scoped_credentials.revoke("Captain"))
                finally:
                    release.set()
                status, payload = pending.result(timeout=10)
        self.assertEqual(401, status, payload)
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM main.geo_ship_events WHERE kind='depart'"
            ).fetchone()[0])

    def test_ship_writer_lock_serializes_departure_before_revoke(self):
        token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        from realm import ship_command_auth
        original = ship_command_auth.depart_and_schedule_in_transaction
        entered, release = threading.Event(), threading.Event()

        def delay_inside_writer(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Timed out holding ship writer transaction")
            return original(*args, **kwargs)

        with patch.object(ship_command_auth, "depart_and_schedule_in_transaction",
                          delay_inside_writer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                departure = pool.submit(self.request, token)
                try:
                    self.assertTrue(entered.wait(5))
                    revoked = pool.submit(self.gateway.scoped_credentials.revoke, "Captain")
                    with self.assertRaises(FutureTimeout):
                        revoked.result(timeout=0.1)
                finally:
                    release.set()
                status, payload = departure.result(timeout=10)
                self.assertTrue(revoked.result(timeout=10))
        self.assertEqual(200, status, payload)
        self.assertEqual("in_transit", self.gateway.geography.ship_status("vesta")["state"])
        self.assertIsNone(self.gateway.scoped_credentials.authenticate(
            token, required_capability="ship:depart"
        ))

    def test_client_supplied_time_and_actor_cannot_control_utc_or_owner(self):
        token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        before = int(time.time())
        status, payload = self.request(token, extra={
            "now": 0, "arrival_at": 0, "actor": "Intruder",
        })
        self.assertEqual(200, status, payload)
        self.assertEqual("Captain", payload["principal"])
        transit = self.gateway.geography.ship_status("vesta")
        self.assertGreaterEqual(transit["arrival_at"], before + 30)
        self.assertLessEqual(transit["arrival_at"], int(time.time()) + 31)
        with closing(sqlite3.connect(self.db_path)) as db:
            actor = db.execute(
                "SELECT actor FROM main.geo_ship_events WHERE kind='depart'"
            ).fetchone()[0]
        self.assertEqual("Captain", actor)

    def test_active_replay_idempotent_but_revoked_token_cannot_read_receipt(self):
        token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        first = self.request(token)
        again = self.request(token)
        self.assertEqual(first, again)
        self.assertEqual(200, first[0])
        self.assertTrue(self.gateway.scoped_credentials.revoke("Captain"))
        rejected = self.request(token)
        self.assertEqual(401, rejected[0])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM main.geo_ship_receipts"
            ).fetchone()[0])

    def test_token_from_other_world_never_moves_ship(self):
        other = Gateway(
            self.db_path.parent / "other.sqlite3",
            world_id="other_ship_world", scoped_credentials=True,
        )
        foreign = other.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        status, payload = self.request(foreign)
        self.assertEqual(401, status, payload)
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])

    def test_disabled_clock_returns_conflict_without_stranding_ship(self):
        token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DROP TABLE main.clock_events")
            db.execute("DROP TABLE main.clock_state")
        status, payload = self.request(token)
        self.assertEqual(409, status, payload)
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])

    def test_temp_scoped_credentials_cannot_forge_owner(self):
        from realm.ship_command_auth import AuthenticatedShipCommands
        forged = "x" * 32
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                "CREATE TEMP TABLE scoped_player_credentials ("
                "world_id TEXT,token_digest TEXT,player_name TEXT,"
                "capabilities_json TEXT,issued_us INTEGER,expires_us INTEGER,"
                "revoked_us INTEGER)"
            )
            now_us = int(time.time() * 1_000_000)
            db.execute(
                "INSERT INTO temp.scoped_player_credentials VALUES (?,?,?,?,?,?,NULL)",
                ("ship_scoped", self.gateway.scoped_credentials._digest(forged),
                 "Captain", '["ship:depart"]', now_us - 1000,
                 now_us + 1_000_000_000),
            )
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            pilot = AuthenticatedShipCommands(self.gateway, self.clock)
            with self.assertRaises(PermissionError):
                pilot.depart_in_transaction(
                    db, forged, "vesta", "moon",
                    now=int(time.time()), action_id="spoof", scoped=True,
                )
            db.rollback()
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])

    def test_nonowner_with_ship_capability_cannot_depart(self):
        token = self.gateway.scoped_credentials.provision(
            "Intruder", capabilities=("ship:depart",)
        )
        status, _ = self.request(token)
        self.assertIn(status, (401, 403))
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])


if __name__ == "__main__":
    unittest.main()
