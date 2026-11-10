"""Actual REALM Engine/Crafting substrate: two-player resource coordination."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from experiments.realm_game_theory.trial import (
    _play_round, always, avoid_last, run_trial, seeded_random,
)


class ScarceResourceGameTests(unittest.TestCase):
    def test_split_locations_give_both_players_full_real_inventory(self):
        trial = run_trial(always("A"), always("B"), rounds=2, seed=7)
        self.assertEqual((6, 6), trial.total_payoffs)
        self.assertEqual(0, trial.collision_count)
        self.assertEqual(1.0, trial.efficiency)
        self.assertEqual([(3, 3), (3, 3)], [r.payoffs for r in trial.rounds])

    def test_prior_opponent_choice_heuristic_reduces_collisions(self):
        fixed = run_trial(always("A"), always("A"), rounds=8, seed=7)
        reactive = run_trial(avoid_last, always("A"), rounds=8, seed=7)
        self.assertEqual(24, sum(fixed.total_payoffs))
        self.assertEqual(45, sum(reactive.total_payoffs))
        self.assertEqual(1, reactive.collision_count)


    def test_equal_priority_after_even_number_of_collisions(self):
        trial = run_trial(always("A"), always("A"), rounds=6, seed=42)
        self.assertEqual((9, 9), trial.total_payoffs)
        self.assertEqual(6, trial.collision_count)
        self.assertEqual(0.5, trial.efficiency)
        order = [r.first_actor for r in trial.rounds]
        self.assertEqual({"Ada", "Ben"}, set(order))
        self.assertTrue(all(a != b for a, b in zip(order, order[1:])))

    def test_both_proposals_see_only_completed_rounds(self):
        observations = []

        def watch(observation):
            observations.append((observation.player, len(observation.completed)))
            return "A"

        run_trial(watch, watch, rounds=3, seed=2)
        self.assertEqual([("Ada", 0), ("Ben", 0), ("Ada", 1),
                          ("Ben", 1), ("Ada", 2), ("Ben", 2)], observations)


    def test_actor_or_site_injection_is_not_a_valid_commitment(self):
        for proposal in (None, True, "C", {"actor": "Ben", "site": "A"}):
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                run_trial(lambda observation: proposal, always("B"), rounds=1, seed=2)
        for rounds in (0, -1, True, 1001):
            with self.subTest(rounds=rounds), self.assertRaises(ValueError):
                run_trial(always("A"), always("B"), rounds=rounds, seed=1)

    def test_host_action_receipts_survive_reopen_without_double_harvest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.db"
            first = _play_round(path, 0, ("A", "B"), 0)
            after_restart = _play_round(path, 0, ("A", "B"), 0)
            self.assertEqual(first, after_restart)
            self.assertEqual((3, 3), after_restart.payoffs)
            with closing(sqlite3.connect(path)) as db:
                count = db.execute("SELECT COUNT(*) FROM events WHERE kind='gather'").fetchone()[0]
            self.assertEqual(2, count)

    def test_seeded_runs_have_identical_payoffs_and_resolution_order(self):
        a = run_trial(always("A"), avoid_last, rounds=4, seed=93)
        b = run_trial(always("A"), avoid_last, rounds=4, seed=93)
        self.assertEqual(a, b)


    def test_seeded_random_baseline_visits_both_sites(self):
        first = run_trial(seeded_random(11), always("A"), rounds=6, seed=3)
        second = run_trial(seeded_random(11), always("A"), rounds=6, seed=3)
        self.assertEqual(first, second)
        self.assertEqual({"A", "B"}, {r.choices[0] for r in first.rounds})
        self.assertGreater(sum(first.total_payoffs), 18)
        self.assertLess(sum(first.total_payoffs), 33)


    def test_conflicting_round_replay_cannot_partially_move_character(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conflict.db"
            _play_round(path, 0, ("A", "B"), 0)
            with closing(sqlite3.connect(path)) as db:
                before = db.execute("SELECT id,room_id FROM players ORDER BY id").fetchall()
                stock = db.execute("SELECT room_id,remaining FROM gathering_nodes ORDER BY room_id").fetchall()
            with self.assertRaisesRegex(ValueError, "different|conflict"):
                _play_round(path, 0, ("B", "A"), 0)
            with closing(sqlite3.connect(path)) as db:
                after = db.execute("SELECT id,room_id FROM players ORDER BY id").fetchall()
                current_stock = db.execute("SELECT room_id,remaining FROM gathering_nodes ORDER BY room_id").fetchall()
            self.assertEqual(before, after)
            self.assertEqual(stock, current_stock)


if __name__ == "__main__":
    unittest.main()
