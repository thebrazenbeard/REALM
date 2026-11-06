"""No-model acceptance tests for host-owned deterministic simulation phase one."""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from realm.auth import CredentialStore
from realm.conversations import NPCIdentity, register_npc_pair
from realm.engine import Engine
from realm.gateway import Gateway
from realm.simulation import (
    LeaseHeld, LeaseLost, SimulationClock, SimulationIntegrityError,
)
from realm.telnet import LocalTelnetServer
from realm.worldpack import import_worldpack


FLAGSHIP = ROOT / "worlds" / "flagship" / "siltglass-reach" / "worldpack.v1.json"


class SimulationClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "world.sqlite3"
        import_worldpack(self.db_path, FLAGSHIP)
        self.engine = Engine(self.db_path)
        self.clock = SimulationClock(self.engine, expected_world_id="siltglass-reach")

    def _query(self, sql, args=()):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql, args).fetchall()

    def _count(self, table, kind=None):
        if kind:
            return self._query("SELECT COUNT(*) FROM events WHERE kind=?", (kind,))[0][0]
        return self._query(f"SELECT COUNT(*) FROM {table}")[0][0]

    def _ready(self, *, npc_count=2):
        self.clock.install()
        for name, curiosity, duty in (("FerryWatcher", 8, 3), ("LedgerKeeper", 2, 9))[:npc_count]:
            self.clock.register_npc(name, persona=f"A host-provisioned observer named {name}.",
                                    curiosity=curiosity, duty=duty)
        self.clock.acquire_lease()

    def test_opt_in_installation_does_not_mutate_wrong_world(self):
        self.assertFalse(self._query(
            "SELECT name FROM sqlite_master WHERE name='simulation_control'"
        ))
        mismatch = SimulationClock(self.engine, expected_world_id="other-world")
        with self.assertRaisesRegex(ValueError, "world_id"):
            mismatch.install()
        self.assertFalse(self._query(
            "SELECT name FROM sqlite_master WHERE name LIKE 'simulation_%'"
        ))
        first = self.clock.install()
        self.assertEqual(first, self.clock.install(), "Repeat install retains world instance identity")

    def test_registration_rejects_existing_player_credentials_and_invalid_drives(self):
        self.clock.install()
        self.engine.join("RealHuman")
        with self.assertRaisesRegex(ValueError, "existing human"):
            self.clock.register_npc("RealHuman", persona="Someone", curiosity=50, duty=50)
        CredentialStore(self.engine).provision("ReservedName")
        with self.assertRaisesRegex(ValueError, "credentialed player"):
            self.clock.register_npc("ReservedName", persona="Someone", curiosity=50, duty=50)
        for bad in (True, -1, 101, 1.5, "80"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.clock.register_npc("NewNPC", persona="Someone", curiosity=bad, duty=10)
        self.assertEqual(1, self._count("players"))
        self.assertEqual(0, self._count("simulation_drives"))

    def test_registered_npc_principal_cannot_be_claimed_by_cli_telnet_or_http(self):
        self._ready(npc_count=1)
        self.assertEqual(1, self._count("npc_actor_registry"))
        self.assertEqual(1, self._count("simulation_drives"))
        for name in ("FerryWatcher", "FERRYWATCHER", "ferrywatcher"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "NPC identity"):
                self.engine.join(name)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "NPC identity"):
                CredentialStore(self.engine).provision(name)
        telnet = LocalTelnetServer(self.engine, token="bounded-local-test-token", port=0)
        try:
            with self.assertRaisesRegex(ValueError, "NPC identity"):
                telnet.join_exclusive("fErRyWaTcHeR")
        finally:
            telnet.server_close()
        gateway = Gateway(self.db_path, world_id="siltglass-reach")
        with self.assertRaisesRegex(ValueError, "NPC identity"):
            gateway.join("FERRYWATCHER")
        self.assertEqual(0, self._count("action_receipts"))

    def test_ninth_npc_registration_fails_before_any_mutation(self):
        self.clock.install()
        for n in range(8):
            self.clock.register_npc(
                f"NPC{n}", persona=f"The {n}th deterministic resident.", curiosity=5, duty=5,
            )
        initial = (self._count("players"), self._count("simulation_drives"),
                   self._count("npc_actor_registry"), self._count("events", kind="join"))
        with self.assertRaisesRegex(ValueError, "resident cap"):
            self.clock.register_npc(
                "NPC8", persona="An extra NPC", curiosity=5, duty=5,
            )
        self.assertEqual((8, 8, 8, 8), initial)
        self.assertEqual(initial, (
            self._count("players"), self._count("simulation_drives"),
            self._count("npc_actor_registry"), self._count("events", kind="join"),
        ))
        self.clock.acquire_lease()
        self.assertEqual(1, self.clock.plan_next_tick())
        self.assertEqual(8, len(self.clock.apply_pending_tick()))

    def test_existing_npc_duet_registry_is_compatible_and_not_hijacked(self):
        register_npc_pair(
            self.engine,
            (NPCIdentity("DuetA", "The first speaker."),
             NPCIdentity("DuetB", "The second speaker.")),
            expected_world_id="siltglass-reach",
        )
        self.clock.install()
        self.clock.register_npc(
            "TickA", persona="A deterministic third speaker.", curiosity=6, duty=7,
        )
        self.assertEqual(3, self._count("npc_actor_registry"))
        self.assertEqual(1, self._count("simulation_drives"))
        for name in ("DuetA", "TickA"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "NPC identity"):
                self.engine.join(name)

    def test_plan_is_durable_and_effects_happen_only_after_apply(self):
        self._ready()
        tick = self.clock.plan_next_tick()
        self.assertEqual(1, tick)
        self.assertEqual(2, self._count("simulation_intents"))
        self.assertEqual(0, self._count("events", kind="say"))
        self.assertEqual(0, self._count("action_receipts"))
        self.assertEqual(1, self.clock.pending_tick())
        planned = self._query(
            "SELECT actor,command,status,proposal_sha256 FROM simulation_intents ORDER BY actor"
        )
        self.assertEqual(["FerryWatcher", "LedgerKeeper"], [row[0] for row in planned])
        self.assertTrue(all(row[2] == "proposed" and len(row[3]) == 64 for row in planned))
        outcomes = self.clock.apply_pending_tick()
        self.assertEqual(2, len(outcomes))
        self.assertTrue(all(o.status == "committed" for o in outcomes))
        self.assertEqual(2, self._count("events", kind="say"))
        self.assertEqual(2, self._count("action_receipts"))
        self.assertIsNone(self.clock.pending_tick())
        self.assertEqual((1, 1), self._query(
            "SELECT planned_through,finalized_through FROM simulation_control"
        )[0])
        self.assertEqual([(6, 4, 1), (3, 7, 1)], self._query(
            "SELECT curiosity,duty,revision FROM simulation_drives ORDER BY actor"
        ))

    def test_crash_after_engine_effect_before_receipt_rolls_back_entire_transaction(self):
        self._ready(npc_count=1)
        self.clock.plan_next_tick()
        original = self.engine._perform

        def fail_after_writing(*args):
            original(*args)
            raise RuntimeError("injected crash after event insert, before receipt")

        with patch.object(self.engine, "_perform", side_effect=fail_after_writing):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.clock.apply_pending_tick()
        self.assertEqual(0, self._count("events", kind="say"))
        self.assertEqual(0, self._count("action_receipts"))
        self.assertEqual("proposed", self._query("SELECT status FROM simulation_intents")[0][0])
        self.assertEqual(0, self._query("SELECT finalized_through FROM simulation_control")[0][0])
        self.assertEqual((8, 3, 0), self._query(
            "SELECT curiosity,duty,revision FROM simulation_drives"
        )[0])
        recovered = self.clock.apply_pending_tick()
        self.assertEqual(1, len(recovered))
        self.assertEqual("committed", recovered[0].status)
        self.assertEqual(1, self._count("events", kind="say"))

    def test_crash_after_durable_receipt_before_cursor_is_reconciled_exactly_once(self):
        self._ready(npc_count=1)
        self.clock.plan_next_tick()
        real_fence = self.clock._fence
        calls = 0

        def fail_before_finalization(db):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected crash after committed intent, before cursor")
            return real_fence(db)

        with patch.object(self.clock, "_fence", side_effect=fail_before_finalization):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.clock.apply_pending_tick()
        self.assertEqual(1, self._count("events", kind="say"))
        self.assertEqual(1, self._count("action_receipts"))
        self.assertEqual("committed", self._query("SELECT status FROM simulation_intents")[0][0])
        self.assertEqual((1, 0), self._query(
            "SELECT planned_through,finalized_through FROM simulation_control"
        )[0])
        self.assertEqual([], self.clock.apply_pending_tick())
        self.assertEqual((1, 1), self._query(
            "SELECT planned_through,finalized_through FROM simulation_control"
        )[0])
        self.assertEqual(1, self._count("events", kind="say"))
        self.assertEqual(1, self._count("action_receipts"))

    def test_lease_expiry_takeover_fences_stale_writer_inside_effect(self):
        self._ready(npc_count=1)
        tick = self.clock.plan_next_tick()
        competitor = SimulationClock(Engine(self.db_path), expected_world_id="siltglass-reach")
        with self.assertRaises(LeaseHeld):
            competitor.acquire_lease()
        # Deterministically simulate expiry; no clock sleeps or timing flake.
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE simulation_control SET lease_expires_unix=0")
        newer_epoch = competitor.acquire_lease()
        self.assertEqual(2, newer_epoch)
        with self.assertRaises(LeaseLost):
            self.clock.apply_pending_tick()
        self.assertEqual(0, self._count("events", kind="say"))
        self.assertEqual(0, self._count("action_receipts"))
        self.assertEqual(tick, competitor.pending_tick())
        outcomes = competitor.apply_pending_tick()
        self.assertEqual(1, len(outcomes))
        self.assertEqual(1, self._count("events", kind="say"))
        self.assertEqual(1, self._count("action_receipts"))

    def test_stale_movement_rejects_planned_proposal_without_speech(self):
        self._ready(npc_count=1)
        self.clock.plan_next_tick()
        self.assertIn("Lantern Jetty", self.engine.act("FerryWatcher", "east"))
        outcomes = self.clock.apply_pending_tick()
        self.assertEqual(["rejected"], [result.status for result in outcomes])
        self.assertEqual(0, self._count("events", kind="say"))
        self.assertEqual(0, self._count("action_receipts"))
        self.assertEqual((8, 3, 0), self._query(
            "SELECT curiosity,duty,revision FROM simulation_drives"
        )[0])
        self.assertEqual((1, 1), self._query(
            "SELECT planned_through,finalized_through FROM simulation_control"
        )[0])

    def test_state_version_or_persisted_proposal_tampering_fails_closed(self):
        self._ready(npc_count=1)
        self.clock.plan_next_tick()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE simulation_intents SET command='north'")
        with self.assertRaisesRegex(SimulationIntegrityError, "checksum"):
            self.clock.apply_pending_tick()
        self.assertEqual(0, self._count("action_receipts"))
        self.assertEqual(0, self._count("events", kind="say"))

    def test_restart_reuses_exact_proposal_without_replanning(self):
        self._ready(npc_count=1)
        self.clock.plan_next_tick()
        before = self._query(
            "SELECT action_id,command,proposal_sha256 FROM simulation_intents"
        )[0]
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE simulation_control SET lease_expires_unix=0")
        restarted = SimulationClock(Engine(self.db_path), expected_world_id="siltglass-reach")
        restarted.acquire_lease()
        with self.assertRaisesRegex(ValueError, "Finish"):
            restarted.plan_next_tick()
        recovered = restarted.apply_pending_tick()
        self.assertEqual(before[0], recovered[0].action_id)
        self.assertEqual(before, self._query(
            "SELECT action_id,command,proposal_sha256 FROM simulation_intents"
        )[0])
        self.assertEqual([], restarted.apply_pending_tick())
        self.assertEqual(1, self._count("events", kind="say"))

    def test_two_world_instances_with_same_worldpack_keep_distinct_ledgers(self):
        self._ready(npc_count=1)
        other_path = Path(self.temp.name) / "other.sqlite3"
        import_worldpack(other_path, FLAGSHIP)
        other = SimulationClock(Engine(other_path), expected_world_id="siltglass-reach")
        one_id = self._query("SELECT instance_id FROM simulation_control")[0][0]
        other_id = other.install()
        self.assertNotEqual(one_id, other_id)
        other.register_npc("FerryWatcher", persona="A ferry observer", curiosity=8, duty=3)
        other.acquire_lease()
        self.clock.plan_next_tick()
        self.clock.apply_pending_tick()
        with closing(sqlite3.connect(other_path)) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'"
            ).fetchone()[0])
        other.plan_next_tick()
        other.apply_pending_tick()
        with closing(sqlite3.connect(other_path)) as other_db:
            self.assertNotEqual(
                self._query("SELECT action_id FROM action_receipts")[0][0],
                other_db.execute("SELECT action_id FROM action_receipts").fetchone()[0],
            )

    def test_empty_tick_is_terminal_and_budget_stays_bounded(self):
        self.clock.install()
        self.clock.acquire_lease()
        self.assertEqual(1, self.clock.plan_next_tick())
        self.assertIsNone(self.clock.pending_tick())
        self.assertEqual([], self.clock.apply_pending_tick())
        self.assertEqual((1, 1), self._query(
            "SELECT planned_through,finalized_through FROM simulation_control"
        )[0])
        for bad in (0, 301, 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.clock.acquire_lease(ttl_seconds=bad)

    def test_concurrent_plan_and_apply_do_not_duplicate_intents_or_receipts(self):
        self._ready(npc_count=1)

        def plan_attempt(_):
            try:
                return self.clock.plan_next_tick()
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            planned = list(pool.map(plan_attempt, range(2)))
        self.assertEqual(1, planned.count(1))
        self.assertEqual(1, sum("Finish or reject" in str(x) for x in planned))
        self.assertEqual(1, self._count("simulation_intents"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            applied = list(pool.map(lambda _: self.clock.apply_pending_tick(), range(2)))
        self.assertEqual(1, sum(len(result) for result in applied))
        self.assertEqual(1, self._count("events", kind="say"))
        self.assertEqual(1, self._count("action_receipts"))
        self.assertEqual((1, 1), self._query(
            "SELECT planned_through,finalized_through FROM simulation_control"
        )[0])

    def test_concurrent_npc_reservation_and_credential_provision_cannot_both_win(self):
        self.clock.install()
        credentials = CredentialStore(self.engine)
        gate = threading.Barrier(2)

        def register():
            gate.wait(timeout=4)
            try:
                self.clock.register_npc(
                    "RaceActor", persona="An explicitly registered NPC.", curiosity=20, duty=30,
                )
                return "npc"
            except ValueError:
                return "rejected-npc"

        def provision():
            gate.wait(timeout=4)
            try:
                credentials.provision("RACEACTOR")
                return "credential"
            except ValueError:
                return "rejected-credential"

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = [pool.submit(register), pool.submit(provision)]
            outcomes = {future.result() for future in result}
        self.assertIn(outcomes, ({"npc", "rejected-credential"},
                                 {"credential", "rejected-npc"}))
        self.assertLessEqual(self._count("npc_actor_registry"), 1)
        self.assertLessEqual(self._count("player_credentials"), 1)
        self.assertEqual(1, self._count("npc_actor_registry") + self._count("player_credentials"))


if __name__ == "__main__":
    unittest.main()
