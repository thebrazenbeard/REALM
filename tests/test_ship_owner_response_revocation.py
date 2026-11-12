"""Regression for ship-ownership transfer between history read and HTTP send.

This exercises a real loopback listener, not a direct handler invocation.
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from realm.event_stream import FeedWaitServer
from realm.gateway import Gateway


class ShipOwnerResponseRevocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="realm_owner_response_")
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "world.sqlite3"
        self.gateway = Gateway(
            self.db_path, world_id="owner_response_test", scoped_credentials=True,
        )
        for player in ("Captain", "Rival"):
            self.gateway.join(player)
        geo = self.gateway.geography
        geo.add_place("universe", "Synthetic Universe", "universe")
        geo.add_place("system", "Synthetic System", "system", "universe")
        geo.add_place("earth", "Synthetic Earth", "planet", "system")
        geo.register_ship("vesta", "Vesta", "Captain", "earth")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO geo_ship_events(occurred_at,ship_id,actor,kind,payload_json) "
                "VALUES(?,?,?,?,?)",
                (1, "vesta", "Captain", "depart", '{"test_marker":"captain_record"}'),
            )
        self.captain = self.gateway.scoped_credentials.provision(
            "Captain", capabilities=("ship:depart",),
        )
        self.rival = self.gateway.scoped_credentials.provision(
            "Rival", capabilities=("ship:depart",),
        )
        self.server = FeedWaitServer(self.gateway, (), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def _request(self, token):
        with closing(http.client.HTTPConnection(
                "127.0.0.1", self.server.server_port, timeout=5)) as connection:
            connection.request(
                "GET", "/api/v1/ship/events?ship_id=vesta",
                headers={"Authorization": "Bearer " + token},
            )
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body)

    def _transfer_to_rival(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE geo_ships SET owner='Rival' WHERE id='vesta'")

    def test_transfer_after_authorized_read_before_http_send_denies_former_owner(self):
        # Without the response-bound owner proof, the Captain remains fully
        # credentialed and the original handler sends HTTP 200 + old records.
        original = self.gateway.ship_events_remote
        did_transfer = threading.Event()

        def transfer_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertEqual("captain_record", result["events"][0]["payload"]["test_marker"])
            self._transfer_to_rival()
            did_transfer.set()
            return result

        with patch.object(self.gateway, "ship_events_remote", transfer_after_read):
            status, denied = self._request(self.captain)
        self.assertTrue(did_transfer.is_set())
        self.assertEqual(403, status)
        self.assertEqual({"ok": False, "error": "Ship is not owned by this pilot"}, denied)
        # Credential remains active: rejection is ownership, not revocation.
        self.assertEqual("Captain", self.gateway.authenticate_remote(
            self.captain, required_capability="ship:depart"))
        self.assertEqual(403, self._request(self.captain)[0])
        # Durable hull history follows the ship to its new owner by design.
        status, entitled = self._request(self.rival)
        self.assertEqual(200, status)
        self.assertEqual("captain_record", entitled["events"][0]["payload"]["test_marker"])

    def test_still_owner_receives_original_result(self):
        status, result = self._request(self.captain)
        self.assertEqual(200, status)
        self.assertEqual("vesta", result["ship_id"])
        self.assertEqual("captain_record", result["events"][0]["payload"]["test_marker"])

    def test_world_binding_changes_after_read_fail_closed(self):
        original = self.gateway.ship_events_remote

        def tamper_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute(
                    "UPDATE world_meta SET value='foreign_world' WHERE key='world_id'"
                )
            return result

        with patch.object(self.gateway, "ship_events_remote", tamper_after_read):
            status, result = self._request(self.captain)
        self.assertEqual(401, status)
        self.assertFalse(result["ok"])
        self.assertNotIn("events", result)


if __name__ == "__main__":
    unittest.main()
