"""Deterministic host-side mortality policy; no live world integration."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.mortality import MortalityPolicy, HealthState, injure, intervene, tick


START = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)


class MortalityRulesTests(unittest.TestCase):
    def policy(self, mode="hardcore"):
        return MortalityPolicy(
            mode=mode,
            policy_id="rules-v1",
            windows_seconds={
                ("abdominal_gunshot", "critical"): 3600,
                ("abdominal_stab", "critical"): 86400,
                ("cardiac_arrest", "critical"): 300,
                ("catastrophic_decompression", "lethal"): 0,
            },
        )
    def injury(self, mode="hardcore", cause="abdominal_gunshot", severity="critical"):
        return injure(
            world_id="isolated-world", character_id="pilot-1",
            cause=cause, severity=severity, occurred_at=START,
            policy=self.policy(mode),
        )

    def test_hardcore_critical_injury_has_server_utc_deadline(self):
        injury = self.injury()
        self.assertEqual(HealthState.CRITICAL, injury.state)
        self.assertEqual(START + timedelta(hours=1), injury.deadline_utc)
        self.assertEqual("hardcore", injury.mode)
        self.assertEqual("rules-v1", injury.policy_id)
        self.assertEqual("isolated-world", injury.world_id)

    def test_treatment_before_deadline_stabilizes_and_retry_is_idempotent(self):
        injury = self.injury()
        healed = intervene(injury, at=START + timedelta(minutes=59),
                           action_id="act-1", actor_id="medic-a", resource_id="medkit-1",
                           authorized=True, nearby=True, supply_verified=True)
        self.assertEqual(HealthState.STABILIZED, healed.state)
        self.assertEqual(healed, intervene(healed, at=START + timedelta(hours=2),
                          action_id="act-1", actor_id="medic-a", resource_id="medkit-1",
                          authorized=True, nearby=True, supply_verified=True))
        self.assertEqual(healed, tick(healed, START + timedelta(days=3)))
    def test_treatment_at_or_after_deadline_is_rejected(self):
        injury = self.injury()
        for instant in (START + timedelta(hours=1), START + timedelta(hours=2)):
            with self.subTest(instant=instant):
                with self.assertRaisesRegex(ValueError, "deadline"):
                    intervene(injury, at=instant, action_id="act-2",
                              actor_id="medic-a", resource_id="medkit-2",
                              authorized=True, nearby=True, supply_verified=True)
        self.assertEqual(HealthState.DEATH_CANDIDATE, tick(injury, START + timedelta(hours=1)).state)

    def test_casual_critical_expires_into_respawn_pending(self):
        casualty = self.injury(mode="casual")
        self.assertEqual(HealthState.RESPAWN_PENDING,
                         tick(casualty, START + timedelta(hours=1)).state)
        self.assertNotEqual(HealthState.DEATH_CANDIDATE, tick(casualty, START + timedelta(days=1)).state)

    def test_instant_fatal_exposure_is_opt_in_game_rule(self):
        injury = self.injury(cause="catastrophic_decompression", severity="lethal")
        self.assertEqual(HealthState.DEATH_CANDIDATE, injury.state)
        self.assertEqual(START, injury.deadline_utc)
        with self.assertRaisesRegex(ValueError, "terminal"):
            intervene(injury, at=START, action_id="a", actor_id="m", resource_id="r",
                      authorized=True, nearby=True, supply_verified=True)
    def test_medical_intervention_requires_all_host_verified_conditions(self):
        injury = self.injury()
        for field in ("authorized", "nearby", "supply_verified"):
            inputs = dict(authorized=True, nearby=True, supply_verified=True)
            inputs[field] = False
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unauthorized"):
                intervene(injury, at=START + timedelta(seconds=1),
                          action_id="rescue", actor_id="medic", resource_id="kit", **inputs)
        self.assertEqual(HealthState.CRITICAL, injury.state)

    def test_replay_id_with_different_medical_actor_or_resource_is_rejected(self):
        injury = self.injury()
        stabilized = intervene(injury, at=START + timedelta(minutes=1), action_id="rescue",
                               actor_id="medic-a", resource_id="kit-a",
                               authorized=True, nearby=True, supply_verified=True)
        for actor, kit in (("medic-b", "kit-a"), ("medic-a", "kit-b")):
            with self.subTest(actor=actor, kit=kit), self.assertRaisesRegex(ValueError, "collision"):
                intervene(stabilized, at=START + timedelta(minutes=2), action_id="rescue",
                          actor_id=actor, resource_id=kit,
                          authorized=True, nearby=True, supply_verified=True)
    def test_naive_time_rejected_and_policy_cannot_change_mid_injury(self):
        with self.assertRaisesRegex(ValueError, "UTC"):
            injure(world_id="one", character_id="a", cause="cardiac_arrest",
                   severity="critical", occurred_at=START.replace(tzinfo=None),
                   policy=self.policy())
        injury = self.injury()
        with self.assertRaisesRegex(ValueError, "UTC"):
            tick(injury, START.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "invalid"):
            MortalityPolicy(mode="permadeath+cheats", policy_id="bad", windows_seconds={})
        with self.assertRaisesRegex(ValueError, "window"):
            MortalityPolicy(mode="hardcore", policy_id="bad", windows_seconds={("x", "y"): -1})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            injure(world_id="one", character_id="a", cause="unknown", severity="critical",
                   occurred_at=START, policy=self.policy())

    def test_uncertain_clock_quarantines_terminal_outcome(self):
        injury = self.injury()
        pending = tick(injury, START + timedelta(hours=1), uncertain_clock=True)
        self.assertEqual(HealthState.PENDING_REVIEW, pending.state)
        self.assertEqual(pending, tick(pending, START + timedelta(days=1)))
        with self.assertRaisesRegex(ValueError, "pending review"):
            intervene(pending, at=START + timedelta(minutes=30),
                      action_id="late", actor_id="medic", resource_id="kit",
                      authorized=True, nearby=True, supply_verified=True)

    def test_policy_windows_are_snapshotted_not_mutable(self):
        author_windows = {("cardiac_arrest", "critical"): 300}
        policy = MortalityPolicy(mode="hardcore", policy_id="v1",
                                 windows_seconds=author_windows)
        author_windows[("cardiac_arrest", "critical")] = 99999
        injury = injure(world_id="one", character_id="two",
                        cause="cardiac_arrest", severity="critical",
                        occurred_at=START, policy=policy)
        self.assertEqual(START + timedelta(minutes=5), injury.deadline_utc)
        with self.assertRaises(TypeError):
            policy.windows_seconds[("cardiac_arrest", "critical")] = 99999

    def test_host_observation_prevents_backdated_treatment(self):
        injury = self.injury()
        advanced = tick(injury, START + timedelta(minutes=50))
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            intervene(advanced, at=START + timedelta(minutes=40),
                      action_id="retro", actor_id="medic", resource_id="kit",
                      authorized=True, nearby=True, supply_verified=True)
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            tick(advanced, START + timedelta(minutes=49))

    def test_absurd_window_is_rejected_before_datetime_overflow(self):
        with self.assertRaisesRegex(ValueError, "window"):
            MortalityPolicy(mode="hardcore", policy_id="v1",
                            windows_seconds={("x", "critical"): 10**100})
        near_max = datetime.max.replace(tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "window"):
            injure(world_id="w", character_id="c", cause="abdominal_gunshot",
                   severity="critical", occurred_at=near_max, policy=self.policy())

    def test_hardcore_unwitnessed_outcomes_never_claim_final_death(self):
        ordinary = tick(self.injury(), START + timedelta(hours=1))
        immediate = self.injury(cause="catastrophic_decompression", severity="lethal")
        self.assertNotEqual(HealthState.DEAD_FINAL, ordinary.state)
        self.assertNotEqual(HealthState.DEAD_FINAL, immediate.state)

    def test_policy_identity_binds_actual_content_hash(self):
        a = MortalityPolicy(mode="hardcore", policy_id="v1",
                            windows_seconds={("x", "critical"): 300})
        b = MortalityPolicy(mode="hardcore", policy_id="v1",
                            windows_seconds={("x", "critical"): 3600})
        self.assertNotEqual(getattr(a, "policy_hash", None),
                            getattr(b, "policy_hash", None))
        injury = injure(world_id="w", character_id="c", cause="x",
                        severity="critical", occurred_at=START, policy=a)
        self.assertEqual(a.policy_hash, injury.policy_hash)

    def test_authenticated_retry_survives_consumed_resources(self):
        injury = self.injury()
        healed = intervene(injury, at=START + timedelta(seconds=1),
                           action_id="a", actor_id="medic", resource_id="kit",
                           authorized=True, nearby=True, supply_verified=True)
        self.assertEqual(healed, intervene(healed, at=START + timedelta(hours=2),
                         action_id="a", actor_id="medic", resource_id="kit",
                         authorized=True, nearby=False, supply_verified=False))
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            intervene(healed, at=START + timedelta(hours=2), action_id="a",
                      actor_id="medic", resource_id="kit",
                      authorized=False, nearby=False, supply_verified=False)


if __name__ == "__main__":
    unittest.main()
