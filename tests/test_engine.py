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

    def test_examine_reads_accessible_item_descriptions_without_leaking_inventory(self):
        self.engine.join("Patrick")
        self.engine.join("Vera")
        description = "A small unmarked token for testing shared item ownership."
        self.assertIn(description, self.engine.act("Patrick", "examine token"))
        self.assertIn(description, self.engine.act("Patrick", "x Brass token"))
        self.assertIn(description, self.engine.act("Patrick", "look at token"))
        self.assertIn("The Atrium", self.engine.act("Patrick", "look around"))
        self.assertIn("Examine what?", self.engine.act("Patrick", "examine"))

        self.assertIn("cannot examine", self.engine.act("Patrick", "examine wrench"))
        self.engine.act("Patrick", "take token")
        self.assertIn(description, self.engine.act("Patrick", "examine token"))
        self.assertIn("cannot examine", self.engine.act("Vera", "examine token"))

        self.engine.act("Patrick", "north")
        self.engine.act("Patrick", "east")
        self.assertIn("A tool left behind", self.engine.act("Patrick", "look wrench"))
        self.assertIn(description, self.engine.act("Patrick", "examine token"))
        self.assertIn("cannot examine", self.engine.act("Vera", "x wrench"))

    def test_examine_does_not_change_world_history(self):
        self.engine.join("Patrick")
        before = self.engine.act("Patrick", "history 10")
        self.engine.act("Patrick", "examine token")
        self.assertEqual(before, self.engine.act("Patrick", "history 10"))

    def test_observation_action_id_rechecks_current_access_and_state(self):
        self.engine.join("Ada")
        self.engine.join("Bob")
        self.assertIn("small unmarked token", self.engine.act("Ada", "examine token", action_id="observe-1"))
        self.engine.act("Bob", "take token")
        self.assertIn("cannot examine", self.engine.act("Ada", "examine token", action_id="observe-1"))
        with self.assertRaises(ValueError):
            self.engine.act("Ada", "look", action_id="observe-1")

        self.assertIn("The Atrium", self.engine.act("Ada", "look", action_id="look-1"))
        self.assertIn("atrium", self.engine.act("Ada", "where", action_id="where-1").lower())
        self.engine.act("Ada", "north")
        self.assertIn("The Corridor", self.engine.act("Ada", "look", action_id="look-1"))
        self.assertIn("corridor", self.engine.act("Ada", "where", action_id="where-1").lower())

        self.assertIn("nothing", self.engine.act("Ada", "inventory", action_id="inv-1"))
        self.engine.act("Ada", "east")
        self.engine.act("Ada", "take wrench")
        self.assertIn("Worn wrench", self.engine.act("Ada", "inventory", action_id="inv-1"))

    def test_observation_action_id_freshens_history_and_who(self):
        self.engine.join("Ada")
        self.assertIn("Ada", self.engine.act("Ada", "who", action_id="who-1"))
        self.engine.join("Bob")
        self.assertIn("Bob", self.engine.act("Ada", "who", action_id="who-1"))
        first = self.engine.act("Ada", "listen", action_id="listen-1")
        self.engine.act("Bob", "say A new line of dialogue")
        second = self.engine.act("Ada", "listen", action_id="listen-1")
        self.assertNotIn("new line", first)
        self.assertIn("new line", second)

    def test_quit_action_id_is_bound_without_changing_plain_quit(self):
        self.engine.join("Ada")
        self.assertIn("Goodbye", self.engine.act("Ada", "quit", action_id="quit-1"))
        self.assertIn("Goodbye", self.engine.act("Ada", "quit", action_id="quit-1"))
        with self.assertRaises(ValueError):
            self.engine.act("Ada", "examine token", action_id="quit-1")
        self.assertIn("Goodbye", self.engine.act("NotJoined", "quit"))

    def test_history_only_exposes_events_since_latest_room_entry(self):
        self.engine.join("Ada")
        self.engine.act("Ada", "say The private opening words")
        self.engine.join("Bob")
        self.assertNotIn("private opening", self.engine.act("Bob", "history 50"))
        self.assertIn("private opening", self.engine.act("Ada", "history 50"))
        self.engine.act("Ada", "say The shared sentence")
        self.assertIn("shared sentence", self.engine.act("Bob", "listen"))

        self.engine.act("Bob", "north")
        self.engine.act("Ada", "say While Bob was away")
        self.engine.act("Bob", "south")
        bob_history = self.engine.act("Bob", "history 50")
        self.assertNotIn("private opening", bob_history)
        self.assertNotIn("shared sentence", bob_history)
        self.assertNotIn("While Bob was away", bob_history)
        self.engine.act("Ada", "say Bob is back")
        self.assertIn("Bob is back", self.engine.act("Bob", "history 50"))

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
