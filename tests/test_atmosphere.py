"""Deterministic, synthetic atmosphere-state tests. No world or canon access."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.atmosphere import AtmosphereModel


def sandbox():
    return AtmosphereModel(
        compartments={
            "bridge": {"volume_l": 1000, "pressure_pa": 100_000, "oxygen_ppm": 210_000, "temperature_mk": 293_150},
            "hold": {"volume_l": 1000, "pressure_pa": 20_000, "oxygen_ppm": 100_000, "temperature_mk": 293_150},
        },
        hatches={"h1": {"a": "bridge", "b": "hold", "operators": ["pilot"], "open": False, "operational": True}},
        occupants={"crew": {"compartment": "hold", "sealed_suit": False},
                   "suited": {"compartment": "hold", "sealed_suit": True}},
    )


class AtmosphereTests(unittest.TestCase):
    def test_closed_hatch_isolates_then_open_hatch_conserves_inventory(self):
        env = sandbox()
        initial = env.snapshot()
        before = sum(c["gas_pa_l"] for c in initial["compartments"].values())
        before_o2 = sum(c["oxygen_pa_l"] for c in initial["compartments"].values())
        env.tick(action_id="tick-closed")
        self.assertEqual(initial["compartments"], env.snapshot()["compartments"])

        env.hatch("h1", open=True, actor="pilot", action_id="open-1")
        self.assertTrue(env.snapshot()["hatches"]["h1"]["open"])
        env.tick(action_id="tick-open")
        changed = env.snapshot()["compartments"]
        self.assertLess(changed["bridge"]["pressure_pa"], 100_000)
        self.assertGreater(changed["hold"]["pressure_pa"], 20_000)
        self.assertEqual(before, sum(c["gas_pa_l"] for c in changed.values()))
        self.assertEqual(before_o2, sum(c["oxygen_pa_l"] for c in changed.values()))

    def test_hatch_requires_operator_and_operational_state(self):
        env = sandbox()
        with self.assertRaises(PermissionError):
            env.hatch("h1", open=True, actor="stranger", action_id="bad")
        with self.assertRaises(ValueError):
            env.hatch("absent", open=True, actor="pilot", action_id="missing")
        self.assertFalse(env.snapshot()["hatches"]["h1"]["open"])

        disabled = sandbox()
        disabled.hatches["h1"]["operational"] = False
        with self.assertRaises(ValueError):
            disabled.hatch("h1", open=True, actor="pilot", action_id="disabled")

    def test_breach_creates_exposure_signal_without_mortality(self):
        env = sandbox()
        env.breach("hold", loss_pa_l_per_tick=10_000_000, action_id="breach-1")
        before = env.snapshot()["compartments"]["hold"]["gas_pa_l"]
        result = env.tick(action_id="tick-breach")
        self.assertEqual(before - 10_000_000, env.snapshot()["compartments"]["hold"]["gas_pa_l"])
        exposed = {e["actor"]: e for e in result["exposures"]}
        self.assertTrue(exposed["crew"]["low_pressure"])
        self.assertTrue(exposed["crew"]["effective_low_pressure"])
        self.assertTrue(exposed["suited"]["low_pressure"])
        self.assertFalse(exposed["suited"]["effective_low_pressure"])
        self.assertNotIn("death", result)

    def test_exact_action_replay_and_conflicting_action_id(self):
        env = sandbox()
        first = env.hatch("h1", open=True, actor="pilot", action_id="open-1")
        self.assertEqual(first, env.hatch("h1", open=True, actor="pilot", action_id="open-1"))
        with self.assertRaisesRegex(ValueError, "different"):
            env.hatch("h1", open=False, actor="pilot", action_id="open-1")
        first_tick = env.tick(action_id="step-1")
        snap = env.snapshot()
        self.assertEqual(first_tick, env.tick(action_id="step-1"))
        self.assertEqual(snap, env.snapshot())
        with self.assertRaisesRegex(ValueError, "different"):
            env.breach("hold", loss_pa_l_per_tick=1000, action_id="step-1")

    def test_invalid_unit_boundaries_fail_closed(self):
        bad = sandbox()
        with self.assertRaises(ValueError):
            bad.breach("hold", loss_pa_l_per_tick=-1, action_id="negative")
        bad_spec = {
            "c": {"volume_l": 0, "pressure_pa": 10, "oxygen_ppm": 100_000, "temperature_mk": 293_150}
        }
        with self.assertRaises(ValueError):
            AtmosphereModel(compartments=bad_spec, hatches={}, occupants={})

    def test_tick_with_invalid_open_hatch_does_not_partially_equalize(self):
        spec = {"volume_l": 1000, "pressure_pa": 100_000, "oxygen_ppm": 210_000,
                "temperature_mk": 293_150}
        env = AtmosphereModel(
            compartments={
                "one": dict(spec),
                "two": {**spec, "pressure_pa": 20_000},
                "three": {**spec, "temperature_mk": 294_150},
            },
            hatches={
                "a": {"a": "one", "b": "two", "operators": ["pilot"], "open": True, "operational": True},
                "z": {"a": "one", "b": "three", "operators": ["pilot"], "open": True, "operational": True},
            }, occupants={})
        before = env.snapshot()
        with self.assertRaisesRegex(ValueError, "Isothermal"):
            env.tick(action_id="invalid-mix")
        self.assertEqual(before, env.snapshot())

    def test_tiny_pressure_quantization_cannot_ping_pong_forever(self):
        sample = {"volume_l": 1, "pressure_pa": 1, "oxygen_ppm": 1_000_000,
                  "temperature_mk": 293_150}
        env = AtmosphereModel(
            compartments={"a": sample, "b": {**sample, "pressure_pa": 0}},
            hatches={"edge": {"a": "a", "b": "b", "operators": ["pilot"],
                              "open": True, "operational": True}},
            occupants={})
        expected = env.snapshot()["compartments"]
        for step in range(6):
            env.tick(action_id=f"quantized-{step}")
            self.assertEqual(expected, env.snapshot()["compartments"],
                             "Sub-quantum transfer must not oscillate between chambers")

    def test_host_mutated_breach_cannot_partially_commit_tick(self):
        env = sandbox()
        env.hatch("h1", open=True, actor="pilot", action_id="open")
        # Host integration must not hand out mutable internals; even if it
        # does, a bad compartment reference cannot half-apply a tick.
        env.breaches["missing"] = 10_000
        before = env.snapshot()
        with self.assertRaises(KeyError):
            env.tick(action_id="corrupted")
        self.assertEqual(before, env.snapshot())

    def test_world_models_do_not_share_mutation(self):
        a, b = sandbox(), sandbox()
        a.breach("hold", loss_pa_l_per_tick=10_000_000, action_id="impact")
        a.tick(action_id="t")
        self.assertEqual(20_000, b.snapshot()["compartments"]["hold"]["pressure_pa"])
        self.assertNotEqual(a.snapshot()["compartments"], b.snapshot()["compartments"])


if __name__ == "__main__":
    unittest.main()
