"""Owner-only ship history over REALM's already-scoped local HTTP credentials."""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from realm.event_stream import FeedWaitServer
from realm.gateway import Gateway
from realm.world_clock import WorldClock


class ShipOwnerEventHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="realm_ship_owner_events_")
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "world.sqlite3"
        self.gateway = Gateway(self.db_path, world_id="owner_ship_test", scoped_credentials=True)
        for actor in ("Captain", "Navigator", "Rival"):
            self.gateway.join(actor)
        geography = self.gateway.geography
        geography.add_place("universe", "Synthetic Universe", "universe")
        geography.add_place("system", "Synthetic System", "system", "universe")
        geography.add_place("earth", "Synthetic Earth", "planet", "system")
        geography.add_place("moon", "Synthetic Moon", "planet", "system")
        geography.link("earth", "moon", "ship", seconds=30)
        geography.register_ship("vesta", "Vesta", "Captain", "earth")
        geography.register_ship("rival", "Rival", "Rival", "earth")
        self.clock = WorldClock(self.db_path)
        self.clock.enable()
        credentials = self.gateway.scoped_credentials
        self.captain = credentials.provision("Captain", capabilities=("ship:depart",))
        self.navigator = credentials.provision("Navigator", capabilities=("feed:read",))
        self.rival = credentials.provision("Rival", capabilities=("ship:depart",))
        self.servers = []
        self._start(self.gateway)
        self.addCleanup(self._stop_all)

    def _start(self, gateway):
        server = FeedWaitServer(gateway, (), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        self.server = server

    def _stop_all(self):
        for server, thread in reversed(self.servers):
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def _request(self, path, token=None, *, method="GET", payload=None, origin=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin is not None:
            headers["Origin"] = origin
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def _depart_and_arrive(self):
        status, depart = self._request(
            "/api/v1/ship/depart", self.captain, method="POST",
            payload={"ship_id": "vesta", "destination": "moon",
                     "action_id": "ship_owner_observe_depart"},
        )
        self.assertEqual(200, status, depart)
        with closing(sqlite3.connect(self.db_path)) as db:
            pending = db.execute(
                "SELECT event_id,due_utc FROM clock_events "
                "WHERE kind='ship_arrival' AND processed_utc IS NULL"
            ).fetchone()
        self.assertIsNotNone(pending)
        due_at = datetime.fromisoformat(pending[1])

        def arrive(db, event):
            self.gateway.geography.arrive_in_transaction(
                db, event.payload["ship_id"],
                now=int(event.due_at.timestamp()), action_id=event.event_id,
            )

        self.assertEqual([pending[0]], [
            event.event_id for event in self.clock.tick(due_at, arrive)
        ])
        self.assertEqual([], self.clock.tick(due_at, arrive))

    def test_owner_reads_durable_depart_arrive_cursor_and_restart(self):
        self._depart_and_arrive()
        path = "/api/v1/ship/events?ship_id=vesta&after=0"
        status, result = self._request(path, self.captain)
        self.assertEqual(200, status, result)
        self.assertEqual("owner_ship_test", result["world_id"])
        self.assertEqual("vesta", result["ship_id"])
        self.assertEqual(["depart", "arrive"], [e["kind"] for e in result["events"]])
        self.assertEqual(["Captain", "system"], [e["actor"] for e in result["events"]])
        ids = [e["id"] for e in result["events"]]
        self.assertEqual(sorted(set(ids)), ids)
        self.assertEqual(ids[-1], result["cursor"])
        self.assertTrue(all(isinstance(e["at"], int) for e in result["events"]))
        self.assertEqual(result, self._request(path, self.captain)[1])
        status, next_result = self._request(
            f"/api/v1/ship/events?ship_id=vesta&after={ids[0]}", self.captain
        )
        self.assertEqual(200, status)
        self.assertEqual([ids[-1]], [e["id"] for e in next_result["events"]])
        status, empty = self._request(
            f"/api/v1/ship/events?ship_id=vesta&after={ids[-1]}", self.captain
        )
        self.assertEqual(200, status)
        self.assertEqual([], empty["events"])
        self.assertEqual(ids[-1], empty["cursor"])
        # Close the first listener; credentials, owner and history survive host restart.
        self.server.shutdown()
        self.server.server_close()
        self.servers[-1][1].join(timeout=5)
        reopened = Gateway(self.db_path, world_id="owner_ship_test", scoped_credentials=True)
        self._start(reopened)
        self.assertEqual(result, self._request(path, self.captain)[1])

    def test_navigator_and_unscoped_bearers_cannot_observe(self):
        self._depart_and_arrive()
        path = "/api/v1/ship/events?ship_id=vesta"
        for token in (None, "garbage", self.navigator):
            with self.subTest(token_kind=bool(token)):
                self.assertEqual(401, self._request(path, token)[0])

    def test_owner_is_ship_specific_and_unknown_ship_not_enumerated(self):
        path = "/api/v1/ship/events?ship_id=vesta"
        denied = self._request(path, self.rival)
        unknown = self._request(
            "/api/v1/ship/events?ship_id=unknown", self.rival
        )
        self.assertEqual(403, denied[0])
        self.assertEqual(denied, unknown)
        self.assertEqual(403, self._request(
            "/api/v1/ship/events?ship_id=rival", self.captain
        )[0])
        status, own = self._request(
            "/api/v1/ship/events?ship_id=rival", self.rival
        )
        self.assertEqual(200, status, own)
        self.assertEqual([], own["events"])

    def test_owner_transfer_changes_read_entitlement_without_crew_grant(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE main.geo_ships SET owner='Rival' WHERE id='vesta'")
        path = "/api/v1/ship/events?ship_id=vesta"
        self.assertEqual(403, self._request(path, self.captain)[0])
        self.assertEqual(200, self._request(path, self.rival)[0])

    def test_foreign_world_token_is_not_authority(self):
        other = Gateway(
            Path(self.temp.name) / "foreign.sqlite3",
            world_id="foreign_ship_test", scoped_credentials=True,
        )
        token = other.scoped_credentials.provision("Captain", capabilities=("ship:depart",))
        self.assertEqual(401, self._request(
            "/api/v1/ship/events?ship_id=vesta", token
        )[0])

    def test_legacy_unscoped_player_bearer_does_not_grant_ship_observation(self):
        legacy = Gateway(self.db_path, world_id="owner_ship_test", scoped_credentials=False)
        token = legacy.credentials.provision("Captain")
        self._start(legacy)
        self.assertEqual(401, self._request(
            "/api/v1/ship/events?ship_id=vesta", token
        )[0])

    def test_ship_history_is_bounded_and_never_includes_other_ship(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            for index in range(105):
                db.execute(
                    "INSERT INTO main.geo_ship_events(occurred_at,ship_id,actor,kind,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    (index, "vesta", "Captain", "depart", json.dumps({"sequence": index})),
                )
                if index % 7 == 0:
                    db.execute(
                        "INSERT INTO main.geo_ship_events(occurred_at,ship_id,actor,kind,payload_json) "
                        "VALUES(?,?,?,?,?)",
                        (index, "rival", "Rival", "depart", "{}"),
                    )
        first_status, first = self._request(
            "/api/v1/ship/events?ship_id=vesta&after=0", self.captain
        )
        self.assertEqual(200, first_status)
        self.assertEqual(100, len(first["events"]))
        self.assertTrue(all(e["ship_id"] == "vesta" for e in first["events"]))
        self.assertEqual(sorted(e["id"] for e in first["events"]),
                         [e["id"] for e in first["events"]])
        second_status, second = self._request(
            f"/api/v1/ship/events?ship_id=vesta&after={first['cursor']}", self.captain
        )
        self.assertEqual(200, second_status)
        self.assertEqual(5, len(second["events"]))
        self.assertTrue(all(e["ship_id"] == "vesta" for e in second["events"]))
        self.assertEqual(105, len({e["id"] for e in first["events"] + second["events"]}))

    def test_revoke_rejects_existing_history_and_rotation(self):
        self._depart_and_arrive()
        path = "/api/v1/ship/events?ship_id=vesta"
        self.assertEqual(200, self._request(path, self.captain)[0])
        rotated = self.gateway.scoped_credentials.rotate(self.captain)
        self.assertEqual(401, self._request(path, self.captain)[0])
        self.assertEqual(200, self._request(path, rotated)[0])
        self.assertTrue(self.gateway.scoped_credentials.revoke("Captain"))
        self.assertEqual(401, self._request(path, rotated)[0])

    def test_expired_scoped_pilot_token_returns_401(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=20)
        with patch.object(self.gateway.scoped_credentials, "_clock", lambda: future):
            self.assertEqual(401, self._request(
                "/api/v1/ship/events?ship_id=vesta", self.captain
            )[0])

    def test_tampered_world_binding_fails_closed(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE main.world_meta SET value='foreign_ship_test' WHERE key='world_id'"
            )
        self.assertEqual(401, self._request(
            "/api/v1/ship/events?ship_id=vesta", self.captain
        )[0])

    def test_strict_query_cursor_and_origin(self):
        invalid = (
            "/api/v1/ship/events",
            "/api/v1/ship/events?ship_id=",
            "/api/v1/ship/events?ship_id=vesta&ship_id=rival",
            "/api/v1/ship/events?ship_id=vesta&after=",
            "/api/v1/ship/events?ship_id=vesta&after=-1",
            "/api/v1/ship/events?ship_id=vesta&after=2.0",
            "/api/v1/ship/events?ship_id=vesta&after=9223372036854775808",
            "/api/v1/ship/events?ship_id=vesta&after=10",
            "/api/v1/ship/events?ship_id=vesta&after=0&after=1",
            "/api/v1/ship/events?ship_id=vesta&world_id=foreign_ship_test",
            "/api/v1/ship/events?ship_id=vesta&player=Navigator",
            "/api/v1/ship/events?ship_id=%00",
        )
        for path in invalid:
            with self.subTest(path=path):
                status, _ = self._request(path, self.captain)
                self.assertEqual(400, status)
        self.assertEqual(403, self._request(
            "/api/v1/ship/events?ship_id=vesta", self.captain,
            origin="https://untrusted.invalid",
        )[0])

    def test_inflight_revoke_before_response_fails_closed(self):
        original = self.gateway.ship_events_remote

        def revoke_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            self.gateway.scoped_credentials.revoke("Captain")
            return result

        with patch.object(self.gateway, "ship_events_remote", revoke_after_read):
            status, _ = self._request(
                "/api/v1/ship/events?ship_id=vesta", self.captain
            )
        self.assertEqual(401, status)


if __name__ == "__main__":
    unittest.main()
