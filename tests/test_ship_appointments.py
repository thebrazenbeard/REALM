"""Host-only, ship-scoped appointments cannot confer global character rank."""

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

    def test_role_requires_exact_trusted_ship_context(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        self.assertEqual("captain", self.roles.role("Nova_Crew", "trader_7"))
        self.assertEqual("Capt. Nova Crew", self.roles.label("Nova_Crew", trusted_ship_id="trader_7"))
        self.assertEqual("Nova Crew", self.roles.label("Nova_Crew"))
        self.assertEqual("Nova Crew", self.roles.label("Nova_Crew", trusted_ship_id="other_1"))
        self.assertEqual("Nova_Crew", CredentialStore(self.engine).authenticate(
            CredentialStore(self.engine).provision("Nova_Crew")
        ))

    def test_multiple_ships_and_scoped_first_officer(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        self.roles.grant("Nova_Crew", "freighter_9", "first_officer")
        self.roles.grant("Alia_Vale", "trader_7", "first_officer")
        self.assertEqual("Capt. Nova Crew", self.roles.label("Nova_Crew", trusted_ship_id="trader_7"))
        self.assertEqual("First Officer Nova Crew", self.roles.label("Nova_Crew", trusted_ship_id="freighter_9"))
        self.assertEqual("First Officer Alia Vale", self.roles.label("Alia_Vale", trusted_ship_id="trader_7"))
        self.assertIsNone(self.roles.role("Alia_Vale", "freighter_9"))

    def test_unique_active_position_requires_explicit_revocation(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        with self.assertRaises(ValueError):
            self.roles.grant("Alia_Vale", "trader_7", "captain")
        self.assertEqual("captain", self.roles.role("Nova_Crew", "trader_7"))
        self.assertTrue(self.roles.revoke("Nova_Crew", "trader_7"))
        self.roles.grant("Alia_Vale", "trader_7", "captain")
        self.assertEqual("Capt. Alia Vale", self.roles.label("Alia_Vale", trusted_ship_id="trader_7"))
        self.assertEqual("Nova Crew", self.roles.label("Nova_Crew", trusted_ship_id="trader_7"))

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
        self.assertIsNone(other_roles.role("Nova_Crew", "trader_7"))
        self.assertEqual("Nova_Crew", other_roles.label("Nova_Crew", trusted_ship_id="trader_7"))

    def test_revocation_persists_across_engine_restart(self):
        self.roles.grant("Nova_Crew", "trader_7", "captain")
        self.assertEqual("captain", ShipAppointmentStore(Engine(self.db_path)).role("nova_crew", "TRADER_7"))
        self.assertTrue(self.roles.revoke("Nova_Crew", "TRADER_7"))
        self.assertFalse(self.roles.revoke("Nova_Crew", "trader_7"))
        restarted = ShipAppointmentStore(Engine(self.db_path))
        self.assertIsNone(restarted.role("Nova_Crew", "trader_7"))
        self.assertEqual("Nova Crew", restarted.label("Nova_Crew", trusted_ship_id="trader_7"))
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
        self.assertIsNone(self.roles.label("Ghost"))
        self.assertFalse(hasattr(self.roles, "provision"))

    def test_host_controlled_display_text_cannot_create_role(self):
        PlayerProfileStore(self.engine).set("Rin_Wolf", "Capt. Rin Wolf")
        self.assertIsNone(self.roles.role("Rin_Wolf", "trader_7"))
        self.assertEqual("Rin Wolf", self.roles.label("Rin_Wolf"))
        self.roles.grant("Rin_Wolf", "trader_7", "first_officer")
        self.assertEqual("First Officer Rin Wolf",
                         self.roles.label("Rin_Wolf", trusted_ship_id="trader_7"))


if __name__ == "__main__":
    unittest.main()
