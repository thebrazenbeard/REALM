"""Deterministic stdlib test suite; run via python -m unittest discover -s tests."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm import Engine


class RealmEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "alpha.sqlite3"
        self.engine = Engine(self.db_path)

    def test_first_login_creates_demo_world(self):
        result = self.engine.join("Patrick")
        self.assertIn("DEMO WORLD", result)
        self.assertIn("Exits: north", result)

    def test_persistent_player_position_across_process_equivalent_restart(self):
        self.engine.join("Patrick")
        result = self.engine.act("Patrick", "north")
        self.assertIn("The Corridor", result)
        self.assertIn("The Corridor", Engine(self.db_path).join("patrick"))

    def test_bad_exit_does_not_move(self):
        self.engine.join("Patrick")
        self.assertIn("No exit", self.engine.act("Patrick", "west"))
        self.assertIn("The Atrium", self.engine.act("Patrick", "look"))

    def test_users_share_world_but_have_independent_positions(self):
        self.engine.join("Patrick")
        self.engine.join("Vera")
        self.assertIn("Vera", self.engine.act("Patrick", "look"))
        self.engine.act("Vera", "north")
        self.assertNotIn("Also here", self.engine.act("Patrick", "look"))
        self.assertIn("Patrick, Vera", self.engine.act("Patrick", "who"))

    def test_durable_speech_is_visible_to_another_player(self):
        self.engine.join("Patrick")
        self.engine.join("Vera")
        self.engine.act("Patrick", "say This place is ours to explore.")
        history = Engine(self.db_path).act("Vera", "listen")
        self.assertIn("Patrick said: This place is ours to explore.", history)

    def test_rejected_inputs_do_not_append_events(self):
        with self.assertRaises(ValueError):
            self.engine.join("../admin")
        self.engine.join("Patrick")
        self.assertIn("Unknown", self.engine.act("Patrick", "destroy everything"))
        self.assertIn("280", self.engine.act("Patrick", "say " + "a" * 281))

    def test_isolated_worlds(self):
        self.engine.join("Patrick")
        other = Engine(Path(self.tmp.name) / "beta.sqlite3")
        other.join("Vera")
        self.assertNotIn("Vera", self.engine.act("Patrick", "who"))

    def test_two_players_contend_for_one_item(self):
        self.engine.join("Patrick")
        self.engine.join("Vera")
        self.assertIn("Brass token", self.engine.act("Patrick", "look"))
        self.assertIn("You take Brass token", self.engine.act("Patrick", "take token"))
        self.assertIn("not here", self.engine.act("Vera", "take token"))
        self.assertIn("Brass token", self.engine.act("Patrick", "inventory"))
        self.assertIn("You drop Brass token", self.engine.act("Patrick", "drop token"))
        self.assertIn("You take Brass token", self.engine.act("Vera", "take token"))
        self.assertIn("Brass token", Engine(self.db_path).act("Vera", "inventory"))

    def test_simultaneous_take_has_only_one_winner(self):
        self.engine.join("Patrick")
        self.engine.join("Vera")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda name: Engine(self.db_path).act(name, "take token"), ["Patrick", "Vera"]))
        self.assertEqual(1, sum("You take" in result for result in results))
        self.assertEqual(1, sum("not here" in result for result in results))

    def test_idempotent_retry_does_not_duplicate_event(self):
        self.engine.join("Patrick")
        self.assertEqual("You take Brass token.", self.engine.act("Patrick", "take token", action_id="0001"))
        self.assertEqual("You take Brass token.", self.engine.act("Patrick", "take token", action_id="0001"))
        with self.assertRaises(ValueError):
            self.engine.act("Patrick", "drop token", action_id="0001")
        history = self.engine.act("Patrick", "history 10")
        self.assertEqual(1, history.count("took token"))


if __name__ == "__main__":
    unittest.main()
