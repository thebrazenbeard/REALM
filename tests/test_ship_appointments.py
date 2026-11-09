"""Ship appointments expose trusted rank separately from freeform display names."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.appointments import ShipAppointmentStore
from realm.auth import CredentialStore
from realm.engine import Engine
from realm.profiles import PlayerProfileStore


class ShipAppointmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "world.sqlite3"
        self.engine = Engine(self.db_path)
        for player in ("Nova_Crew", "Alia_Vale", "Rin_Wolf"):
            self.engine.join(player)
        profiles = PlayerProfileStore(self.engine)
        profiles.set("Nova_Crew", "Nova Crew")
        profiles.set("Alia_Vale", "Alia Vale")
        self.roles = ShipAppointmentStore(self.engine)
    def assert_identity(self, actor, ship, display, role, title):
        identity = self.roles.identity(actor, trusted_ship_id=ship)
        self.assertIsNotNone(identity)
        self.assertEqual((display, role, title),
                         (identity.display_name, identity.role, identity.title))
        return identity

    def test_rank_only_from_exact_trusted_ship_context(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        self.assertEqual("captain", self.roles.role("Nova_Crew", "trader_7"))
        view = self.assert_identity("Nova_Crew", "trader_7", "Nova Crew", "captain", "Capt.")
        self.assertEqual("Nova_Crew", view.player_id)
        self.assert_identity("Nova_Crew", None, "Nova Crew", None, None)
        self.assert_identity("Nova_Crew", "other_1", "Nova Crew", None, None)
        token = CredentialStore(self.engine).provision("Nova_Crew")
        self.assertEqual("Nova_Crew", CredentialStore(self.engine).authenticate(token))

    def test_multiple_ships_and_scoped_first_officer(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        self.roles.grant("Nova_Crew", "freighter_9", "first_officer")
        self.roles.grant("Alia_Vale", "trader_7", "first_officer")
        self.assert_identity("Nova_Crew", "trader_7", "Nova Crew", "captain", "Capt.")
        self.assert_identity("Nova_Crew", "freighter_9", "Nova Crew", "first_officer", "First Officer")
        self.assert_identity("Alia_Vale", "trader_7", "Alia Vale", "first_officer", "First Officer")
        self.assert_identity("Alia_Vale", "freighter_9", "Alia Vale", None, None)

    def test_unique_active_position_needs_explicit_revocation(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        with self.assertRaises(ValueError):
            self.roles.grant("Alia_Vale", "trader_7", "captain")
        self.assertEqual("captain", self.roles.role("Nova_Crew", "trader_7"))
        self.assertTrue(self.roles.revoke("Nova_Crew", "trader_7"))
        self.roles.grant("Alia_Vale", "trader_7", "captain")
        self.assert_identity("Alia_Vale", "trader_7", "Alia Vale", "captain", "Capt.")
        self.assert_identity("Nova_Crew", "trader_7", "Nova Crew", None, None)

    def test_existing_role_cannot_silently_change(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        with self.assertRaises(ValueError):
            self.roles.grant("Nova_Crew", "trader_7", "first_officer")
        self.assertEqual("captain", self.roles.role("Nova_Crew", "trader_7"))

    def test_appointments_do_not_cross_world_files(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        other = Engine(Path(self.tmp.name) / "other.sqlite3")
        other.join("Nova_Crew")
        other_roles = ShipAppointmentStore(other)
        identity = other_roles.identity("Nova_Crew", trusted_ship_id="trader_7")
        self.assertEqual(("Nova_Crew", None, None),
                         (identity.display_name, identity.role, identity.title))
    def test_revocation_persists_across_engine_restart(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        restarted = ShipAppointmentStore(Engine(self.db_path))
        self.assertEqual("captain", restarted.role("nova_crew", "TRADER_7"))
        self.assertTrue(self.roles.revoke("Nova_Crew", "TRADER_7"))
        self.assertFalse(self.roles.revoke("Nova_Crew", "trader_7"))
        self.assert_identity("Nova_Crew", "trader_7", "Nova Crew", None, None)
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        self.assertEqual("captain", restarted.role("Nova_Crew", "trader_7"))

    def test_spoofed_roles_and_unjoined_actors_fail_closed(self):
        for player, ship, role in (
            ("Ghost", "trader_7", "captain"),
            ("Nova_Crew", "trader_7", "admiral"),
            ("Nova_Crew", "trader_7", "CAPTAIN"),
            ("Nova_Crew", "trader\n7", "captain"),
            ("Nova_Crew", "", "captain"),
        ):
            with self.subTest(player=player, ship=ship, role=role):
                with self.assertRaises(ValueError):
                    self.roles.grant(player, ship, role)
        self.assertIsNone(self.roles.role("Nova_Crew", "trader_7"))
        self.assertEqual("Unknown command. Type 'help' for options.",
                         self.engine.act("Nova_Crew", "appoint trader_7 captain"))
        self.assertIsNone(self.roles.identity("Ghost"))
        self.assertFalse(hasattr(self.roles, "label"))
    def test_rank_looking_display_never_becomes_authority(self):
        profiles = PlayerProfileStore(self.engine)
        rank_looking = (
            "Capt. Rin Wolf",
            "“Capt. Rin Wolf”",
            "“First Officer Rin Wolf”",
            "“Captain” Rin Wolf",
            "“Capt” Rin Wolf",
            "“First Officer” Rin Wolf",
            "“First-Officer” Rin Wolf",
            "Ｃａｐｔ. Rin Wolf",
            "Сapt. Rin Wolf",
            "Ƈapt. Rin Wolf",
            "ᑕapt. Rin Wolf",
        )
        for label in rank_looking:
            with self.subTest(label=label):
                profiles.set("Rin_Wolf", label)
                view = self.assert_identity("Rin_Wolf", "trader_7", label, None, None)
                self.assertEqual("Rin_Wolf", view.player_id)
        self.roles.grant("Rin_Wolf", "trader_7", "first_officer")
        for label in rank_looking:
            with self.subTest(granted_label=label):
                profiles.set("Rin_Wolf", label)
                self.assert_identity("Rin_Wolf", "trader_7", label, "first_officer", "First Officer")
                self.assert_identity("Rin_Wolf", None, label, None, None)
    def test_normal_names_are_preserved_without_rank(self):
        profiles = PlayerProfileStore(self.engine)
        for label in ("“Rin Wolf”", "Captaincy Rin", "First Officerly Rin"):
            with self.subTest(label=label):
                profiles.set("Rin_Wolf", label)
                self.assert_identity("Rin_Wolf", None, label, None, None)

    def test_structured_role_does_not_depend_on_name_spelling(self):
        profiles = PlayerProfileStore(self.engine)
        profiles.set("Rin_Wolf", "Rin Wolf")
        self.roles.grant("Rin_Wolf", "trader_7", "captain")
        self.assert_identity("Rin_Wolf", "trader_7", "Rin Wolf", "captain", "Capt.")
        profiles.set("Rin_Wolf", "“First-Officer” Rin Wolf")
        self.assert_identity("Rin_Wolf", "trader_7", "“First-Officer” Rin Wolf", "captain", "Capt.")


if __name__ == "__main__":
    unittest.main()
