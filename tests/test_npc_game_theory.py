"""Synthetic, disposable SQLite evidence for read-only strategic NPC proposals."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from realm.crafting import Crafting
from realm.engine import Engine
from realm.npc_game_theory import ResourceSite, choose_economic_site
from realm.worldpack import import_worldpack


SITES = (ResourceSite("atrium", "stone"), ResourceSite("corridor", "wood"))
WORLD = {
    "schema": "realm.worldpack.v1",
    "world_id": "strategy_fixture",
    "name": "Synthetic Strategy World",
    "start_room": "atrium",
    "rooms": [
        {"id": "atrium", "name": "Atrium", "description": "A neutral room.",
         "exits": {"north": "corridor"}},
        {"id": "corridor", "name": "Corridor", "description": "A neutral room.",
         "exits": {"south": "atrium"}},
    ],
    "items": [],
}


class HostGameTheoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="realm-strategy-bridge-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "world.sqlite3"
        source = root / "worldpack.json"
        source.write_text(json.dumps(WORLD), encoding="utf-8")
        import_worldpack(self.db, source)
        self.engine = Engine(self.db)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.crafting = Crafting(self.db)
        self.assertTrue(self.crafting.place_node("atrium", "stone", stock=6, batch_yield=3))
        self.assertTrue(self.crafting.place_node("corridor", "wood", stock=9, batch_yield=3))

    def watermark(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT MAX(id) FROM events").fetchone()[0]

    def pouch(self, actor, material):
        with closing(sqlite3.connect(self.db)) as db:
            row = db.execute(
                "SELECT quantity FROM material_pouches WHERE actor=? AND material=?",
                (actor, material),
            ).fetchone()
            return 0 if row is None else row[0]

    def choose(self, *, cutoff=None, seed=1, world_id="strategy_fixture"):
        return choose_economic_site(
            self.db, expected_world_id=world_id,
            actor="Ada", opponent="Ben",
            sites=SITES, observed_through_event=self.watermark() if cutoff is None else cutoff,
            seed=seed,
        )

    def test_best_response_uses_real_prior_opponent_gathers_and_offers_no_authority(self):
        self.assertIn("gather stone", self.crafting.perform("Ben", "gather stone", action_id="ben-1"))
        self.assertIn("gather stone", self.crafting.perform("Ben", "gather stone", action_id="ben-2"))
        before_events = self.watermark()
        before_wood = self.pouch("Ada", "wood")
        choice = self.choose()
        self.assertEqual(choice.site, SITES[1])
        self.assertEqual(choice.world_id, "strategy_fixture")
        self.assertEqual(choice.actor, "Ada")
        self.assertEqual(choice.opponent, "Ben")
        self.assertEqual(choice.observed_through_event, before_events)
        self.assertEqual(choice.observations, 2)
        self.assertGreater(choice.forecast_numerator, 0)
        self.assertGreater(choice.forecast_denominator, 0)
        self.assertEqual(choice.status, "HOST_REVIEW_REQUIRED")
        self.assertEqual(self.watermark(), before_events)
        self.assertEqual(self.pouch("Ada", "wood"), before_wood)

    def test_cutoff_excludes_future_gathers_but_does_not_freeze_current_stock(self):
        prior = self.watermark()
        baseline = self.choose(cutoff=prior, seed=1)
        self.assertEqual(baseline.site, SITES[0])
        self.crafting.perform("Ben", "gather stone", action_id="ben-extra")
        held = self.choose(cutoff=prior, seed=1)
        updated = self.choose(seed=1)
        # Prior-history cutoff excludes Ben's new event, but current stock
        # still changed; this is NOT a replay-sealed proposal.
        self.assertEqual(held.observations, 0)
        self.assertEqual(updated.observations, 1)
        self.assertEqual(held.observed_through_event, prior)
        self.assertEqual(held.site, SITES[1])
        self.assertNotEqual(baseline.evidence_sha256, held.evidence_sha256)

    def test_host_can_apply_reviewed_choice_via_real_engine_and_crafting_receipts(self):
        self.crafting.perform("Ben", "gather stone", action_id="ben-observed")
        choice = self.choose()
        self.assertEqual(choice.site, SITES[1])
        self.assertIn("corridor", self.engine.act("Ada", "north", action_id="host-ada-move").lower())
        response = self.crafting.perform("Ada", "gather wood", action_id="host-ada-gather")
        self.assertIn("wood x3", response)
        self.assertEqual(self.pouch("Ada", "wood"), 3)
        self.assertEqual(self.crafting.perform("Ada", "gather wood", action_id="host-ada-gather"), response)
        self.assertEqual(self.pouch("Ada", "wood"), 3)

    def test_symmetric_two_actor_tie_does_not_self_coordinate_on_one_scarce_site(self):
        # Identical seed and tie: raw PRNG draws previously collided, leaving
        # half the harvest unused. Real receipts now witness both full batches.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gathering_nodes SET remaining=3")
        event_id = self.watermark()
        proposals = [
            choose_economic_site(
                self.db, expected_world_id="strategy_fixture",
                actor=actor, opponent=rival, sites=SITES,
                observed_through_event=event_id, seed=23,
            ) for actor, rival in (("Ada", "Ben"), ("Ben", "Ada"))
        ]
        self.assertEqual(proposals[0].forecast_numerator * proposals[1].forecast_denominator,
                         proposals[1].forecast_numerator * proposals[0].forecast_denominator)
        self.assertNotEqual(proposals[0].site, proposals[1].site)
        reordered = choose_economic_site(
            self.db, expected_world_id="strategy_fixture", actor="Ben", opponent="Ada",
            sites=tuple(reversed(SITES)), observed_through_event=event_id, seed=23,
        )
        self.assertEqual(reordered.site, proposals[1].site)
        for actor, choice in zip(("Ada", "Ben"), proposals):
            if choice.site.room_id == "corridor":
                self.engine.act(actor, "north", action_id=f"{actor}-move")
            response = self.crafting.perform(
                actor, f"gather {choice.site.material}", action_id=f"{actor}-gather",
            )
            self.assertIn("x3", response)
            self.assertEqual(self.pouch(actor, choice.site.material), 3)
        self.assertEqual(self.pouch("Ada", "stone") + self.pouch("Ada", "wood")
                         + self.pouch("Ben", "stone") + self.pouch("Ben", "wood"), 6)

    def test_strict_payoff_advantage_is_not_overridden_by_tie_coordination(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gathering_nodes SET remaining=1 WHERE material='wood'")
        offered = [
            choose_economic_site(
                self.db, expected_world_id="strategy_fixture", actor=a, opponent=b,
                sites=SITES, observed_through_event=self.watermark(), seed=23,
            ) for a, b in (("Ada", "Ben"), ("Ben", "Ada"))
        ]
        self.assertEqual([choice.site for choice in offered], [SITES[0], SITES[0]])

    def test_three_five_eight_actor_competition_has_bounded_real_payoffs(self):
        # Existing strategy is pairwise, not a global multi-agent auction.
        actors = ["Ada", "Ben"] + [f"Bot{i}" for i in range(6)]
        for actor in actors[2:]:
            self.engine.join(actor)
        for n in (3, 5, 8):
            plans = lambda: [choose_economic_site(
                self.db, expected_world_id="strategy_fixture", actor=actors[i],
                opponent=actors[(i + 1) % n], sites=SITES,
                observed_through_event=self.watermark(), seed=23,
            ).site for i in range(n)]
            first = plans()
            self.assertEqual(first, plans())
            # Two offered sites cannot assign eight distinct sites; explicitly
            # preserve the pairwise scope rather than imply Nash/equilibrium.
            self.assertGreaterEqual(n - len(set(first)), n - 2)
            if n == 8:
                payouts = []
                for actor, site in zip(actors, first):
                    if site.room_id == "corridor":
                        self.engine.act(actor, "north", action_id=f"{actor}-move-eight")
                    self.crafting.perform(actor, f"gather {site.material}",
                                          action_id=f"{actor}-eight-gather")
                    payouts.append(self.pouch(actor, site.material))
                self.assertLessEqual(sum(payouts), 15)  # 6 stone + 9 wood
                self.assertGreaterEqual(sum(payout == 0 for payout in payouts), 3)

    def test_abundant_stock_makes_a_colliding_second_gather_costless(self):
        # Host fixture stocks 9, Ben gathers 3, leaving 6; both actors can
        # obtain a full batch even if they collide on stone next.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gathering_nodes SET remaining=9 WHERE room_id='atrium' AND material='stone'")
        self.crafting.perform("Ben", "gather stone", action_id="ben-abundance")
        choice = self.choose(seed=1)
        self.assertEqual(choice.site, SITES[0])
        self.assertEqual(
            choice.forecast_numerator, 3 * choice.forecast_denominator
        )
        self.assertIn("stone x3", self.crafting.perform(
            "Ada", "gather stone", action_id="ada-same-site"
        ))
        self.assertEqual(self.pouch("Ada", "stone"), 3)

    def test_no_harvestable_resources_does_not_propose_a_fake_action(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gathering_nodes SET remaining=0")
        with self.assertRaises(ValueError):
            self.choose()

    def test_validation_refuses_world_swap_unbound_or_malformed_inputs(self):
        with self.assertRaises(ValueError):
            self.choose(world_id="another_world")
        with self.assertRaises(ValueError):
            choose_economic_site(
                self.db, expected_world_id="strategy_fixture", actor="Ada", opponent="Ada",
                sites=SITES, observed_through_event=self.watermark(), seed=1,
            )
        with self.assertRaises(ValueError):
            choose_economic_site(
                self.db, expected_world_id="strategy_fixture", actor="Ada", opponent="Ben",
                sites=(SITES[0], SITES[0]), observed_through_event=self.watermark(), seed=1,
            )
        with self.assertRaises(ValueError):
            self.choose(cutoff=True)
        with self.assertRaises(ValueError):
            self.choose(cutoff=10 ** 80)
        missing = Path(self.temp.name) / "never-created.sqlite3"
        with self.assertRaises((FileNotFoundError, ValueError, sqlite3.OperationalError)):
            choose_economic_site(
                missing, expected_world_id="strategy_fixture", actor="Ada", opponent="Ben",
                sites=SITES, observed_through_event=0, seed=1,
            )
        self.assertFalse(missing.exists())

    def test_real_resource_depletion_changes_expected_payoff(self):
        self.crafting.perform("Ben", "gather stone", action_id="drain-1")
        self.crafting.perform("Ben", "gather stone", action_id="drain-2")
        self.crafting.perform("Ben", "gather stone", action_id="drain-3")
        choice = self.choose()
        self.assertEqual(choice.site, SITES[1])
        self.assertEqual(choice.available_units, 3)

    def test_reviewed_choice_requires_same_transaction_and_fresh_state(self):
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            with self.assertRaises(ValueError):
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=choice, sites=SITES, seed=1,
                )
            db.execute("BEGIN IMMEDIATE")
            verified = npc_game_theory.verify_economic_choice_in_transaction(
                db, choice=choice, sites=SITES, seed=1,
            )
            self.assertEqual(verified.status, "VERIFIED_FOR_HOST_ACTION")
            self.assertEqual(verified.site, choice.site)
            self.assertEqual(verified.world_id, choice.world_id)
            db.rollback()

    def test_reviewed_choice_rejects_forgery_and_changed_world_state(self):
        from dataclasses import replace
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=replace(choice, site=SITES[1] if choice.site == SITES[0] else SITES[0]),
                    sites=SITES, seed=1,
                )
            db.rollback()
        self.crafting.perform("Ben", "gather stone", action_id="ben-changed-stock")
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=choice, sites=SITES, seed=1,
                )
            db.rollback()

    def test_temp_shadow_table_cannot_spoof_world_identity(self):
        from realm import npc_game_theory
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TEMP TABLE world_meta AS SELECT * FROM main.world_meta")
            db.execute("UPDATE temp.world_meta SET value='shadow_world' WHERE key='world_id'")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                forged = npc_game_theory.choose_economic_site(
                    self.db, expected_world_id="shadow_world",
                    actor="Ada", opponent="Ben", sites=SITES,
                    observed_through_event=self.watermark(), seed=1,
                    _connection=db,
                )
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=forged, sites=SITES, seed=1,
                )
            db.rollback()

    def test_revalidation_reads_uncommitted_stock_in_same_transaction(self):
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE main.gathering_nodes SET remaining=0 WHERE room_id='atrium'")
            with self.assertRaises(ValueError):
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=choice, sites=SITES, seed=1,
                )
            db.rollback()
        # The rejected speculative action never changed real stock.
        self.assertEqual(self.choose(seed=1), choice)

    def test_verified_status_is_not_reusable_as_fresh_review_authority(self):
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            verified = npc_game_theory.verify_economic_choice_in_transaction(
                db, choice=choice, sites=SITES, seed=1,
            )
            with self.assertRaises(ValueError):
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=verified, sites=SITES, seed=1,
                )
            db.rollback()

    def test_query_only_deferred_connection_cannot_validate_for_action(self):
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            with self.assertRaises(ValueError):
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=choice, sites=SITES, seed=1,
                )
            db.rollback()

    def test_deferred_transaction_must_hold_write_reservation_after_validation(self):
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            self.assertEqual(
                npc_game_theory.verify_economic_choice_in_transaction(
                    db, choice=choice, sites=SITES, seed=1,
                ).status, "VERIFIED_FOR_HOST_ACTION"
            )
            with closing(sqlite3.connect(self.db, timeout=0)) as competitor:
                with self.assertRaises(sqlite3.OperationalError):
                    competitor.execute("BEGIN IMMEDIATE")
            db.rollback()

    def test_competing_writer_reservation_fails_closed(self):
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db, timeout=0)) as competitor:
            competitor.execute("BEGIN IMMEDIATE")
            with closing(sqlite3.connect(self.db, timeout=0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN")
                with self.assertRaises(ValueError):
                    npc_game_theory.verify_economic_choice_in_transaction(
                        db, choice=choice, sites=SITES, seed=1,
                    )
                db.rollback()
            competitor.rollback()

    def test_write_reservation_probe_never_touches_world_rows_or_triggers(self):
        from realm import npc_game_theory
        choice = self.choose(seed=1)
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TEMP TRIGGER reject_meta_update AFTER UPDATE ON main.world_meta "
                       "BEGIN SELECT RAISE(FAIL, 'meta row unexpectedly touched'); END")
            db.execute("BEGIN")
            before_changes = db.total_changes
            verified = npc_game_theory.verify_economic_choice_in_transaction(
                db, choice=choice, sites=SITES, seed=1,
            )
            self.assertEqual(verified.status, "VERIFIED_FOR_HOST_ACTION")
            self.assertEqual(db.total_changes, before_changes)
            db.rollback()

    def test_host_roster_allocation_uses_full_capacity_without_oversubscription(self):
        from realm.npc_roster_allocation import propose_roster_allocation
        actors = ("Ada", "Ben") + tuple(f"Bot{i}" for i in range(6))
        for actor in actors[2:]:
            self.engine.join(actor)
        cutoff = self.watermark()
        proposal = propose_roster_allocation(
            self.db, expected_world_id="strategy_fixture",
            actors=actors, sites=SITES, observed_through_event=cutoff, seed=23,
        )
        self.assertEqual(proposal.status, "HOST_REVIEW_REQUIRED")
        self.assertEqual(proposal.total_expected_units, 15)
        self.assertEqual(sum(a.expected_units for a in proposal.assignments), 15)
        self.assertEqual(sum(a.site == SITES[0] for a in proposal.assignments), 2)
        self.assertEqual(sum(a.site == SITES[1] for a in proposal.assignments), 3)
        self.assertEqual(sum(a.site is None for a in proposal.assignments), 3)
        self.assertEqual(self.watermark(), cutoff)
        reordered = propose_roster_allocation(
            self.db, expected_world_id="strategy_fixture",
            actors=tuple(reversed(actors)), sites=tuple(reversed(SITES)),
            observed_through_event=cutoff, seed=23,
        )
        self.assertEqual(proposal, reordered)

    def test_host_site_utility_dominance_survives_capacity_allocation(self):
        from realm.npc_roster_allocation import propose_roster_allocation
        high_value = {SITES[0]: 10, SITES[1]: 1}
        choice = propose_roster_allocation(
            self.db, expected_world_id="strategy_fixture", actors=("Ada", "Ben"),
            sites=SITES, observed_through_event=self.watermark(), seed=23,
            site_values=high_value,
        )
        self.assertEqual([a.site for a in choice.assignments], [SITES[0], SITES[0]])
        self.assertEqual(choice.total_expected_units, 6)
        self.assertEqual(choice.total_expected_utility, 60)
        with self.assertRaises(ValueError):
            propose_roster_allocation(
                self.db, expected_world_id="strategy_fixture",
                actors=("Ada", "Ben"), sites=SITES,
                observed_through_event=self.watermark(), seed=23,
                site_values={SITES[0]: True, SITES[1]: 1},
            )

    def test_multi_actor_full_roster_is_capacity_bound_across_seeds(self):
        from realm.npc_roster_allocation import propose_roster_allocation
        actors = ("Ada", "Ben") + tuple(f"Bot{i}" for i in range(6))
        for actor in actors[2:]:
            self.engine.join(actor)
        for n in (3, 5, 8):
            for seed in range(12):
                result = propose_roster_allocation(
                    self.db, expected_world_id="strategy_fixture",
                    actors=actors[:n], sites=SITES,
                    observed_through_event=self.watermark(), seed=seed,
                )
                self.assertEqual(result.total_expected_units, min(n * 3, 15))
                self.assertLessEqual(sum(a.site == SITES[0] for a in result.assignments), 2)
                self.assertLessEqual(sum(a.site == SITES[1] for a in result.assignments), 3)
                self.assertEqual(sum(a.site is None for a in result.assignments),
                                 max(0, n - 5))

    def test_host_actions_following_full_roster_proposal_create_real_idempotent_receipts(self):
        from realm.npc_roster_allocation import propose_roster_allocation
        actors = ("Ada", "Ben") + tuple(f"Bot{i}" for i in range(6))
        for actor in actors[2:]:
            self.engine.join(actor)
        cutoff = self.watermark()
        result = propose_roster_allocation(
            self.db, expected_world_id="strategy_fixture",
            actors=actors, sites=SITES, observed_through_event=cutoff, seed=23,
        )
        self.assertEqual(result.status, "HOST_REVIEW_REQUIRED")
        for row in result.assignments:
            if row.site is None:
                self.assertEqual(row.expected_units, 0)
                continue
            if row.site.room_id == "corridor":
                self.engine.act(row.actor, "north", action_id=f"host-{row.actor}-move")
            action = f"gather {row.site.material}"
            action_id = f"host-{row.actor}-gather"
            response = self.crafting.perform(row.actor, action, action_id=action_id)
            self.assertIn("x3", response)
            self.assertEqual(self.crafting.perform(row.actor, action, action_id=action_id),
                             response)
            self.assertEqual(self.pouch(row.actor, row.site.material), 3)
        with closing(sqlite3.connect(self.db)) as db:
            gathered = db.execute("SELECT COUNT(*) FROM events WHERE kind='gather'").fetchone()[0]
        self.assertEqual(gathered, 5)
        self.assertNotEqual(self.watermark(), cutoff)
        with self.assertRaises(ValueError):
            propose_roster_allocation(self.db, expected_world_id="strategy_fixture",
                    actors=actors, sites=SITES,
                    observed_through_event=self.watermark(), seed=23)

    def test_full_roster_rejects_unjoined_duplicate_world_swap_and_forged_values(self):
        from realm.npc_roster_allocation import propose_roster_allocation
        args = dict(expected_world_id="strategy_fixture", actors=("Ada", "Ben"),
                    sites=SITES, observed_through_event=self.watermark(), seed=23)
        for bad in (("Ada", "ada"), ("Ada", "Absent")):
            with self.assertRaises(ValueError):
                propose_roster_allocation(self.db, **(args | {"actors": bad}))
        with self.assertRaises(ValueError):
            propose_roster_allocation(self.db, **(args | {"expected_world_id": "fake"}))
        for bad in ({SITES[0]: 10}, {SITES[0]: 10, SITES[1]: 0},
                    {SITES[0]: 10, SITES[1]: 1.5}):
            with self.assertRaises(ValueError):
                propose_roster_allocation(self.db, **args, site_values=bad)
        with self.assertRaises(ValueError):
            propose_roster_allocation(self.db, **(args | {"observed_through_event": 10**50}))
        with self.assertRaises(ValueError):
            propose_roster_allocation(
                self.db, **(args | {"sites": (ResourceSite(1, "stone"), SITES[1])})
            )


if __name__ == "__main__":
    unittest.main()
