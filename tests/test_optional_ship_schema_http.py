"""Scoped ship HTTP routes must not install optional ship schema on a read/deny."""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from realm.gateway import Gateway, GatewayServer
from realm.world_clock import WorldClock
from realm.worldpack import import_worldpack


FANTASY_PACK = Path(__file__).resolve().parents[1] / "examples" / "cedarwatch-world.json"


class OptionalShipSchemaHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "cedarwatch.sqlite3"
        import_worldpack(self.path, FANTASY_PACK)
        self.gateway = Gateway(self.path, world_id="cedarwatch-example", scoped_credentials=True)
        self.gateway.join("Captain")
        self.token = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",)
        )
        self.server = GatewayServer(self.gateway, (), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def _tables(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {row[0] for row in db.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table'"
            )}

    def _schema_sql(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT type,name,sql FROM main.sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()

    def _configure_ship(self):
        geo = self.gateway.geography
        geo.add_place("universe", "Synthetic Universe", "universe")
        geo.add_place("system", "Synthetic System", "system", "universe")
        geo.add_place("earth", "Synthetic Earth", "planet", "system")
        geo.add_place("moon", "Synthetic Moon", "planet", "system")
        geo.link("earth", "moon", "ship", seconds=30)
        geo.register_ship("vesta", "Vesta", "Captain", "earth")
        WorldClock(self.path).enable()

    def _request(self, method, url, *, token=None, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Authorization": "Bearer " + (self.token if token is None else token)}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, url, body=body, headers=headers)
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def test_unknown_ship_get_denies_without_creating_ship_schema(self):
        before = self._tables()
        self.assertFalse(any(name.startswith("geo_") for name in before))
        status, payload = self._request("GET", "/api/v1/ship/events?ship_id=missing_ship")
        self.assertEqual(403, status, payload)
        self.assertEqual("Ship is not owned by this pilot", payload["error"])
        self.assertEqual(before, self._tables())

    def test_concurrent_unknown_ship_reads_denied_without_schema_writes(self):
        before = self._tables()
        with ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(
                lambda _: self._request("GET", "/api/v1/ship/events?ship_id=missing_ship"),
                range(30),
            ))
        self.assertEqual([403] * 30, [status for status, _ in responses])
        self.assertEqual(before, self._tables())

    def test_ship_post_without_opt_in_clock_or_geography_preserves_schema(self):
        before = self._tables()
        status, payload = self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "missing_ship", "destination": "moon", "action_id": "ship_no_clock"},
        )
        self.assertEqual(409, status, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(before, self._tables())

    def test_ship_post_with_clock_only_does_not_install_geography(self):
        WorldClock(self.path).enable()
        before = self._tables()
        status, payload = self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "missing_ship", "destination": "moon", "action_id": "clock_only"},
        )
        self.assertEqual(409, status, payload)
        self.assertEqual(before, self._tables())
        self.assertFalse(any(name.startswith("geo_") for name in self._tables()))

    def test_ship_post_with_geography_only_does_not_enable_clock(self):
        self.gateway.geography
        before = self._tables()
        status, payload = self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "missing_ship", "destination": "moon", "action_id": "geo_only"},
        )
        self.assertEqual(409, status, payload)
        self.assertEqual(before, self._tables())
        self.assertNotIn("clock_events", self._tables())

    def test_scoped_bearer_denial_precedes_optional_ship_schema_probing(self):
        before = self._tables()
        self.assertEqual(401, self._request(
            "GET", "/api/v1/ship/events?ship_id=missing_ship", token="bad-bearer"
        )[0])
        self.assertEqual(401, self._request(
            "POST", "/api/v1/ship/depart", token="bad-bearer",
            payload={"ship_id": "missing_ship", "destination": "moon", "action_id": "bad"},
        )[0])
        self.assertEqual(before, self._tables())

    def test_partial_ship_schema_does_not_become_enabled_on_get(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE geo_ships (id TEXT PRIMARY KEY)")
        before = self._tables()
        self.assertEqual(403, self._request(
            "GET", "/api/v1/ship/events?ship_id=missing_ship"
        )[0])
        self.assertEqual(before, self._tables())

    def test_fully_named_ship_table_with_wrong_columns_fails_closed(self):
        self._configure_ship()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE main.geo_ships RENAME COLUMN owner TO old_owner")
        before = self._schema_sql()
        status, result = self._request("GET", "/api/v1/ship/events?ship_id=vesta")
        self.assertEqual(403, status, result)
        status, result = self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": "bad_shape_geo"},
        )
        self.assertEqual(409, status, result)
        self.assertEqual(before, self._schema_sql())

    def test_fully_named_clock_table_with_wrong_columns_fails_closed(self):
        self._configure_ship()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE main.clock_events RENAME COLUMN due_utc TO old_due_utc")
        before = self._schema_sql()
        status, result = self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": "bad_shape_clock"},
        )
        self.assertEqual(409, status, result)
        self.assertEqual(before, self._schema_sql())

    def test_named_geography_index_on_wrong_column_is_not_trusted(self):
        self._configure_ship()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP INDEX main.idx_geo_parent")
            db.execute("CREATE INDEX idx_geo_parent ON geo_places(name)")
        before = self._schema_sql()
        self.assertEqual(403, self._request(
            "GET", "/api/v1/ship/events?ship_id=vesta"
        )[0])
        self.assertEqual(409, self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": "wrong_geo_index"},
        )[0])
        self.assertEqual(before, self._schema_sql())

    def test_named_clock_index_on_wrong_columns_is_not_trusted(self):
        self._configure_ship()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP INDEX main.idx_clock_due")
            db.execute("CREATE INDEX idx_clock_due ON clock_events(event_id)")
        before = self._schema_sql()
        self.assertEqual(409, self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": "wrong_clock_index"},
        )[0])
        self.assertEqual(before, self._schema_sql())

    def test_explicit_ship_host_opt_in_keeps_owner_depart_and_events_functional(self):
        self._configure_ship()
        status, empty = self._request("GET", "/api/v1/ship/events?ship_id=vesta")
        self.assertEqual(200, status, empty)
        self.assertEqual([], empty["events"])
        status, departure = self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": "ship_opt_in"},
        )
        self.assertEqual(200, status, departure)
        self.assertEqual("Captain", departure["principal"])
        status, events = self._request("GET", "/api/v1/ship/events?ship_id=vesta")
        self.assertEqual(200, status, events)
        self.assertEqual(["depart"], [event["kind"] for event in events["events"]])
        self.assertEqual((200, departure), self._request(
            "POST", "/api/v1/ship/depart",
            payload={"ship_id": "vesta", "destination": "moon", "action_id": "ship_opt_in"},
        ))


if __name__ == "__main__":
    unittest.main()
