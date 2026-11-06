"""Future integration acceptance test for the deterministic NPC and memory PRs.

This is deliberately a *failing acceptance contract* once both optional
modules are present without their atomic event->grant integration. Main does
not have either module, so the test skips there. Do not mark it xfail or
claim it verifies an integrated scheduler until the integration is shipped.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import unittest
from pathlib import Path

from realm.engine import Engine
from realm.worldpack import import_worldpack


@unittest.skipUnless(
    importlib.util.find_spec("realm.simulation") is not None
    and importlib.util.find_spec("realm.actor_memory") is not None,
    "Requires both PR27 simulation and PR28 actor memory",
)
class AtomicMemoryIntegrationContractTests(unittest.TestCase):
    def test_copresent_residents_remember_every_committed_speech_in_one_tick(self):
        from realm import actor_memory as memory
        from realm import simulation

        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            world_path = Path(__file__).resolve().parent.parent / "examples" / "shore-world.json"
            db_path = Path(directory) / "world.sqlite3"
            import_worldpack(db_path, world_path)
            engine = Engine(db_path)
            clock = simulation.SimulationClock(engine, expected_world_id="shore-example")
            clock.install()
            clock.register_npc("Moss", persona="A careful witness", curiosity=80, duty=20)
            clock.register_npc("Ember", persona="Another careful witness", curiosity=20, duty=80)
            memories = memory.ActorMemory(engine, world_id="shore-example")

            clock.acquire_lease()
            clock.plan_next_tick()
            outcomes = clock.apply_pending_tick()
            self.assertEqual([result.status for result in outcomes], ["committed", "committed"])

            with sqlite3.connect(db_path) as db:
                speech_events = db.execute(
                    "SELECT id, actor FROM events WHERE kind='say' ORDER BY id"
                ).fetchall()
            self.assertEqual(len(speech_events), 2)

            # No legal after-the-fact replay grant for the first event once
            # the second event is head. The scheduler must issue grants in its
            # existing transaction alongside each event/receipt.
            expected = {event_id for event_id, _ in speech_events}
            for observer in ("Moss", "Ember"):
                visible = {row["source_event_id"] for row in memories.recall(observer)}
                self.assertTrue(expected <= visible, f"Missing {expected - visible} for {observer}")

            # Committed rights survive restart, not in-memory callbacks.
            restarted = memory.ActorMemory(Engine(db_path), world_id="shore-example")
            for observer in ("Moss", "Ember"):
                visible = {row["source_event_id"] for row in restarted.recall(observer)}
                self.assertTrue(expected <= visible, f"Restart lost grants for {observer}")
