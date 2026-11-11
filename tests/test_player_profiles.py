"""Opt-in character display names keep authenticated player IDs stable."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.auth import CredentialStore
from realm.engine import Engine
from realm.profiles import PlayerProfileStore


class PlayerProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "world.sqlite3"
        self.engine = Engine(self.db_path)
        self.engine.join("Nova_Crew")
        self.profiles = PlayerProfileStore(self.engine)

    def test_host_sets_display_name_without_changing_login_principal(self):
        token = CredentialStore(self.engine).provision("Nova_Crew")
        self.assertEqual("Nova_Crew", CredentialStore(self.engine).authenticate(token))
        self.assertEqual("Nova_Crew", self.profiles.get("Nova_Crew"))
        self.assertEqual("Nova Crew", self.profiles.set("Nova_Crew", "Nova Crew"))
        self.assertEqual("Nova Crew", PlayerProfileStore(Engine(self.db_path)).get("nova_crew"))
        self.assertEqual("Nova_Crew", CredentialStore(self.engine).authenticate(token))

    def test_unknown_actor_cannot_receive_profile(self):
        with self.assertRaises(ValueError):
            self.profiles.set("Nonexistent", "First Officer")
        self.assertIsNone(self.profiles.get("Nonexistent"))

    def test_invalid_display_names_denied_without_writing(self):
        names = ("", "   ", "x" * 65, "A\nB", "<script>", "A\u202eB", "A\x00B")
        for name in names:
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                self.profiles.set("Nova_Crew", name)
        self.assertEqual("Nova_Crew", self.profiles.get("Nova_Crew"))

    def test_unicode_and_spaces_are_normalized(self):
        self.assertEqual("Alia “Scout” Vale", self.profiles.set("Nova_Crew", "  Alia   “Scout” Vale  "))

    def test_separate_worlds_do_not_share_names(self):
        second_db = Path(self.tmp.name) / "other.sqlite3"
        other = Engine(second_db)
        other.join("Nova_Crew")
        other_profiles = PlayerProfileStore(other)
        self.profiles.set("Nova_Crew", "Captain Nova")
        self.assertEqual("Nova_Crew", other_profiles.get("Nova_Crew"))

    def test_credentials_remain_distinct_from_display_metadata(self):
        self.profiles.set("Nova_Crew", "Capt. Nova Crew")
        with closing(sqlite3.connect(self.db_path)) as db:
            names = [row[1] for row in db.execute("PRAGMA table_info(player_profiles)")]
            self.assertEqual(["player_name", "display_name", "updated_at"], names)
        self.assertFalse(hasattr(self.profiles, "provision"))


if __name__ == "__main__":
    unittest.main()
