"""Deterministic, no-model tests of two persistent host-owned NPCs."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from realm.auth import CredentialStore
from realm.conversations import NPCDuetRunner, NPCIdentity, register_npc_pair
from realm.engine import Engine
from realm.worldpack import import_worldpack


FLAGSHIP = ROOT / "worlds" / "flagship" / "siltglass-reach" / "worldpack.v1.json"
ROSTER = (
    NPCIdentity("FerrySteward", "A cautious ferry steward protecting the crossing's livelihoods."),
    NPCIdentity("CrossingInspector", "An inspector who challenges hearsay about the old crossing."),
)


class NPCConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "siltglass.sqlite3"
        self.assertFalse(import_worldpack(self.db, FLAGSHIP)["unchanged"])
        self.engine = Engine(self.db)

    def _count(self, table: str, *, kind: str | None = None) -> int:
        with closing(sqlite3.connect(self.db)) as db:
            if kind is None:
                return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            return db.execute(
                "SELECT COUNT(*) FROM events WHERE kind=?", (kind,)
            ).fetchone()[0]

    def _register(self):
        register_npc_pair(self.engine, ROSTER, expected_world_id="siltglass-reach")

    def _has_duet_schema(self):
        with closing(sqlite3.connect(self.db)) as db:
            return bool(db.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('npc_actor_registry','npc_duet_sessions')"
            ).fetchall())

    @staticmethod
    def _providers():
        def steward(ctx):
            assert ctx.room_id == "tidegate"
            assert "ferry steward" in ctx.persona
            return {"command": "say Keep the route open until the measurements settle."}

        def inspector(ctx):
            assert ctx.previous_speech is not None
            assert "measurements" in ctx.previous_speech
            return {"command": "say I will compare the ledger before I agree."}

        return {"FerrySteward": steward, "CrossingInspector": inspector}

    def test_pair_registration_is_explicit_persistent_and_non_hijacking(self):
        self.assertEqual(0, self._count("players"))
        self._register()
        self.assertEqual(2, self._count("players"))
        self.assertEqual(2, self._count("events", kind="join"))
        self.assertEqual(2, self._count("npc_actor_registry"))
        with closing(sqlite3.connect(self.db)) as db:
            rows = db.execute(
                "SELECT actor,persona FROM npc_actor_registry ORDER BY actor"
            ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertTrue(any("inspector" in persona for _, persona in rows))
        self.assertIn("Tidegate", Engine(self.db).act("FerrySteward", "look"))
        with self.assertRaisesRegex(ValueError, "existing player"):
            self._register()
        self.assertEqual(2, self._count("players"))

    def test_conversation_commits_four_alternating_speech_events_and_resumes(self):
        self._register()
        runner = NPCDuetRunner(
            self.engine, actor_a="FerrySteward", actor_b="CrossingInspector",
            providers=self._providers(),
        )
        turns = runner.run(session_id="first_trial", turns=4)
        self.assertEqual(4, len(turns))
        self.assertEqual(4, self._count("events", kind="say"))
        self.assertEqual(4, self._count("action_receipts"))
        self.assertEqual(
            ["FerrySteward", "CrossingInspector", "FerrySteward", "CrossingInspector"],
            [turn.speaker for turn in turns],
        )
        self.assertTrue(all(turn.room_id == "tidegate" and not turn.replayed for turn in turns))
        with closing(sqlite3.connect(self.db)) as db:
            events = db.execute(
                "SELECT actor,room_id,payload_json FROM events WHERE kind='say' ORDER BY id"
            ).fetchall()
        self.assertEqual(4, len(events))
        self.assertTrue(all(room == "tidegate" for _, room, _ in events))
        self.assertEqual("Keep the route open until the measurements settle.", json.loads(events[0][2])["text"])

        def forbidden(_):
            self.fail("A committed turn must not invoke a provider again")

        restarted = NPCDuetRunner(
            Engine(self.db), actor_a="FerrySteward", actor_b="CrossingInspector",
            providers={"FerrySteward": forbidden, "CrossingInspector": forbidden},
        )
        replayed = restarted.run(session_id="first_trial", turns=4)
        self.assertTrue(all(turn.replayed for turn in replayed))
        self.assertEqual([t.response for t in turns], [t.response for t in replayed])
        self.assertEqual(4, self._count("events", kind="say"))

    def test_model_extra_actor_and_item_move_proposals_are_denied(self):
        self._register()
        for bad in (
            {"command": "say Secret", "actor": "SomeHuman"},
            {"command": "take charred_receipt"},
            {"command": "north"},
            {"command": "say \x1b]52;c;clipboard"},
        ):
            with self.subTest(proposal=bad):
                runner = NPCDuetRunner(
                    self.engine, actor_a="FerrySteward", actor_b="CrossingInspector",
                    providers={"FerrySteward": lambda _, payload=bad: payload,
                               "CrossingInspector": lambda _: {"command": "say OK"}},
                )
                with self.assertRaises(ValueError):
                    runner.run(session_id="bad_trial", turns=2)
        self.assertEqual(0, self._count("events", kind="say"))
        self.assertEqual(0, self._count("action_receipts"))

    def test_world_binding_fresh_names_and_same_room_are_required(self):
        with self.assertRaisesRegex(ValueError, "world_id"):
            register_npc_pair(self.engine, ROSTER, expected_world_id="another_world")
        self.assertEqual(0, self._count("players"))
        self.assertFalse(self._has_duet_schema(), "Rejected registration must not create tables")

        self.engine.join("FerrySteward")
        with self.assertRaisesRegex(ValueError, "existing player"):
            self._register()
        self.assertEqual(1, self._count("players"))
        self.assertFalse(self._has_duet_schema(), "Identity collision must not create tables")

        db2 = Path(self.tmp.name) / "fresh_siltglass.sqlite3"
        import_worldpack(db2, FLAGSHIP)
        other = Engine(db2)
        register_npc_pair(other, ROSTER, expected_world_id="siltglass-reach")
        self.assertIn("Lantern Jetty", other.act("CrossingInspector", "east"))
        runner = NPCDuetRunner(
            other, actor_a="FerrySteward", actor_b="CrossingInspector",
            providers=self._providers(),
        )
        with self.assertRaisesRegex(ValueError, "same room"):
            runner.run(session_id="separated")

    def test_rejects_preprovisioned_player_credentials(self):
        CredentialStore(self.engine).provision("FerrySteward")
        with self.assertRaisesRegex(ValueError, "credential"):
            self._register()
        self.assertEqual(0, self._count("players"))
        self.assertFalse(self._has_duet_schema(), "Credential collision must not create tables")

    def test_actor_movement_during_inference_cannot_relocate_speech(self):
        self._register()

        def malicious_provider(ctx):
            self.assertEqual("tidegate", ctx.room_id)
            self.assertIn("Lantern Jetty", self.engine.act("FerrySteward", "east"))
            return {"command": "say This belonged in the original room."}

        runner = NPCDuetRunner(
            self.engine, actor_a="FerrySteward", actor_b="CrossingInspector",
            providers={"FerrySteward": malicious_provider,
                       "CrossingInspector": lambda _: {"command": "say Valid"}},
        )
        with self.assertRaisesRegex(ValueError, "Actor moved"):
            runner.run(session_id="actor_moved", turns=2)
        self.assertEqual(0, self._count("events", kind="say"))
        self.assertEqual(0, self._count("action_receipts"))

    def test_peer_movement_during_inference_cannot_publish_false_dialogue(self):
        self._register()

        def malicious_provider(ctx):
            self.assertEqual("tidegate", ctx.room_id)
            self.assertIn("Lantern Jetty", self.engine.act("CrossingInspector", "east"))
            return {"command": "say A line from a room now abandoned by the listener."}

        runner = NPCDuetRunner(
            self.engine, actor_a="FerrySteward", actor_b="CrossingInspector",
            providers={"FerrySteward": malicious_provider,
                       "CrossingInspector": lambda _: {"command": "say Valid"}},
        )
        with self.assertRaisesRegex(ValueError, "partner moved"):
            runner.run(session_id="peer_moved", turns=2)
        self.assertEqual(0, self._count("events", kind="say"))
        self.assertEqual(0, self._count("action_receipts"))

    def test_registered_npcs_cannot_be_adopted_by_player_join_or_provisioning(self):
        self._register()
        before_players = self._count("players")
        before_joins = self._count("events", kind="join")
        for spelling in ("FerrySteward", "ferrysteward", "CROSSINGINSPECTOR"):
            with self.subTest(join=spelling), self.assertRaisesRegex(ValueError, "reserved"):
                Engine(self.db).join(spelling)
            with self.subTest(credential=spelling), self.assertRaisesRegex(ValueError, "NPC identity"):
                CredentialStore(self.engine).provision(spelling)
        self.assertEqual(before_players, self._count("players"))
        self.assertEqual(before_joins, self._count("events", kind="join"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM player_credentials").fetchone()[0])

    def test_session_binding_and_turn_limits(self):
        self._register()
        runner = NPCDuetRunner(
            self.engine, actor_a="FerrySteward", actor_b="CrossingInspector",
            providers=self._providers(),
        )
        for turns in (0, 21, -1, True):
            with self.subTest(turns=turns), self.assertRaises(ValueError):
                runner.run(session_id="unit_trial", turns=turns)
        with self.assertRaises(ValueError):
            runner.run(session_id="../unsafe", turns=2)
        runner.run(session_id="bound_trial", turns=2)
        swapped = NPCDuetRunner(
            self.engine, actor_a="CrossingInspector", actor_b="FerrySteward",
            providers={"FerrySteward": lambda _: {"command": "say No"},
                       "CrossingInspector": lambda _: {"command": "say No"}},
        )
        with self.assertRaisesRegex(ValueError, "already bound"):
            swapped.run(session_id="bound_trial", turns=2)

    def test_short_session_can_extend_after_restart_without_regeneration(self):
        self._register()
        runner = NPCDuetRunner(
            self.engine, actor_a="FerrySteward", actor_b="CrossingInspector",
            providers=self._providers(),
        )
        runner.run(session_id="resume_test", turns=2)
        replay_seen = []

        def steward(ctx):
            replay_seen.append((ctx.speaker, ctx.turn_index, ctx.previous_speech))
            return {"command": "say Keep the route open until the measurements settle."}

        def inspector(ctx):
            replay_seen.append((ctx.speaker, ctx.turn_index, ctx.previous_speech))
            return {"command": "say I will compare the ledger before I agree."}

        resumed = NPCDuetRunner(
            Engine(self.db), actor_a="FerrySteward", actor_b="CrossingInspector",
            providers={"FerrySteward": steward, "CrossingInspector": inspector},
        ).run(session_id="resume_test", turns=4)
        self.assertEqual([True, True, False, False], [t.replayed for t in resumed])
        self.assertEqual([("FerrySteward", 2), ("CrossingInspector", 3)],
                         [(name, index) for name, index, _ in replay_seen])
        self.assertEqual(4, self._count("events", kind="say"))

    def test_separate_local_world_databases_never_share_speech(self):
        self._register()
        isolated_db = Path(self.tmp.name) / "separate_owner.sqlite3"
        import_worldpack(isolated_db, FLAGSHIP)
        other = Engine(isolated_db)
        register_npc_pair(other, ROSTER, expected_world_id="siltglass-reach")

        for engine, utterance in ((self.engine, "Only in first world."),
                                  (other, "Only in second world.")):
            providers = {
                name: (lambda _ctx, value=utterance: {"command": f"say {value}"})
                for name in ("FerrySteward", "CrossingInspector")
            }
            NPCDuetRunner(
                engine, actor_a="FerrySteward", actor_b="CrossingInspector",
                providers=providers,
            ).run(session_id="isolated_trial", turns=2)
        with closing(sqlite3.connect(self.db)) as left, closing(sqlite3.connect(isolated_db)) as right:
            def texts(db):
                return [json.loads(payload)["text"] for (payload,) in db.execute(
                    "SELECT payload_json FROM events WHERE kind='say' ORDER BY id"
                )]
            self.assertEqual(["Only in first world."] * 2, texts(left))
            self.assertEqual(["Only in second world."] * 2, texts(right))


if __name__ == "__main__":
    unittest.main()
