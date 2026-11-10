"""Opt-in ship-role appointment uses the real world writer and revokes cleanly."""

from __future__ import annotations

import concurrent.futures
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from realm.engine import Engine
from realm.gateway import Gateway
from realm.geography import Geography
from realm.crew_roster import CrewRoster
from realm.title_flags import (
    issue_first_officer_grant, ship_display_name, ship_roster_display_name,
)


WORLD = "crew_world"
NOW = 1_800_000_000
KEY = b"test-only-crew-display-key-32-characters-123"


class CrewRosterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        self.gateway = Gateway(self.path, world_id=WORLD)
        self.engine = self.gateway.engine
        for player in ("Ada", "Bryn", "Cato", "Dara"):
            self.engine.join(player)
        self.token = self.gateway.credentials.provision("Ada")
        self.rival_token = self.gateway.credentials.provision("Cato")
        geo = Geography(self.path)
        geo.add_place("universe", "Universe", "universe")
        geo.add_place("system", "System", "system", "universe")
        geo.add_place("planet", "Planet", "planet", "system")
        geo.register_ship("horizon", "Horizon", "Ada", "planet")
        self.roster = CrewRoster(self.path, world_id=WORLD)

    def _tables(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {row[0] for row in db.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table'"
            )}

    def _display(self, actor, *, ship="horizon", world=WORLD):
        with closing(self.engine._connect()) as db:
            return ship_roster_display_name(db, actor=actor, ship_id=ship, world_id=world)

    def _grant(self, actor="Bryn"):
        with closing(self.engine._connect()) as db:
            return issue_first_officer_grant(
                db, actor=actor, ship_id="horizon", world_id=WORLD,
                signing_key=KEY, now_utc=NOW, expires_utc=NOW + 100,
            )

    def _signed_display(self, grant, *, now=NOW):
        with closing(self.engine._connect()) as db:
            return ship_display_name(
                db, actor="Bryn", ship_id="horizon", world_id=WORLD,
                officer_grant=grant, signing_key=KEY, now_utc=now,
            )

    def test_schema_is_explicit_and_install_is_idempotent(self):
        self.assertNotIn("crew_role_state", self._tables())
        with self.assertRaises(RuntimeError):
            self.roster.appoint(
                self.token, ship_id="horizon", officer="Bryn",
                action_id="preinstall", now_utc=NOW,
            )
        self.assertNotIn("crew_role_state", self._tables())
        self.roster.install()
        self.roster.install()
        self.assertIn("crew_role_state", self._tables())
        self.assertIsNone(self.roster.current("horizon"))
        self.assertEqual("Ada", self._display("Ada", ship="unknown"))

    def test_authenticated_owner_appoints_and_displays_after_restart(self):
        self.roster.install()
        result = self.roster.appoint(
            self.token, ship_id="horizon", officer="Bryn",
            action_id="appoint1", now_utc=NOW,
        )
        self.assertEqual("Bryn", result["officer"])
        self.assertTrue(result["applied"])
        self.assertFalse(result["replayed"])
        self.assertTrue(result["active"])
        self.assertEqual("Capt. Ada", self._display("aDA"))
        self.assertEqual("First Officer Bryn", self._display("BRYN"))
        self.assertEqual("Cato", self._display("Cato"))
        restarted = CrewRoster(self.path, world_id=WORLD)
        self.assertEqual("Bryn", restarted.current("horizon")["officer"])
        self.assertEqual("First Officer Bryn", self._display("Bryn"))

    def test_nonowner_token_and_unjoined_actor_cannot_appoint(self):
        self.roster.install()
        with self.assertRaises(PermissionError):
            self.roster.appoint(
                self.rival_token, ship_id="horizon", officer="Bryn",
                action_id="badactor1", now_utc=NOW,
            )
        with self.assertRaises(PermissionError):
            self.roster.appoint(
                "garbage", ship_id="horizon", officer="Bryn",
                action_id="badtoken1", now_utc=NOW,
            )
        with self.assertRaises(PermissionError):
            self.roster.appoint(
                self.token, ship_id="horizon", officer="Unjoined",
                action_id="unjoined1", now_utc=NOW,
            )
        self.assertIsNone(self.roster.current("horizon"))

    def test_revoke_immediately_hides_roster_and_invalidates_signed_display(self):
        self.roster.install()
        with self.assertRaises(PermissionError):
            self._grant()
        self.roster.appoint(
            self.token, ship_id="horizon", officer="Bryn",
            action_id="appoint1", now_utc=NOW,
        )
        grant = self._grant()
        self.assertEqual("First Officer Bryn", self._signed_display(grant))
        result = self.roster.revoke(
            self.token, ship_id="horizon",
            action_id="revoke1", now_utc=NOW + 1,
        )
        self.assertEqual("Bryn", result["officer"])
        self.assertTrue(result["applied"])
        self.assertIsNone(self.roster.current("horizon"))
        self.assertEqual("Bryn", self._display("Bryn"))
        self.assertEqual("Bryn", self._signed_display(grant))
        with self.assertRaises(PermissionError):
            self._grant()

    def test_owner_change_and_aba_do_not_resurrect_old_officer(self):
        self.roster.install()
        self.roster.appoint(
            self.token, ship_id="horizon", officer="Bryn",
            action_id="appoint1", now_utc=NOW,
        )
        grant = self._grant()
        with closing(self.engine._connect()) as db, db:
            db.execute("UPDATE geo_ships SET owner='Cato' WHERE id='horizon'")
        self.assertIsNone(self.roster.current("horizon"))
        self.assertEqual("Bryn", self._signed_display(grant))
        self.assertEqual("Capt. Cato", self._display("Cato"))
        with closing(self.engine._connect()) as db, db:
            db.execute("UPDATE geo_ships SET owner='Ada' WHERE id='horizon'")
        self.assertIsNone(self.roster.current("horizon"))
        self.assertEqual("Bryn", self._signed_display(grant))
        self.assertEqual("Bryn", self._display("Bryn"))

    def test_replays_are_once_only_and_conflicting_action_id_is_denied(self):
        self.roster.install()
        first = self.roster.appoint(
            self.token, ship_id="horizon", officer="Bryn",
            action_id="appoint1", now_utc=NOW,
        )
        replay = self.roster.appoint(
            self.token, ship_id="horizon", officer="Bryn",
            action_id="appoint1", now_utc=NOW,
        )
        self.assertEqual(first["event_id"], replay["event_id"])
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["active"])
        with self.assertRaises(ValueError):
            self.roster.appoint(
                self.token, ship_id="horizon", officer="Cato",
                action_id="appoint1", now_utc=NOW,
            )
        self.roster.revoke(self.token, ship_id="horizon", action_id="revoke1", now_utc=NOW + 1)
        replay_after_revoke = self.roster.appoint(
            self.token, ship_id="horizon", officer="Bryn",
            action_id="appoint1", now_utc=NOW,
        )
        self.assertTrue(replay_after_revoke["replayed"])
        self.assertFalse(replay_after_revoke["active"])
        self.assertIsNone(self.roster.current("horizon"))
        with closing(self.engine._connect()) as db:
            self.assertEqual(2, db.execute(
                "SELECT COUNT(*) FROM crew_role_events WHERE ship_id='horizon'"
            ).fetchone()[0])

    def test_new_officer_requires_explicit_revoke(self):
        self.roster.install()
        self.roster.appoint(
            self.token, ship_id="horizon", officer="Bryn",
            action_id="appoint1", now_utc=NOW,
        )
        with self.assertRaises(ValueError):
            self.roster.appoint(
                self.token, ship_id="horizon", officer="Cato",
                action_id="appoint2", now_utc=NOW + 1,
            )
        self.roster.revoke(self.token, ship_id="horizon", action_id="revoke1", now_utc=NOW + 2)
        appointed = self.roster.appoint(
            self.token, ship_id="horizon", officer="Cato",
            action_id="appoint3", now_utc=NOW + 3,
        )
        self.assertTrue(appointed["active"])
        self.assertEqual("First Officer Cato", self._display("Cato"))

    def test_two_competing_writers_have_one_winner(self):
        self.roster.install()

        def appoint(officer):
            return self.roster.appoint(
                self.token, ship_id="horizon", officer=officer,
                action_id="appoint_" + officer, now_utc=NOW,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(appoint, who) for who in ("Bryn", "Cato")]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(("ok", future.result()))
                except ValueError as exc:
                    outcomes.append(("conflict", str(exc)))
        self.assertEqual(["conflict", "ok"], sorted(x[0] for x in outcomes))
        with closing(self.engine._connect()) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM crew_role_events WHERE ship_id='horizon'"
            ).fetchone()[0])
        self.assertIn(self.roster.current("horizon")["officer"], ("Bryn", "Cato"))

    def test_rotated_and_revoked_tokens_fail_at_writer_boundary(self):
        self.roster.install()
        new_token = self.gateway.credentials.rotate("Ada")
        with self.assertRaises(PermissionError):
            self.roster.appoint(
                self.token, ship_id="horizon", officer="Bryn",
                action_id="oldtoken", now_utc=NOW,
            )
        self.roster.appoint(
            new_token, ship_id="horizon", officer="Bryn",
            action_id="newtoken", now_utc=NOW,
        )
        self.gateway.credentials.revoke("Ada")
        with self.assertRaises(PermissionError):
            self.roster.revoke(
                new_token, ship_id="horizon",
                action_id="afterrevoke", now_utc=NOW + 1,
            )
        self.assertEqual("Bryn", self.roster.current("horizon")["officer"])

    def test_wrong_world_binding_fails_closed(self):
        self.roster.install()
        alien = CrewRoster(self.path, world_id="another_world")
        with self.assertRaises(PermissionError):
            alien.appoint(
                self.token, ship_id="horizon", officer="Bryn",
                action_id="wrongworld", now_utc=NOW,
            )
        with self.assertRaises(PermissionError):
            alien.current("horizon")
        self.assertEqual("Bryn", self._display("Bryn", world="another_world"))

    def test_invalid_inputs_and_missing_schema_do_not_commit(self):
        self.roster.install()
        bad_cases = [
            ("<ship>", "Bryn", "safe1", NOW),
            ("horizon", "<Bryn>", "safe2", NOW),
            ("horizon", "Bryn", "a\nline", NOW),
            ("horizon", "Bryn", "safe4", True),
        ]
        for ship, officer, action, now in bad_cases:
            with self.subTest(ship=ship, officer=officer, action=action):
                with self.assertRaises(ValueError):
                    self.roster.appoint(
                        self.token, ship_id=ship, officer=officer,
                        action_id=action, now_utc=now,
                    )
        with closing(self.engine._connect()) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM crew_role_events"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
