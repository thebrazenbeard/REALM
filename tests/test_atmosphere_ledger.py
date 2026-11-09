"""Persistent synthetic compartment rules; no canonical ship, medical, or host authentication."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.atmosphere import AtmosphereModel
from realm.atmosphere_ledger import AtmosphereLedger, _rehydrate


def sample():
    return AtmosphereModel(
        compartments={
            "bridge": {"volume_l": 1000, "pressure_pa": 100000, "oxygen_ppm": 210000, "temperature_mk": 293150},
            "hold": {"volume_l": 1000, "pressure_pa": 20000, "oxygen_ppm": 100000, "temperature_mk": 293150},
        },
        hatches={"h1": {"a": "bridge", "b": "hold", "operators": ["pilot"], "open": False, "operational": True}},
        occupants={"crew": {"compartment": "hold", "sealed_suit": False},
                   "suited": {"compartment": "hold", "sealed_suit": True}},
    )


class AtmosphereLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "one.sqlite3"
        Engine(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO world_meta(key,value) VALUES('world_id','worldOne')")
        self.ledger = AtmosphereLedger(self.path)
        self.ledger.enable(world_id="worldOne", model=sample())

    def perform(self, action_id, op, params, version):
        return self.ledger.apply(world_id="worldOne", action_id=action_id,
                                 operation=op, params=params, expected_version=version)

    def test_persistent_hatch_tick_conserves_pressure_and_replays(self):
        a = self.ledger.status(world_id="worldOne")
        initial_gas = sum(c["gas_pa_l"] for c in a["snapshot"]["compartments"].values())
        initial_o2 = sum(c["oxygen_pa_l"] for c in a["snapshot"]["compartments"].values())
        opened = self.perform("open001", "hatch", {"hatch_id": "h1", "open": True, "actor": "pilot"}, 0)
        self.assertEqual(1, opened["version"])
        self.assertEqual(opened, self.perform("open001", "hatch", {"hatch_id": "h1", "open": True, "actor": "pilot"}, 0))
        with self.assertRaises(ValueError):
            self.perform("open001", "hatch", {"hatch_id": "h1", "open": False, "actor": "pilot"}, 0)
        tick = self.perform("tick001", "tick", {}, 1)
        self.assertEqual(2, tick["version"])
        persisted = AtmosphereLedger(self.path).status(world_id="worldOne")
        self.assertEqual(2, persisted["version"])
        rooms = persisted["snapshot"]["compartments"]
        self.assertLess(rooms["bridge"]["pressure_pa"], 100000)
        self.assertGreater(rooms["hold"]["pressure_pa"], 20000)
        self.assertEqual(initial_gas, sum(c["gas_pa_l"] for c in rooms.values()))
        self.assertEqual(initial_o2, sum(c["oxygen_pa_l"] for c in rooms.values()))

    def test_operator_and_world_scope_fail_closed(self):
        before = self.ledger.status(world_id="worldOne")
        with self.assertRaises(PermissionError):
            self.perform("bad001", "hatch", {"hatch_id": "h1", "open": True, "actor": "intruder"}, 0)
        with self.assertRaises(ValueError):
            self.ledger.apply(world_id="worldTwo", action_id="bad002", operation="tick", params={}, expected_version=0)
        with self.assertRaises(ValueError):
            self.perform("bad003", "hatch", {"hatch_id": "h1", "open": "yes", "actor": "pilot"}, 0)
        self.assertEqual(before, self.ledger.status(world_id="worldOne"))

    def test_breach_exposure_and_restart(self):
        self.perform("br001", "breach", {"chamber_id": "hold", "loss_pa_l_per_tick": 10000000}, 0)
        tick = self.perform("brtick", "tick", {}, 1)
        exposed = {row["actor"]: row for row in tick["result"]["exposures"]}
        self.assertTrue(exposed["crew"]["effective_low_pressure"])
        self.assertFalse(exposed["suited"]["effective_low_pressure"])
        self.assertNotIn("death", tick)
        self.assertEqual(2, AtmosphereLedger(self.path).status(world_id="worldOne")["version"])

    def test_stale_version_and_concurrent_hatch_race(self):
        replies = []
        lock = threading.Lock()
        def contender(action):
            try:
                r = self.perform(action, "hatch", {"hatch_id": "h1", "open": True, "actor": "pilot"}, 0)
                outcome = ("success", r["version"])
            except Exception as exc:
                outcome = (type(exc).__name__, str(exc))
            with lock:
                replies.append(outcome)
        ts = [threading.Thread(target=contender, args=(f"race{i}",)) for i in range(2)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(1, sum(x[0] == "success" for x in replies), replies)
        self.assertEqual(1, self.ledger.status(world_id="worldOne")["version"])

    def test_receipt_failure_rolls_back_state_and_caught_in_transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER atm_forbid BEFORE INSERT ON atm_receipts BEGIN SELECT RAISE(FAIL, 'block'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.perform("blocked", "hatch", {"hatch_id": "h1", "open": True, "actor": "pilot"}, 0)
        self.assertEqual(0, self.ledger.status(world_id="worldOne")["version"])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER atm_forbid")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE audit(marker TEXT)")
            db.execute("CREATE TEMP TRIGGER atm_late BEFORE INSERT ON main.atm_receipts BEGIN INSERT INTO audit(marker) VALUES('partial'); SELECT RAISE(FAIL, 'block'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                self.ledger.apply_in_transaction(db, world_id="worldOne", action_id="blocked2",
                    operation="hatch", params={"hatch_id": "h1", "open": True, "actor": "pilot"}, expected_version=0)
            db.commit()  # The outer host mistakenly swallows the failure.
        self.assertEqual(0, self.ledger.status(world_id="worldOne")["version"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([], db.execute("SELECT marker FROM audit").fetchall())

    def test_existing_ledger_and_tampered_state_fail_closed(self):
        with self.assertRaises(RuntimeError):
            self.ledger.enable(world_id="worldOne", model=sample())
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT state_json FROM atm_state WHERE id=1").fetchone()
            tampered = json.loads(row[0])
            tampered["compartments"]["hold"]["gas_pa_l"] = -1
            db.execute("UPDATE atm_state SET state_json=? WHERE id=1", (json.dumps(tampered),))
        with self.assertRaises(ValueError):
            self.ledger.status(world_id="worldOne")

    def test_max_length_compartment_id_can_be_rehydrated_with_breach(self):
        room = "r" * 64
        model = AtmosphereModel(
            compartments={room: {"volume_l": 1, "pressure_pa": 100000, "oxygen_ppm": 200000, "temperature_mk": 293150}},
            hatches={}, occupants={})
        model.breach(room, loss_pa_l_per_tick=1, action_id="original")
        restored = _rehydrate(json.loads(json.dumps(model.snapshot())))
        self.assertEqual(model.snapshot()["compartments"], restored.snapshot()["compartments"])
        self.assertEqual({room: 1}, restored.breaches)

    def test_different_physical_world_db_rejected(self):
        other = Path(self.tmp.name) / "other.sqlite3"
        Engine(other)
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("INSERT INTO world_meta(key,value) VALUES('world_id','worldOne')")
            db.commit()
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(RuntimeError):
                self.ledger.apply_in_transaction(db, world_id="worldOne", action_id="wrongDb",
                    operation="tick", params={}, expected_version=0)


    def test_mutated_invalid_initial_model_cannot_install_schema(self):
        other = Path(self.tmp.name) / "uninitialized.sqlite3"
        Engine(other)
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("INSERT INTO world_meta(key,value) VALUES('world_id','worldTwo')")
        altered = sample()
        altered.compartments["bridge"]["gas_pa_l"] = -1
        with self.assertRaises(ValueError):
            AtmosphereLedger(other).enable(world_id="worldTwo", model=altered)
        with closing(sqlite3.connect(other)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='atm_state'").fetchone())


    def test_rehydration_rejects_impossible_pressure_but_accepts_exact_upper_bound(self):
        max_pressure = 10_000_000
        exact = AtmosphereModel(compartments={
            "room": {"volume_l": 17, "pressure_pa": max_pressure,
                     "oxygen_ppm": 1_000_000, "temperature_mk": 2_000_000}},
            hatches={}, occupants={})
        original = json.loads(json.dumps(exact.snapshot()))
        self.assertEqual(original["compartments"], _rehydrate(original).snapshot()["compartments"])
        room = original["compartments"]["room"]
        room["gas_pa_l"] += 1
        room["oxygen_pa_l"] = 0
        room["oxygen_ppm"] = 0
        # The pressure floor hides a supermaximum one-unit gas inventory.
        with self.assertRaises(ValueError):
            _rehydrate(original)
        room["gas_pa_l"] = room["volume_l"] * (max_pressure + 1)
        room["pressure_pa"] = max_pressure + 1
        with self.assertRaises(ValueError):
            _rehydrate(original)

    def test_forged_persisted_superpressure_rejects_read_and_action_without_receipt(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            snapshot = json.loads(db.execute("SELECT state_json FROM atm_state WHERE id=1").fetchone()[0])
            c = snapshot["compartments"]["bridge"]
            c["gas_pa_l"] = c["volume_l"] * 10_000_001
            c["oxygen_pa_l"] = 0
            c["pressure_pa"] = 10_000_001
            c["oxygen_ppm"] = 0
            raw = json.dumps(snapshot, sort_keys=True)
            db.execute("UPDATE atm_state SET state_json=? WHERE id=1", (raw,))
        with self.assertRaises(ValueError):
            self.ledger.status(world_id="worldOne")
        with self.assertRaises(ValueError):
            self.perform("overmax", "tick", {}, 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual((0, raw), db.execute("SELECT version,state_json FROM atm_state WHERE id=1").fetchone())
            self.assertEqual(0, db.execute("SELECT count(*) FROM atm_receipts").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
