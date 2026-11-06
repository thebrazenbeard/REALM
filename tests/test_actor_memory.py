"""Adversarial, model-free evidence tests for host-issued NPC memory grants."""

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.actor_memory import ActorMemory
from realm.engine import Engine
from realm.gateway import Gateway


class ActorMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "alpha.sqlite3"
        self.gateway = Gateway(self.db, world_id="alpha")
        self.gateway.join("Moss")
        self.gateway.join("Ember")
        self.memory = ActorMemory(Engine(self.db), world_id="alpha")

    def _say(self, actor: str, message: str) -> int:
        return self.gateway.command(actor, "say " + message, f"say-{actor}-{message}")["cursor"]

    def _insert_host_event(self, kind: str, payload: dict, room: str = "atrium") -> int:
        with closing(self.gateway.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.gateway.engine._event(db, "Moss", room, kind, payload)
            return db.execute("SELECT MAX(id) FROM events").fetchone()[0]

    def test_host_grants_witnesses_latest_event_and_recall_is_speech_not_fact(self):
        event_id = self._say("Moss", "I defeated the king")
        first = self.memory.grant_latest_event("Ember", event_id)
        self.assertFalse(first["unchanged"])
        self.assertTrue(self.memory.grant_latest_event("Ember", event_id)["unchanged"])
        self.memory.grant_latest_event("Moss", event_id)
        seen = self.memory.recall("Ember")
        self.assertEqual(1, len(seen))
        self.assertEqual(event_id, seen[0]["source_event_id"])
        self.assertEqual("utterance_only_not_claim_truth", seen[0]["evidence_scope"])
        self.assertEqual("Moss said: I defeated the king", seen[0]["source_text"])
        self._say("Moss", "a later unrelated event")
        self.assertTrue(
            self.memory.grant_latest_event("Ember", event_id)["unchanged"],
            "a duplicate host-issued grant remains idempotent after newer events",
        )
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(2, db.execute(
                "SELECT COUNT(*) FROM actor_memory_grants WHERE event_id=?", (event_id,),
            ).fetchone()[0])

    def test_racing_duplicate_host_grants_serialize_without_extra_rows(self):
        event_id = self._say("Moss", "one shared observation")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: self.memory.grant_latest_event("Ember", event_id), range(8),
            ))
        self.assertEqual(1, sum(not item["unchanged"] for item in results))
        self.assertEqual(7, sum(item["unchanged"] for item in results))
        self.assertEqual(1, len(self.memory.recall("Ember")))

    def test_later_arrival_cannot_backfill_room_history(self):
        event_id = self._say("Moss", "secret before newcomer")
        self.gateway.join("Cedar")
        with self.assertRaisesRegex(ValueError, "retrospective"):
            self.memory.grant_latest_event("Cedar", event_id)
        self.assertEqual([], self.memory.recall("Cedar"))

    def test_newer_public_speech_blocks_retroactive_grant_even_if_always_copresent(self):
        old_event = self._say("Moss", "earlier public comment")
        self._say("Moss", "more recent public comment")
        with self.assertRaisesRegex(ValueError, "retrospective"):
            self.memory.grant_latest_event("Ember", old_event)
        with self.assertRaisesRegex(ValueError, "No active"):
            self.memory.add_summary("Ember", old_event, "I invented my own entitlement")
        self.assertEqual([], self.memory.recall("Ember"))

    def test_non_copresent_actor_cannot_receive_latest_room_speech(self):
        self.gateway.command("Ember", "north", "ember-north")
        event_id = self._say("Moss", "private to this room")
        with self.assertRaisesRegex(ValueError, "event room"):
            self.memory.grant_latest_event("Ember", event_id)
        self.assertEqual([], self.memory.recall("Ember"))

    def test_atomic_movement_pair_grants_departure_to_old_room_and_arrival_to_new(self):
        arrival_id = self.gateway.command("Moss", "north", "moss-north")["cursor"]
        departure_id = arrival_id - 1
        self.memory.grant_latest_event("Ember", departure_id)
        self.memory.grant_latest_event("Moss", arrival_id)
        self.assertEqual("leave", self.memory.recall("Ember")[0]["kind"])
        self.assertEqual("move", self.memory.recall("Moss")[0]["kind"])
        with self.assertRaisesRegex(ValueError, "event room"):
            self.memory.grant_latest_event("Ember", arrival_id)
        self.assertEqual(1, len(self.memory.recall("Ember")))

    def test_forged_private_whisper_and_extra_say_fields_are_never_grants(self):
        for kind, payload in (
            ("whisper", {"to": "Ember", "text": "this is private"}),
            ("say", {"text": "this is private", "private": True}),
        ):
            with self.subTest(kind=kind):
                event_id = self._insert_host_event(kind, payload)
                with self.assertRaisesRegex(ValueError, "(kind|payload)"):
                    self.memory.grant_latest_event("Ember", event_id)
        self.assertEqual([], self.memory.recall("Ember"))

    def test_known_resident_join_marker_is_public_but_arbitrary_marker_is_not(self):
        accepted_id = self._insert_host_event("join", {"resident": True})
        self.memory.grant_latest_event("Ember", accepted_id)
        self.assertEqual("join", self.memory.recall("Ember")[0]["kind"])
        rejected_id = self._insert_host_event("join", {"resident": "Moss", "secret": "other"})
        with self.assertRaisesRegex(ValueError, "payload"):
            self.memory.grant_latest_event("Ember", rejected_id)
        numeric_spoof = self._insert_host_event("join", {"resident": 1})
        with self.assertRaisesRegex(ValueError, "payload"):
            self.memory.grant_latest_event("Ember", numeric_spoof)

    def test_nonexistent_actor_or_event_and_invalid_receipt_are_denied(self):
        event_id = self._say("Moss", "present")
        with self.assertRaisesRegex(ValueError, "joined"):
            self.memory.grant_latest_event("NeverJoined", event_id)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.memory.grant_latest_event("Ember", event_id + 999)
        with self.assertRaises(ValueError):
            self.memory.grant_latest_event("Ember", True)
        with closing(self.gateway.engine._connect()) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM actor_memory_grants").fetchone()[0])

    def test_rolled_back_event_never_becomes_a_memory(self):
        with closing(self.gateway.engine._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.gateway.engine._event(db, "Moss", "atrium", "say", {"text": "uncommitted"})
            candidate = db.execute("SELECT MAX(id) FROM events").fetchone()[0]
            db.rollback()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.memory.grant_latest_event("Ember", candidate)
        self.assertEqual([], self.memory.recall("Ember"))

    def test_revocation_censors_source_and_derived_note_after_restart(self):
        event_id = self._say("Moss", "I own the moon")
        self.memory.grant_latest_event("Ember", event_id)
        self.memory.add_summary("Ember", event_id, "UNVERIFIED: Moss claims lunar ownership")
        seen = self.memory.recall("Ember")
        self.assertEqual("UNVERIFIED: Moss claims lunar ownership", seen[0]["derived_summary_unverified"])
        self.assertTrue(self.memory.revoke("Ember", event_id))
        self.assertFalse(self.memory.revoke("Ember", event_id))
        restarted = ActorMemory(Engine(self.db), world_id="alpha")
        self.assertEqual([], restarted.recall("Ember"))
        with self.assertRaisesRegex(ValueError, "Revoked"):
            restarted.grant_latest_event("Ember", event_id)
        with self.assertRaisesRegex(ValueError, "No active"):
            restarted.add_summary("Ember", event_id, "Attempted revival")

    def test_event_mutation_after_grant_fails_closed(self):
        event_id = self._say("Moss", "original statement")
        self.memory.grant_latest_event("Ember", event_id)
        with closing(self.gateway.engine._connect()) as db, db:
            db.execute("UPDATE events SET payload_json=? WHERE id=?", (
                json.dumps({"text": "rewritten statement"}), event_id,
            ))
        self.assertEqual([], self.memory.recall("Ember"))

    def test_world_identity_is_bound_to_database_and_grants_do_not_cross(self):
        another_db = Path(self.temp.name) / "beta.sqlite3"
        beta = Gateway(another_db, world_id="beta")
        beta.join("Moss")
        beta.join("Ember")
        beta_memory = ActorMemory(Engine(another_db), world_id="beta")
        alpha_event = self._say("Moss", "only in alpha")
        self.memory.grant_latest_event("Ember", alpha_event)
        self.assertEqual([], beta_memory.recall("Ember"))
        self.assertEqual("alpha", self.memory.recall("Ember")[0]["world_id"])
        with self.assertRaisesRegex(ValueError, "bound world_id"):
            ActorMemory(Engine(self.db), world_id="beta")
        with self.assertRaisesRegex(ValueError, "bound world_id"):
            ActorMemory(Engine(another_db), world_id="alpha")

    def test_memory_requires_binding_and_cannot_backfill_preexisting_history(self):
        unbound = Path(self.temp.name) / "unbound.sqlite3"
        engine = Engine(unbound)
        engine.join("OldActor")
        with self.assertRaisesRegex(ValueError, "bound world_id"):
            ActorMemory(engine, world_id="demo")
        with closing(engine._connect()) as db:
            self.assertFalse(db.execute(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='actor_memory_grants')"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
