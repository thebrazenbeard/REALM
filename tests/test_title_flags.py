"""Bounded display-only role claims; real same-world ownership is required."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from realm.engine import Engine
from realm.geography import Geography
from realm.title_flags import OfficerGrant, issue_first_officer_grant, ship_display_name


WORLD = "title_test_world"
KEY = b"test-only-host-signing-key-000001"
NOW = 1_800_000_000


class TitleFlagTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "realm.sqlite3"
        self.engine = Engine(self.path)
        self.engine.join("Ada")
        self.engine.join("Bryn")
        self.engine.join("Cato")
        with closing(self.engine._connect()) as db, db:
            db.execute("INSERT INTO world_meta(key,value) VALUES ('world_id',?)", (WORLD,))
        self.geo = Geography(self.path)
        self.geo.add_place("universe", "Universe", "universe")
        self.geo.add_place("system", "System", "system", "universe")
        self.geo.add_place("planet", "Planet", "planet", "system")

    def register(self):
        self.geo.register_ship("horizon", "Horizon", "Ada", "planet")

    def display(self, actor, *, grant=None, world_id=WORLD,
                ship_id="horizon", key=KEY, now=NOW):
        with closing(self.engine._connect()) as db:
            return ship_display_name(
                db, actor=actor, ship_id=ship_id, world_id=world_id,
                officer_grant=grant, signing_key=key, now_utc=now,
            )

    def officer(self, *, actor="Bryn", world_id=WORLD,
                ship_id="horizon", expires=NOW + 120, now=NOW, key=KEY):
        with closing(self.engine._connect()) as db:
            return issue_first_officer_grant(
                db, actor=actor, ship_id=ship_id, world_id=world_id,
                signing_key=key, expires_utc=expires, now_utc=now,
            )

    def test_owner_earns_captain_display_only_after_host_registration(self):
        self.assertEqual("Ada", self.display("Ada"))
        self.register()
        self.assertEqual("Capt. Ada", self.display("Ada"))
        self.assertEqual("Bryn", self.display("Bryn"))

    def test_case_variants_render_canonical_joined_player(self):
        self.register()
        self.assertEqual("Capt. Ada", self.display("aDA"))
        self.assertEqual("Bryn", self.display("BRYN"))

    def test_first_officer_requires_host_signed_live_grant(self):
        self.register()
        grant = self.officer()
        self.assertIsInstance(grant, OfficerGrant)
        self.assertEqual("First Officer Bryn", self.display("Bryn", grant=grant))
        self.assertEqual("Cato", self.display("Cato", grant=grant))

    def test_self_asserted_grant_and_tampering_cannot_promote(self):
        self.register()
        grant = self.officer()
        self.assertEqual("Bryn", self.display("Bryn", grant=replace(grant, signature="0" * 64)))
        self.assertEqual("Bryn", self.display("Bryn", grant=replace(grant, actor="Bryn_")))
        self.assertEqual("Bryn", self.display("Bryn", grant=replace(grant, expires_utc=NOW + 3600)))
        self.assertEqual("Cato", self.display("Cato", grant=replace(grant, actor="Cato")))

    def test_owner_change_revokes_grant_without_rerouting_gameplay(self):
        self.register()
        grant = self.officer()
        with closing(self.engine._connect()) as db, db:
            db.execute("UPDATE geo_ships SET owner='Cato' WHERE id='horizon'")
        self.assertEqual("Bryn", self.display("Bryn", grant=grant))
        self.assertEqual("Capt. Cato", self.display("Cato"))
        self.assertEqual("Ada", self.display("Ada"))

    def test_expiration_denies_and_captain_not_overridden(self):
        self.register()
        grant = self.officer()
        self.assertEqual("Bryn", self.display("Bryn", grant=grant, now=NOW + 121))
        self.assertEqual("Capt. Ada", self.display("Ada", grant=grant))

    def test_world_binding_rejects_wrong_world_even_with_same_ship_id(self):
        self.register()
        grant = self.officer()
        self.assertEqual("Bryn", self.display("Bryn", grant=grant, world_id="otherworld"))
        self.assertEqual("Ada", self.display("Ada", world_id="otherworld"))

    def test_no_verification_key_or_bad_key_mints_no_officer_title(self):
        self.register()
        grant = self.officer()
        self.assertEqual("Bryn", self.display("Bryn", grant=grant, key=None))
        self.assertEqual("Bryn", self.display("Bryn", grant=grant, key=b"X" * 32))
        with self.assertRaises(ValueError):
            self.officer(key=b"weak")

    def test_unregistered_or_wrong_ship_cannot_mint(self):
        with self.assertRaises(PermissionError):
            self.officer()
        self.register()
        with self.assertRaises(PermissionError):
            self.officer(ship_id="elsewhere")
        self.assertEqual("Ada", self.display("Ada", ship_id="elsewhere"))

    def test_owner_cannot_grant_own_first_officer_title(self):
        self.register()
        with self.assertRaises(PermissionError):
            self.officer(actor="Ada")

    def test_unjoined_principal_is_not_promoted(self):
        self.register()
        self.assertEqual("NotJoined", self.display("NotJoined"))
        with self.assertRaises(PermissionError):
            self.officer(actor="NotJoined")

    def test_signing_requires_explicit_bounded_host_time(self):
        self.register()
        for expires, now in ((NOW, NOW), (NOW + 86401, NOW), (NOW + 120, -1),
                             (True, NOW), (NOW + 100, 1.25)):
            with self.subTest(expires=expires, now=now):
                with self.assertRaises(ValueError):
                    self.officer(expires=expires, now=now)

    def test_rendering_does_not_modify_database(self):
        self.register()
        with closing(self.engine._connect()) as db:
            before = [tuple(x) for x in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
            count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        grant = self.officer()
        self.assertEqual("First Officer Bryn", self.display("Bryn", grant=grant))
        with closing(self.engine._connect()) as db:
            after = [tuple(x) for x in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
            final_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(count, final_count)

    def test_invalid_actor_and_world_identifiers_are_rejected(self):
        self.register()
        for actor in ("<Ada>", "A\nB", "𝔸da", ""):
            with self.subTest(actor=actor):
                with self.assertRaises(ValueError):
                    self.display(actor)
        with self.assertRaises(ValueError):
            self.display("Ada", world_id="wrong/world")


if __name__ == "__main__":
    unittest.main()
