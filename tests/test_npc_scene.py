"""Host-built NPC context must honor the current room-entry privacy boundary."""
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
from realm.npc_scene import ResidentSceneBuilder
from realm.npc_turn_host import ResidentTurnHost, ScopedResidentTurn
from realm.residents import ResidentController


class _Fixture(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        obj = json.loads(self.rfile.read(length))
        self.server.received.append(obj)
        if self.server.before_reply:
            self.server.before_reply()
        answer = json.dumps({"choices": [{"message": {"content":
            json.dumps({"command": "say NPC_RESPONSE"})}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *_args):
        pass


class ResidentSceneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        self.other_path = Path(temp.name) / "other.sqlite3"
        self.engine = Engine(self.path)
        self.other = Engine(self.other_path)
        self.engine.join("Alice")
        self.engine.join("Moss")
        self.other.join("Moss")
        self.moss = ResidentController(self.engine, "Moss", allowed_verbs=frozenset({"say"}))
        self.scene = ResidentSceneBuilder(self.engine)

    def _events(self, actor: str):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT payload_json FROM events WHERE actor=? AND kind='say' ORDER BY id",
                (actor,),
            ).fetchall()

    def _server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
        server.received = []
        server.before_reply = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        proposer = LocalChatProposer(LocalChatConfig(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            model="fixture", timeout_seconds=2,
        ))
        return server, ResidentTurnHost(self.engine, proposer)

    def test_current_arrival_hides_earlier_speech_but_shows_later_speech(self):
        self.engine.act("Alice", "say BEFORE_ENTRY")
        self.engine.join("Cara")
        cara = ResidentController(self.engine, "Cara", allowed_verbs=frozenset({"say"}))
        self.engine.act("Alice", "say AFTER_ENTRY")
        scene = self.scene.build(cara)
        self.assertNotIn("BEFORE_ENTRY", scene.view)
        self.assertIn("AFTER_ENTRY", scene.view)
        obj = json.loads(scene.view)
        self.assertEqual("Cara", obj["resident"])
        self.assertEqual("The Atrium", obj["room"]["name"])
        self.assertIn("north", obj["room"]["exits"])
        self.assertIn("Brass token", obj["room"]["items"])

    def test_depart_reenter_cannot_hear_absent_or_old_speech(self):
        self.engine.act("Alice", "say OLD_VISIT")
        self.engine.act("Moss", "north")
        self.engine.act("Alice", "say WHILE_AWAY")
        self.engine.act("Moss", "south")
        self.engine.act("Alice", "say AFTER_RETURN")
        scene = self.scene.build(self.moss)
        self.assertNotIn("OLD_VISIT", scene.view)
        self.assertNotIn("WHILE_AWAY", scene.view)
        self.assertIn("AFTER_RETURN", scene.view)

    def test_foreign_world_controller_denied_before_any_scene_is_built(self):
        foreign = ResidentController(self.other, "Moss", allowed_verbs=frozenset({"say"}))
        with self.assertRaisesRegex(ValueError, "same Engine"):
            self.scene.build(foreign)

    def test_removed_latest_arrival_denies_without_old_join_fallback(self):
        self.engine.act("Moss", "north")
        self.engine.act("Moss", "south")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM events WHERE actor='Moss' AND kind='move' AND room_id='atrium'")
        with self.assertRaisesRegex(ValueError, "arrival"):
            self.scene.build(self.moss)

    def test_tampered_large_or_nonprintable_speech_fails_closed(self):
        self.engine.act("Alice", "say safe")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE events SET payload_json=? WHERE actor='Alice' AND kind='say'",
                (json.dumps({"text": "unsafe\u001btext"}),),
            )
        with self.assertRaises(ValueError):
            self.scene.build(self.moss)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE events SET payload_json=? WHERE actor='Alice' AND kind='say'",
                (json.dumps({"text": "A" * 7000}),),
            )
        with self.assertRaises(ValueError):
            self.scene.build(self.moss)

    def test_scoped_tick_uses_host_selected_scene_and_receipt_replay(self):
        server, host = self._server()
        self.engine.act("Alice", "say SEEN_BY_NPC")
        first = host.run_scoped_tick([ScopedResidentTurn(self.moss, "scene-0001")])[0]
        second = host.run_scoped_tick([ScopedResidentTurn(self.moss, "scene-0001")])[0]
        self.assertEqual((False, True), (first.replayed, second.replayed))
        self.assertEqual(1, len(server.received))
        sent = server.received[0]["messages"][1]["content"]
        self.assertIn("SEEN_BY_NPC", sent)
        self.assertEqual(1, len(self._events("Moss")))

    def test_scene_movement_during_model_call_rejects_stale_commit(self):
        server, host = self._server()
        server.before_reply = lambda: self.engine.act("Moss", "north")
        with self.assertRaisesRegex(ValueError, "Stale"):
            host.run_scoped_tick([ScopedResidentTurn(self.moss, "scene-move")])
        self.assertEqual(1, len(server.received))
        self.assertEqual(0, len(self._events("Moss")))

    def test_scene_departure_and_reentry_during_inference_denies_old_scene(self):
        server, host = self._server()

        def travel_away_and_back():
            self.engine.act("Moss", "north")
            self.engine.act("Moss", "south")

        server.before_reply = travel_away_and_back
        with self.assertRaisesRegex(ValueError, "reentry"):
            host.run_scoped_tick([ScopedResidentTurn(self.moss, "scene-reentry")])
        self.assertEqual(1, len(server.received))
        self.assertEqual(0, len(self._events("Moss")))

    def test_invalid_later_scoped_turn_fails_before_first_model_call(self):
        server, host = self._server()
        with self.assertRaisesRegex(ValueError, "receipt"):
            host.run_scoped_tick([
                ScopedResidentTurn(self.moss, "good-id"),
                ScopedResidentTurn(self.moss, "bad\nreceipt"),
            ])
        self.assertEqual([], server.received)
        self.assertEqual(0, len(self._events("Moss")))


if __name__ == "__main__":
    unittest.main()
