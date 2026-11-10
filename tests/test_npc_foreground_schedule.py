"""Host-invoked NPC cadence selection with real SQLite and a loopback fixture."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.npc_foreground_schedule import ResidentCadence, ResidentForegroundSchedule
from realm.npc_local_model import LocalChatConfig, LocalChatProposer
from realm.npc_turn_host import ResidentTurnHost
from realm.residents import ResidentController


class _Model(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.server.requests.append(json.loads(self.rfile.read(length)))
        content = json.dumps({"command": "say FIxture turn"})
        payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


class ResidentForegroundScheduleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "one.sqlite3"
        self.other_path = Path(temp.name) / "other.sqlite3"
        self.engine = Engine(self.path)
        self.foreign = Engine(self.other_path)
        self.engine.join("Alice")
        self.engine.act("Alice", "say BEFORE_RESIDENTS")
        self.engine.join("Moss")
        self.engine.join("Ember")
        self.engine.act("Alice", "say AFTER_RESIDENTS")
        self.foreign.join("Moss")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
        self.server.requests = []
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.proposer = LocalChatProposer(LocalChatConfig(
            endpoint=f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions",
            model="offline-fixture", timeout_seconds=2,
        ))
        self.moss = ResidentController(self.engine, "Moss", allowed_verbs=frozenset({"say"}))
        self.ember = ResidentController(self.engine, "Ember", allowed_verbs=frozenset({"say"}))
        self.host = ResidentTurnHost(self.engine, self.proposer)

    def _schedule(self, host=None, moss=None):
        return ResidentForegroundSchedule(
            host or self.host, (
                ResidentCadence(moss or self.moss, every_pulses=2),
                ResidentCadence(self.ember, every_pulses=3, phase=1),
            ), schedule_id="synthetic-v1",
        )

    def _speeches(self, db_path):
        with closing(sqlite3.connect(db_path)) as db:
            return db.execute("SELECT actor,payload_json FROM events WHERE kind='say' AND actor IN ('Moss','Ember') ORDER BY id").fetchall()

    def test_only_due_residents_respond_and_receive_scoped_room_context(self):
        schedule = self._schedule()
        at_zero = schedule.run_pulse(0)
        at_one = schedule.run_pulse(1)
        at_three = schedule.run_pulse(3)
        at_four = schedule.run_pulse(4)
        self.assertEqual([r.actor for r in at_zero], ["Moss"])
        self.assertEqual([r.actor for r in at_one], ["Ember"])
        self.assertEqual(at_three, ())
        self.assertEqual([r.actor for r in at_four], ["Moss", "Ember"])
        self.assertEqual(4, len(self.server.requests))
        self.assertEqual(4, len(self._speeches(self.path)))
        self.assertEqual([], self._speeches(self.other_path))
        self.assertEqual(4, len({r.action_id for r in at_zero + at_one + at_four}))
        for request in self.server.requests:
            view = request["messages"][1]["content"]
            self.assertIn("AFTER_RESIDENTS", view)
            self.assertNotIn("BEFORE_RESIDENTS", view)

    def test_restart_replays_original_receipt_without_another_model_call(self):
        first = self._schedule().run_pulse(0)[0]
        restarted = Engine(self.path)
        same_controller = ResidentController(restarted, "Moss", allowed_verbs=frozenset({"say"}))
        same_host = ResidentTurnHost(restarted, self.proposer)
        schedule = ResidentForegroundSchedule(
            same_host, (ResidentCadence(same_controller, every_pulses=2),),
            schedule_id="synthetic-v1",
        )
        replay = schedule.run_pulse(0)[0]
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.action_id, replay.action_id)
        self.assertEqual(first.response, replay.response)
        self.assertEqual(1, len(self.server.requests))
        self.assertEqual(1, len(self._speeches(self.path)))

    def test_cross_world_alias_and_bad_schedule_rejected_before_inference(self):
        outsider = ResidentController(self.foreign, "Moss", allowed_verbs=frozenset({"say"}))
        with self.assertRaisesRegex(ValueError, "same Engine"):
            ResidentForegroundSchedule(self.host, (ResidentCadence(outsider, 1),), schedule_id="x")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ResidentForegroundSchedule(self.host, (
                ResidentCadence(self.moss, 1),
                ResidentCadence(ResidentController(self.engine, "mOSS"), 1),
            ), schedule_id="x")
        with self.assertRaises(ValueError):
            ResidentCadence(self.moss, every_pulses=True)
        with self.assertRaises(ValueError):
            ResidentCadence(self.moss, every_pulses=2, phase=2)
        with self.assertRaises(ValueError):
            ResidentForegroundSchedule(self.host, (ResidentCadence(self.moss, 1),), schedule_id="bad\n")
        self.assertEqual([], self.server.requests)
        self.assertEqual([], self._speeches(self.path))

    def test_invalid_or_over_capacity_pulse_cannot_partially_act(self):
        schedule = self._schedule()
        for invalid in (True, -1, 2**64):
            with self.assertRaisesRegex(ValueError, "pulse"):
                schedule.run_pulse(invalid)
        constrained = ResidentForegroundSchedule(
            ResidentTurnHost(self.engine, self.proposer, max_turns=1),
            (ResidentCadence(self.moss, 1), ResidentCadence(self.ember, 1)),
            schedule_id="crowded",
        )
        with self.assertRaisesRegex(ValueError, "capacity"):
            constrained.run_pulse(0)
        self.assertEqual([], self.server.requests)
        self.assertEqual([], self._speeches(self.path))


if __name__ == "__main__":
    unittest.main()
