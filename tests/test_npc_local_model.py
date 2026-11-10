"""Loopback model proposals are untrusted; a fixture is not neural inference."""

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
from realm.residents import ResidentController


class _ChatFixture(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.server.requests.append(json.loads(self.rfile.read(size)))
        status, body = self.server.responses.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _reply(command: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": json.dumps({"command": command})}}]}).encode()


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "model-world.sqlite3"
        self.engine = Engine(self.db)
        self.engine.join("Moss")
        self.engine.join("Ember")
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _ChatFixture)
        self.http.requests = []
        self.http.responses = []
        self.addCleanup(self.http.server_close)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.http.shutdown)
        self.proposer = LocalChatProposer(LocalChatConfig(
            endpoint=f"http://127.0.0.1:{self.http.server_port}/v1/chat/completions",
            model="fixture", timeout_seconds=2.0,
        ))

    def _speech(self, actor):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT COUNT(*) FROM events WHERE actor=? AND kind='say'", (actor,)).fetchone()[0]

    def test_loopback_fixture_proposes_one_command_without_committing(self):
        self.http.responses.append((200, _reply("say Rain is coming")))
        proposal = self.proposer.propose("Only what Moss could see")
        self.assertEqual({"command": "say Rain is coming"}, proposal)
        self.assertEqual(0, self._speech("Moss"))
        self.assertEqual("Only what Moss could see", self.http.requests[0]["messages"][1]["content"])
        self.assertEqual("fixture", self.http.requests[0]["model"])
        self.assertEqual(0, self.http.requests[0]["temperature"])

    def test_host_bound_controller_decides_actor_and_verb_policy(self):
        self.http.responses.append((200, _reply("say Affirmative")))
        controller = ResidentController(self.engine, "Moss", allowed_verbs=frozenset({"say"}))
        result = self.proposer.commit(controller, "Moss's room", action_id="moss-1")
        self.assertIn("You say", result)
        self.assertEqual(1, self._speech("Moss"))
        self.assertEqual(0, self._speech("Ember"))
        self.http.responses.append((200, _reply("take token")))
        with self.assertRaisesRegex(ValueError, "not authorized"):
            self.proposer.commit(controller, "Moss's room", action_id="moss-2")
        self.assertEqual(1, self._speech("Moss"))

    def test_rejects_any_remote_or_credentialed_endpoint(self):
        bad = (
            "http://example.com:9999/v1/chat/completions",
            "http://localhost:9999/v1/chat/completions",
            "http://127.0.0.1/v1/chat/completions",
            "https://127.0.0.1:9999/v1/chat/completions",
            "http://user:pass@127.0.0.1:9999/v1/chat/completions",
            "http://127.0.0.1:9999/v1/chat/completions?token=x",
            "http://127.0.0.1:9999/v1/chat/completions#fragment",
        )
        for endpoint in bad:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                LocalChatConfig(endpoint=endpoint, model="test")

    def test_rejects_invalid_host_view_before_http(self):
        for view in ("", "Bad\x1bsequence", "x" * 6001, 42):
            with self.subTest(view=str(view)[:20]), self.assertRaises(ValueError):
                self.proposer.propose(view)
        self.assertEqual([], self.http.requests)

    def test_rejects_extra_or_duplicate_proposal_fields(self):
        malformed = (
            b'{"choices":[{"message":{"content":"{\\"command\\":\\"say hi\\",\\"actor\\":\\"Ember\\"}"}}]}',
            b'{"choices":[{"message":{"content":"{\\"command\\":\\"say hi\\",\\"command\\":\\"take token\\"}"}}]}',
            b'{"choices":[{"message":{"content":"not json"}}]}',
        )
        for payload in malformed:
            self.http.responses.append((200, payload))
            with self.subTest(payload=payload[:40]), self.assertRaises(ValueError):
                self.proposer.propose("Host scene")
        self.assertEqual(0, self._speech("Moss"))

    def test_non_200_or_excessive_response_never_commits(self):
        self.http.responses.append((500, b'{}'))
        with self.assertRaisesRegex(ValueError, "HTTP status 500"):
            self.proposer.propose("Host scene")
        self.http.responses.append((200, b"x" * 9000))
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.proposer.propose("Host scene")


if __name__ == "__main__":
    unittest.main()
