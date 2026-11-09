"""Host-only ship pilot authorization over real REALM credentials and SQLite."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from realm.gateway import Gateway
from realm.ship_command_auth import AuthenticatedShipCommands
from realm.world_clock import WorldClock


UTC = timezone.utc
AT = int(datetime(2026, 11, 7, 18, 0, tzinfo=UTC).timestamp())


class ShipCommandAuthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        self.gateway = Gateway(self.path, world_id="ship_auth_world")
        for name in ("Captain", "Intruder"):
            self.gateway.join(name)
        self.captain_token = self.gateway.credentials.provision("Captain")
        self.intruder_token = self.gateway.credentials.provision("Intruder")
        geo = self.gateway.geography
        geo.add_place("universe", "Universe", "universe")
        geo.add_place("system", "System", "system", "universe")
        geo.add_place("earth", "Earth", "planet", "system")
        geo.add_place("moon", "Moon", "planet", "system")
        geo.link("earth", "moon", "ship", seconds=50)
        geo.register_ship("vesta", "Vesta", "Captain", "earth")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.pilot = AuthenticatedShipCommands(self.gateway, self.clock)

    def _depart(self, token=None, action_id="depart-1", destination="moon"):
        return self.pilot.depart(
            self.captain_token if token is None else token,
            "vesta", destination, now=AT, action_id=action_id,
        )

    def test_owner_token_departure_has_correct_actor_and_utc_receipt(self):
        result = self._depart()
        self.assertEqual("Captain", result["principal"])
        self.assertEqual("in_transit", result["departure"]["state"])
        self.assertEqual("vesta", self.clock.lookup(result["event_id"]).payload["ship_id"])
        with closing(sqlite3.connect(self.path)) as db:
            actor = db.execute(
                "SELECT actor FROM main.geo_ship_events WHERE ship_id='vesta' AND kind='depart'"
            ).fetchone()[0]
        self.assertEqual("Captain", actor)
        self.assertNotIn(self.captain_token, str(result))

    def test_other_players_token_cannot_impersonate_ship_owner(self):
        with self.assertRaises(PermissionError):
            self._depart(token=self.intruder_token)
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])
        with self.assertRaises(TypeError):
            self.pilot.depart(self.intruder_token, "vesta", "moon", actor="Captain",
                              now=AT, action_id="forged")

    def test_revoked_and_rotated_tokens_fail_closed(self):
        self.gateway.credentials.revoke("Captain")
        with self.assertRaises(PermissionError):
            self._depart()
        new_token = self.gateway.credentials.provision("Captain")
        result = self._depart(token=new_token)
        self.assertEqual("Captain", result["principal"])
        self.gateway.credentials.rotate("Captain")
        with self.assertRaises(PermissionError):
            self._depart(token=new_token, action_id="depart-2")

    def test_wrong_world_token_and_unjoined_actor_rejected(self):
        another = Gateway(self.path.parent / "other.sqlite3", world_id="otherworld")
        another.join("Captain")
        other_token = another.credentials.provision("Captain")
        with self.assertRaises(PermissionError):
            self._depart(token=other_token)
        ghost = self.gateway.credentials.provision("Ghost")
        with self.assertRaises(PermissionError):
            self._depart(token=ghost)

    def test_replay_and_new_action_collision_preserve_single_departure(self):
        first = self._depart()
        self.assertEqual(first, self._depart())
        with self.assertRaises(ValueError):
            self._depart(action_id="depart-2")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(1, db.execute(
                "SELECT count(*) FROM main.geo_ship_events WHERE ship_id='vesta' AND kind='depart'"
            ).fetchone()[0])

    def test_wrong_route_never_moves_ship_or_creates_arrival(self):
        with self.assertRaises(ValueError):
            self._depart(destination="universe")
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])

    def test_temp_credentials_cannot_forge_principal(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TEMP TABLE player_credentials(player_name TEXT,token_digest TEXT)")
            db.execute("INSERT INTO temp.player_credentials VALUES ('Captain',?)",
                       (self.gateway.credentials._digest(self.intruder_token),))
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(PermissionError):
                self.pilot.depart_in_transaction(
                    db, self.intruder_token, "vesta", "moon", now=AT, action_id="spoof",
                )
            db.rollback()
        self.assertEqual("docked", self.gateway.geography.ship_status("vesta")["state"])

    def test_competing_departures_serialized(self):
        def attempt(key):
            try:
                self._depart(action_id=key)
                return "departed"
            except ValueError:
                return "already-in-transit"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("attempt-A", "attempt-B")))
        self.assertEqual(["already-in-transit", "departed"], sorted(results))


if __name__ == "__main__":
    unittest.main()
