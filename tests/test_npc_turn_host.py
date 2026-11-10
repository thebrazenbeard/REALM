"""Host-driven resident turns with real SQLite and a loopback model fixture."""

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
from realm.npc_local_model import LocalChatConfig, LocalChatProposer
from realm.npc_turn_host import ResidentTurn, ResidentTurnHost
from realm.residents import ResidentController


def _answer(command: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": json.dumps({"command": command})}}]}).encode()


class _ModelFixture(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.server.requests.append(json.loads(self.rfile.read(size)))
        reply = self.server.responses.pop(0)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *_args):
        return


class ResidentTurnHostTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.first_path = Path(tmp.name) / "world-a.sqlite3"
        self.other_path = Path(tmp.name) / "world-b.sqlite3"
        self.engine = Engine(self.first_path)
        self.other = Engine(self.other_path)
        self.engine.join("Moss")
        self.engine.join("Ember")
        self.other.join("Moss")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelFixture)
        self.server.requests = []
        self.server.responses = []
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.proposer = LocalChatProposer(LocalChatConfig(
            endpoint=f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions",
            model="fixture", timeout_seconds=2.0,
        ))
        self.host = ResidentTurnHost(self.engine, self.proposer)
        self.moss = ResidentController(self.engine, "Moss", allowed_verbs=frozenset({"say"}))
        self.ember = ResidentController(self.engine, "Ember", allowed_verbs=frozenset({"say"}))

    def _speech(self, path, actor):
        with closing(sqlite3.connect(path)) as db:
            return db.execute("SELECT payload_json FROM events WHERE actor=? AND kind='say' ORDER BY id", (actor,)).fetchall()

    def test_bounded_host_tick_commits_two_actor_attributed_events(self):
        self.server.responses.extend((_answer("say Moss ready"), _answer("say Ember ready")))
        result = self.host.run_tick([
            ResidentTurn(self.moss, "Moss-scoped room", "tick-1"),
            ResidentTurn(self.ember, "Ember-scoped room", "tick-1"),
        ])
        self.assertEqual([r.actor for r in result], ["Moss", "Ember"])
        self.assertEqual([r.replayed for r in result], [False, False])
        self.assertEqual(2, len(self.server.requests))
        self.assertEqual("Moss-scoped room", self.server.requests[0]["messages"][1]["content"])
        self.assertIn("Moss ready", self._speech(self.first_path, "Moss")[0][0])
        self.assertIn("Ember ready", self._speech(self.first_path, "Ember")[0][0])

    def test_restart_replay_uses_sqlite_receipt_not_a_new_model_call(self):
        self.server.responses.append(_answer("say Confirmed"))
        first = self.host.run_tick([ResidentTurn(self.moss, "Scene", "fixed-id")])[0]
        restarted = Engine(self.first_path)
        again = ResidentTurnHost(restarted, self.proposer).run_tick([
            ResidentTurn(ResidentController(restarted, "Moss", allowed_verbs=frozenset({"say"})), "Scene", "fixed-id")
        ])[0]
        self.assertEqual(first.response, again.response)
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(1, len(self.server.requests))
        self.assertEqual(1, len(self._speech(self.first_path, "Moss")))

    def test_cross_world_controller_is_rejected_before_inference(self):
        foreign = ResidentController(self.other, "Moss", allowed_verbs=frozenset({"say"}))
        with self.assertRaisesRegex(ValueError, "same Engine"):
            self.host.run_tick([ResidentTurn(foreign, "Foreign private view", "tick-foreign")])
        self.assertEqual([], self.server.requests)
        self.assertEqual([], self._speech(self.first_path, "Moss"))
        self.assertEqual([], self._speech(self.other_path, "Moss"))

    def test_duplicate_casefold_actor_and_oversized_batch_fail_before_first_action(self):
        alias = ResidentController(self.engine, "mOSS", allowed_verbs=frozenset({"say"}))
        with self.assertRaisesRegex(ValueError, "one turn per resident"):
            self.host.run_tick([
                ResidentTurn(self.moss, "First", "tick-a"), ResidentTurn(alias, "Second", "tick-b")
            ])
        with self.assertRaisesRegex(ValueError, "batch"):
            self.host.run_tick([ResidentTurn(self.moss, "Scene", "tick-a")] * 5)
        self.assertEqual(0, len(self.server.requests))

    def test_invalid_later_turn_preflights_before_earlier_mutation(self):
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.host.run_tick([
                ResidentTurn(self.moss, "Valid", "valid-id"),
                ResidentTurn(self.ember, "Valid", "bad\nreceipt"),
            ])
        self.assertEqual(0, len(self.server.requests))
        self.assertEqual([], self._speech(self.first_path, "Moss"))

    def test_model_actor_injection_does_not_create_other_actor_event(self):
        self.server.responses.append(json.dumps({"choices": [{"message": {
            "content": '{"command":"say Forged","actor":"Ember"}',
        }}]}).encode())
        with self.assertRaises(ValueError):
            self.host.run_tick([ResidentTurn(self.moss, "Moss view", "forged")])
        self.assertEqual([], self._speech(self.first_path, "Moss"))
        self.assertEqual([], self._speech(self.first_path, "Ember"))

    def test_policy_tightening_denies_receipt_replay_without_inference(self):
        self.server.responses.append(_answer("say First"))
        self.host.run_tick([ResidentTurn(self.moss, "Scene", "stable")])
        tighter = ResidentController(self.engine, "Moss", allowed_verbs=frozenset({"look"}))
        with self.assertRaisesRegex(ValueError, "not authorized"):
            self.host.run_tick([ResidentTurn(tighter, "Scene", "stable")])
        self.assertEqual(1, len(self.server.requests))
        self.assertEqual(1, len(self._speech(self.first_path, "Moss")))

    def test_partial_tick_retries_committed_action_without_repeat_inference(self):
        self.server.responses.extend((_answer("say First"), _answer("take token")))
        with self.assertRaisesRegex(ValueError, "not authorized"):
            self.host.run_tick([
                ResidentTurn(self.moss, "First", "partial"),
                ResidentTurn(self.ember, "Second", "partial"),
            ])
        self.assertEqual(1, len(self._speech(self.first_path, "Moss")))
        self.assertEqual(0, len(self._speech(self.first_path, "Ember")))
        self.server.responses.append(_answer("say Recovery"))
        results = self.host.run_tick([
            ResidentTurn(self.moss, "First", "partial"),
            ResidentTurn(self.ember, "Second", "partial"),
        ])
        self.assertEqual([True, False], [r.replayed for r in results])
        self.assertEqual(3, len(self.server.requests))
        self.assertEqual(1, len(self._speech(self.first_path, "Moss")))
        self.assertEqual(1, len(self._speech(self.first_path, "Ember")))

    def test_unknown_resident_and_invalid_view_fail_before_inference(self):
        unknown = ResidentController(self.engine, "NotJoined", allowed_verbs=frozenset({"say"}))
        with self.assertRaisesRegex(ValueError, "joined"):
            self.host.run_tick([ResidentTurn(unknown, "Host scene", "tick-x")])
        with self.assertRaisesRegex(ValueError, "view"):
            self.host.run_tick([ResidentTurn(self.moss, "Bad\x1bview", "tick-y")])
        self.assertEqual(0, len(self.server.requests))


if __name__ == "__main__":
    unittest.main()
