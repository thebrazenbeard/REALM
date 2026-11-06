"""Host-transaction regression for PR27+PR28 atomic NPC observations.

All tests use new disposable world databases, deterministic (non-model) speech
and no service/listener. Memory grants are not public history or human consent.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from realm.actor_memory import ActorMemory
from realm.engine import Engine
from realm.simulation import SimulationClock, SimulationIntegrityError
from realm.worldpack import import_worldpack


SHORE = ROOT / "examples" / "shore-world.json"


class AtomicNPCMemoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shore.sqlite3"
        import_worldpack(self.path, SHORE)
        self.engine = Engine(self.path)
        self.clock = SimulationClock(self.engine, expected_world_id="shore-example")
        self.clock.install()
        self.clock.register_npc("Moss", persona="A careful observer.", curiosity=70, duty=30)
        self.clock.register_npc("Ember", persona="A conscientious observer.", curiosity=20, duty=80)
        self.memory = ActorMemory(self.engine, world_id="shore-example")

    def _rows(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, params).fetchall()

    def _count(self, table, where=""):
        return self._rows(f"SELECT COUNT(*) FROM {table} {where}")[0][0]

    def _first_tick(self):
        self.clock.acquire_lease()
        self.clock.plan_next_tick()
        return self.clock.apply_pending_tick()

    def test_two_npcs_each_get_exact_two_committed_speech_observations(self):
        outcomes = self._first_tick()
        self.assertEqual(["committed", "committed"], [o.status for o in outcomes])
        event_ids = {row[0] for row in self._rows(
            "SELECT id FROM events WHERE kind='say' ORDER BY id"
        )}
        self.assertEqual(2, len(event_ids))
        self.assertEqual(4, self._count("actor_memory_grants"))
        for resident in ("Moss", "Ember"):
            records = self.memory.recall(resident)
            self.assertEqual(event_ids, {m["source_event_id"] for m in records})
            self.assertTrue(all(m["evidence_scope"] == "utterance_only_not_claim_truth" for m in records))
        restarted = ActorMemory(Engine(self.path), world_id="shore-example")
        self.assertEqual(event_ids, {m["source_event_id"] for m in restarted.recall("Moss")})

    def test_grant_failure_after_event_insert_rolls_back_every_effect(self):
        self.clock.acquire_lease()
        self.clock.plan_next_tick()
        original = ActorMemory._grant_new_speech_in_transaction
        calls = 0

        def fail_after_first_grant(db, **kw):
            nonlocal calls
            original(db, **kw)
            calls += 1
            if calls == 1:
                raise RuntimeError("injected crash after event and one memory grant")

        with patch.object(ActorMemory, "_grant_new_speech_in_transaction", side_effect=fail_after_first_grant):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.clock.apply_pending_tick()
        self.assertEqual(0, self._count("events", "WHERE kind='say'"))
        self.assertEqual(0, self._count("actor_memory_grants"))
        self.assertEqual(0, self._count("action_receipts"))
        self.assertEqual(0, self._count("simulation_intents", "WHERE status='committed'"))
        self.assertEqual(0, self._count("simulation_drives", "WHERE revision>0"))
        recovered = self.clock.apply_pending_tick()
        self.assertEqual(2, len(recovered))
        self.assertEqual(2, self._count("events", "WHERE kind='say'"))
        self.assertEqual(4, self._count("actor_memory_grants"))

    def test_revocation_and_summary_never_regranted_on_replay(self):
        self._first_tick()
        event_id = self._rows("SELECT MIN(id) FROM events WHERE kind='say'")[0][0]
        self.memory.add_summary("Moss", event_id, "Unverified impression about an old bell.")
        self.assertTrue(self.memory.revoke("Moss", event_id))
        self.assertNotIn(event_id, {m["source_event_id"] for m in self.memory.recall("Moss")})
        self.assertEqual([], self.clock.apply_pending_tick())
        restarted = ActorMemory(Engine(self.path), world_id="shore-example")
        self.assertNotIn(event_id, {m["source_event_id"] for m in restarted.recall("Moss")})
        with self.assertRaisesRegex(ValueError, "Revoked"):
            restarted.grant_latest_event("Moss", event_id)
        self.assertEqual(4, self._count("actor_memory_grants"))

    def test_out_of_room_npc_and_unconsented_human_get_no_auto_grants(self):
        self.engine.join("HumanObserver")  # Physical room position is not live consent.
        self.clock.acquire_lease()
        self.clock.plan_next_tick()
        self.assertIn("The Grove", self.engine.act("Moss", "north"))
        outcomes = self.clock.apply_pending_tick()
        self.assertEqual(["committed", "rejected"], [o.status for o in outcomes])
        event_ids = [row[0] for row in self._rows(
            "SELECT id FROM events WHERE kind='say' ORDER BY id"
        )]
        self.assertEqual(1, len(event_ids))
        self.assertEqual(set(), {m["source_event_id"] for m in self.memory.recall("Moss")})
        self.assertEqual(set(event_ids), {m["source_event_id"] for m in self.memory.recall("Ember")})
        self.assertEqual([], self.memory.recall("HumanObserver"))

    def test_missing_partial_memory_schema_fails_closed_with_no_speech(self):
        self.clock.acquire_lease()
        self.clock.plan_next_tick()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE actor_memory_summaries")
        with self.assertRaisesRegex(SimulationIntegrityError, "memory schema is incomplete"):
            self.clock.apply_pending_tick()
        self.assertEqual(0, self._count("events", "WHERE kind='say'"))
        self.assertEqual(0, self._count("action_receipts"))
        self.assertEqual(0, self._count("actor_memory_grants"))

    def test_enabled_memory_cannot_silently_downgrade_when_both_tables_missing(self):
        self.clock.acquire_lease()
        self.clock.plan_next_tick()
        self.assertEqual("1", self._rows(
            "SELECT value FROM world_meta WHERE key='actor_memory_enabled'"
        )[0][0])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE actor_memory_summaries")
            db.execute("DROP TABLE actor_memory_grants")
        with self.assertRaisesRegex(SimulationIntegrityError, "enabled but its schema is missing"):
            self.clock.apply_pending_tick()
        self.assertEqual(0, self._count("events", "WHERE kind='say'"))
        self.assertEqual(0, self._count("action_receipts"))

    def test_join_marker_matches_existing_actor_memory_public_shape(self):
        with closing(sqlite3.connect(self.path)) as db:
            joins = db.execute(
                "SELECT id,payload_json FROM events WHERE kind='join' ORDER BY id"
            ).fetchall()
        self.assertEqual(2, len(joins))
        self.assertTrue(all(json.loads(payload) == {"resident": True} for _, payload in joins))
        # Existing PR28 public API may grant the latest host-reviewed join.
        receipt = self.memory.grant_latest_event("Moss", joins[-1][0])
        self.assertFalse(receipt["unchanged"])

    def test_distinct_world_database_never_receives_foreign_grants(self):
        self._first_tick()
        other_path = Path(self.temp.name) / "another.sqlite3"
        import_worldpack(other_path, SHORE)
        other_engine = Engine(other_path)
        other_clock = SimulationClock(other_engine, expected_world_id="shore-example")
        other_clock.install()
        other_clock.register_npc("Moss", persona="A separate resident.", curiosity=10, duty=15)
        other_memory = ActorMemory(other_engine, world_id="shore-example")
        self.assertEqual([], other_memory.recall("Moss"))
        other_clock.acquire_lease()
        other_clock.plan_next_tick()
        other_clock.apply_pending_tick()
        with closing(sqlite3.connect(other_path)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM actor_memory_grants").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM events WHERE kind='say'").fetchone()[0])
        self.assertEqual(4, self._count("actor_memory_grants"))


if __name__ == "__main__":
    unittest.main()
