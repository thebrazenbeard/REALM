"""Hostile synthetic network invariants; not canonical topology or physiology."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.atmosphere import AtmosphereModel


def room(pressure):
    return {"volume_l": 1000, "pressure_pa": pressure,
            "oxygen_ppm": 210_000, "temperature_mk": 293_150}


def edge(a, b):
    return {"a": a, "b": b, "operators": ["pilot"],
            "open": True, "operational": True}


class HostileNetworkTests(unittest.TestCase):
    def test_hatch_id_rename_does_not_change_physical_transfer(self):
        rooms = {"a": room(100_000), "b": room(50_000), "c": room(0)}
        first = AtmosphereModel(compartments=rooms,
                                hatches={"ab": edge("a", "b"), "bc": edge("b", "c")}, occupants={})
        renamed = AtmosphereModel(compartments=rooms,
                                  hatches={"za": edge("a", "b"), "aa": edge("b", "c")}, occupants={})
        first.tick(action_id="once")
        renamed.tick(action_id="once")
        self.assertEqual(first.snapshot()["compartments"], renamed.snapshot()["compartments"])

    def test_star_exchange_cannot_overdraw_center(self):
        rooms = {"hub": room(1_000_000)}
        rooms.update({f"v{i}": room(0) for i in range(12)})
        links = {f"h{i}": edge("hub", f"v{i}") for i in range(12)}
        model = AtmosphereModel(compartments=rooms, hatches=links, occupants={})
        before = model.snapshot()["compartments"]
        result = model.tick(action_id="star")
        after = model.snapshot()["compartments"]
        self.assertTrue(result["events"])
        self.assertEqual(sum(r["gas_pa_l"] for r in before.values()),
                         sum(r["gas_pa_l"] for r in after.values()))
        self.assertEqual(sum(r["oxygen_pa_l"] for r in before.values()),
                         sum(r["oxygen_pa_l"] for r in after.values()))
        for compartment in after.values():
            self.assertGreaterEqual(compartment["gas_pa_l"], 0)
            self.assertGreaterEqual(compartment["oxygen_pa_l"], 0)
            self.assertLessEqual(compartment["oxygen_pa_l"], compartment["gas_pa_l"])

    def test_many_inbound_edges_never_overpressurize(self):
        rooms = {"center": room(5_000_000)}
        rooms.update({f"v{i}": room(10_000_000) for i in range(12)})
        links = {f"h{i}": edge("center", f"v{i}") for i in range(12)}
        model = AtmosphereModel(compartments=rooms, hatches=links, occupants={})
        model.tick(action_id="inbound")
        self.assertTrue(all(c["pressure_pa"] <= 10_000_000
                            for c in model.snapshot()["compartments"].values()))

    def test_randomized_networks_preserve_both_species(self):
        import random
        rng = random.Random(83)
        for case in range(100):
            n = rng.randrange(3, 10)
            rooms = {f"r{i}": {
                "volume_l": rng.randrange(1, 10_001),
                "pressure_pa": rng.randrange(0, 10_000_001),
                "oxygen_ppm": rng.randrange(0, 1_000_001),
                "temperature_mk": 293_150,
            } for i in range(n)}
            pairs = [(a, b) for a in range(n) for b in range(a + 1, n)]
            rng.shuffle(pairs)
            links = {f"e{i}": edge(f"r{a}", f"r{b}")
                     for i, (a, b) in enumerate(pairs[:15])}
            model = AtmosphereModel(compartments=rooms, hatches=links, occupants={})
            for tick in range(4):
                before = model.snapshot()["compartments"]
                model.tick(action_id=f"{case}-{tick}")
                after = model.snapshot()["compartments"]
                for species in ("gas_pa_l", "oxygen_pa_l"):
                    self.assertEqual(sum(r[species] for r in before.values()),
                                     sum(r[species] for r in after.values()))
                self.assertTrue(all(0 <= r["oxygen_pa_l"] <= r["gas_pa_l"]
                                    <= 10_000_000 * r["volume_l"] for r in after.values()))

    def test_tick_boundary_and_replay_do_not_change_network_state(self):
        rooms = {"a": room(100_000), "b": room(50_000), "c": room(0)}
        edges = {"ab": edge("a", "b"), "bc": edge("b", "c")}
        continuous = AtmosphereModel(compartments=rooms, hatches=edges, occupants={})
        interrupted = AtmosphereModel(compartments=rooms, hatches=edges, occupants={})
        for tick in range(4):
            continuous.tick(action_id=f"t{tick}")
        for tick in range(2):
            interrupted.tick(action_id=f"t{tick}")
        midpoint = interrupted.snapshot()
        interrupted.tick(action_id="t1")  # Exact receipt replay must not step time.
        self.assertEqual(midpoint, interrupted.snapshot())
        for tick in range(2, 4):
            interrupted.tick(action_id=f"t{tick}")
        self.assertEqual(continuous.snapshot(), interrupted.snapshot())
