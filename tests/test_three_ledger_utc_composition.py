"""Synthetic NPC/atmosphere/ship UTC composition on one disposable SQLite world.

These tests qualify the shared transaction and both new host-tick custody
hooks, not privileged writes outside WorldClock or a deployed game service.
"""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.atmosphere import AtmosphereModel
from realm.atmosphere_clock import AtmosphereUTCHandler
from realm.atmosphere_ledger import AtmosphereLedger
from realm.geography import Geography
from realm.npc_roster_utc_policy import perform_host_roster_gather_in_transaction
from realm.world_clock import WorldClock

# Reuse the synthetic signed-roster fixture's valid host permit and proposal.
import test_npc_roster_utc_policy as roster_fixture

NOW = roster_fixture.NOW


class ThreeLedgerUtcCompositionTests(unittest.TestCase):
    def setUp(self):
        fx = roster_fixture.RosterUtcTests("test_three_actors_two_sites_wait_atomic_receipts")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        self.fx = fx
        self.clock = fx.clock
        self.policy, self.mac = fx.grant(fx.proposal(), event_id="a_roster")

        self.geo = Geography(fx.dbpath)
        self.geo.add_place("universe", "Universe", "universe")
        self.geo.add_place("system", "System", "system", "universe")
        self.geo.add_place("earth", "Earth", "planet", "system")
        self.geo.add_place("moon", "Moon", "planet", "system")
        self.geo.link("earth", "moon", "ship", seconds=50)
        self.geo.register_ship("voyager", "Voyager", "Captain", "earth")
        self.geo.depart("voyager", "moon", actor="Captain",
                        now=int((NOW - timedelta(seconds=50)).timestamp()),
                        action_id="synthetic_depart")
        self.clock.schedule("c_ship", due_at=NOW, kind="ship_arrival",
                            payload={"ship_id": "voyager"})

        self.ledger = AtmosphereLedger(fx.dbpath)
        model = AtmosphereModel(
            compartments={"atrium": {"volume_l": 1000, "pressure_pa": 100000,
                                     "oxygen_ppm": 210000, "temperature_mk": 293150}},
            hatches={}, occupants={},
        )
        self.ledger.enable(world_id="npc_roster", model=model)
        self.clock.schedule("b_atmosphere", due_at=NOW, kind="atmosphere_tick",
                            payload={"expected_version": 0})

    def handler(self):
        def delegated(db, event):
            if event.kind == "npc_roster_gather":
                return perform_host_roster_gather_in_transaction(
                    db, event, engine=self.fx.engine, crafting=self.fx.crafting,
                    policy=self.policy, host_key=roster_fixture.HOST_KEY,
                    grant_mac_hex=self.mac, now_utc=NOW,
                )
            if event.kind == "ship_arrival":
                return self.geo.arrive_in_transaction(
                    db, event.payload["ship_id"], now=int(event.due_at.timestamp()),
                    action_id=event.event_id,
                )
            if event.kind == "audit":
                db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                           "VALUES(?,?,?,?,?)", (event.due_at.isoformat(), "Captain",
                                                "atrium", "say", '{"text":"allowed"}'))
                return None
            raise ValueError("Unknown synthetic UTC event")
        return AtmosphereUTCHandler(
            self.ledger, world_id="npc_roster", other_handler=delegated,
        )

    def snapshot(self):
        with closing(sqlite3.connect(self.fx.dbpath)) as db:
            return (
                db.execute("SELECT version,state_json FROM atm_state").fetchall(),
                db.execute("SELECT * FROM atm_receipts ORDER BY action_id").fetchall(),
                db.execute("SELECT * FROM geo_ships ORDER BY id").fetchall(),
                db.execute("SELECT * FROM geo_ship_events ORDER BY id").fetchall(),
                db.execute("SELECT * FROM geo_ship_receipts ORDER BY action_id").fetchall(),
                db.execute("SELECT * FROM events ORDER BY id").fetchall(),
                db.execute("SELECT * FROM gathering_nodes ORDER BY material").fetchall(),
                db.execute("SELECT * FROM material_pouches ORDER BY actor,material").fetchall(),
                db.execute("SELECT * FROM clock_events ORDER BY event_id").fetchall(),
                db.execute("SELECT * FROM clock_state").fetchall(),
            )

    def sql(self, script):
        with closing(sqlite3.connect(self.fx.dbpath)) as db:
            db.executescript(script)

    def initial_tick(self):
        result = self.clock.tick(NOW, self.handler())
        self.assertEqual(["a_roster", "b_atmosphere", "c_ship"],
                         [event.event_id for event in result])
        self.assertEqual(2, self.fx.counts()[0])
        self.assertEqual("docked", self.geo.ship_status("voyager")["state"])
        self.assertEqual((1,), self._one("SELECT version FROM atm_state"))
        self.assertEqual((1,), self._one("SELECT COUNT(*) FROM atm_receipts"))
        self.assertEqual((2,), self._one("SELECT COUNT(*) FROM geo_ship_events"))

    def _one(self, sql):
        with closing(sqlite3.connect(self.fx.dbpath)) as db:
            return db.execute(sql).fetchone()

    def test_signed_roster_atmosphere_and_ship_arrive_once_in_same_tick(self):
        self.initial_tick()
        before = self.snapshot()
        self.assertEqual([], self.clock.tick(NOW, self.handler()))
        self.assertEqual(before, self.snapshot())

    def test_same_batch_late_trigger_rollback_all_three_effect_families(self):
        before = self.snapshot()
        self.sql("""CREATE TRIGGER main.tamper_triad_on_clock_state
            AFTER INSERT ON clock_state BEGIN
              DELETE FROM atm_receipts;
              DELETE FROM geo_ship_events WHERE kind='arrive';
              DELETE FROM events WHERE kind='gather';
            END;""")
        with self.assertRaises((ValueError, RuntimeError)):
            self.clock.tick(NOW, self.handler())
        self.assertEqual(before, self.snapshot())
        self.sql("DROP TRIGGER main.tamper_triad_on_clock_state;")
        self.initial_tick()

    def test_later_audit_ship_history_mutation_rejected_with_atmosphere_selected(self):
        self.initial_tick()
        later = NOW + timedelta(seconds=1)
        self.clock.schedule("z_audit", due_at=later, kind="audit", payload={})
        before = self.snapshot()
        self.sql("""CREATE TRIGGER main.tamper_ship_during_audit
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='z_audit' BEGIN
              UPDATE geo_ship_events SET actor='Forgery' WHERE kind='arrive';
            END;""")
        with self.assertRaisesRegex(ValueError, "Ship UTC"):
            self.clock.tick(later, self.handler())
        self.assertEqual(before, self.snapshot())
        self.sql("DROP TRIGGER main.tamper_ship_during_audit;")
        self.assertEqual(["z_audit"],
                         [e.event_id for e in self.clock.tick(later, self.handler())])
        self.assertEqual([], self.clock.tick(later, self.handler()))

    def test_later_audit_atmosphere_mutation_rejected_with_ship_guard_present(self):
        self.initial_tick()
        later = NOW + timedelta(seconds=1)
        self.clock.schedule("z_audit", due_at=later, kind="audit", payload={})
        before = self.snapshot()
        self.sql("""CREATE TRIGGER main.tamper_atmosphere_during_audit
            AFTER UPDATE OF processed_utc ON clock_events
            WHEN NEW.event_id='z_audit' BEGIN
              DELETE FROM atm_receipts WHERE action_id='b_atmosphere';
            END;""")
        with self.assertRaises((ValueError, RuntimeError)):
            self.clock.tick(later, self.handler())
        self.assertEqual(before, self.snapshot())
        self.sql("DROP TRIGGER main.tamper_atmosphere_during_audit;")
        self.assertEqual(["z_audit"],
                         [e.event_id for e in self.clock.tick(later, self.handler())])
        self.assertEqual(2, self.fx.counts()[0])

    def test_idle_tick_ship_and_atmosphere_mutation_roll_back(self):
        self.initial_tick()
        later = NOW + timedelta(seconds=1)
        before = self.snapshot()
        self.sql("""CREATE TRIGGER main.tamper_idle_ledgers
            AFTER UPDATE OF last_utc ON clock_state BEGIN
              DELETE FROM geo_ship_events WHERE kind='arrive';
              DELETE FROM atm_receipts WHERE action_id='b_atmosphere';
            END;""")
        with self.assertRaises((ValueError, RuntimeError)):
            self.clock.tick(later, self.handler())
        self.assertEqual(before, self.snapshot())
        self.sql("DROP TRIGGER main.tamper_idle_ledgers;")
        self.assertEqual([], self.clock.tick(later, self.handler()))
        self.assertEqual(2, self.fx.counts()[0])


if __name__ == "__main__":
    unittest.main()
