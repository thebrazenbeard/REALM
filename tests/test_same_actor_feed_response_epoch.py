"""Two separately connected HTTP clients with one bearer; stale room feed custody.

All tokens synthetic and only disposable SQLite + OS ephemeral loopback sockets.
The second client changes the same actor's location between a completed feed
snapshot and the first client's HTTP send. This does not assert session monopoly:
it tests current-room response-time authorization independently.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib import request, error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway, GatewayServer


class SameActorFeedResponseEpochTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gateway = Gateway(
            Path(temp.name) / "world.sqlite3", world_id="shared_actor_epoch",
            scoped_credentials=getattr(self, "scoped", False),
            scoped_guard_path=Path(temp.name) / "scoped-guard.json",
        )
        store = (self.gateway.scoped_credentials
                 if getattr(self, "scoped", False) else self.gateway.credentials)
        self.token = store.provision("Alice")
        self.bob = store.provision("Bob")
        self.server = GatewayServer(self.gateway, (), port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def request(self, token, endpoint, body=None):
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        msg = request.Request(
            f"http://127.0.0.1:{self.server.server_port}{endpoint}",
            headers=headers, data=data,
        )
        try:
            with request.urlopen(msg, timeout=6) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_old_http_client_cannot_receive_old_room_feed_after_same_actor_moves(self):
        self._exercise_competing_same_actor_response(move=True)

    def test_old_http_client_cannot_receive_old_room_feed_after_same_actor_rejoins(self):
        self._exercise_competing_same_actor_response(move=False)

    def _exercise_competing_same_actor_response(self, *, move: bool):
        status, joined = self.request(self.token, "/api/v1/join", {"player": "Alice"})
        self.assertEqual(200, status)
        self.assertEqual(200, self.request(self.bob, "/api/v1/join", {"player": "Bob"})[0])
        secret = "ROOM_SPEECH_BEFORE_COMPETING_SAME_ACTOR_MOVE"
        self.assertEqual(200, self.request(self.bob, "/api/v1/command",
                                           {"command": "say " + secret, "action_id": "bob-1"})[0])
        snapshot_ready = threading.Event()
        allow_send = threading.Event()
        original_feed = self.gateway.feed

        def pause_after_feed(actor, after):
            result = original_feed(actor, after)
            if actor == "Alice" and not snapshot_ready.is_set():
                self.assertTrue(any(secret in e["text"] for e in result["events"]))
                snapshot_ready.set()
                if not allow_send.wait(4):
                    raise AssertionError("Old HTTP feed was never released")
            return result

        self.gateway.feed = pause_after_feed
        old_result = []

        def old_client():
            old_result.append(self.request(
                self.token, f"/api/v1/feed?player=Alice&after={joined['cursor']}"
            ))
        t = threading.Thread(target=old_client, daemon=True)
        t.start()
        try:
            self.assertTrue(snapshot_ready.wait(3), "Old feed did not reach send boundary")
            # New physical HTTP connection, same valid actor bearer. Existing
            # semantics permit the second client to move the canonical actor.
            if move:
                moved_status, moved = self.request(
                    self.token, "/api/v1/command",
                    {"command": "north", "action_id": "competing-second-connection-north"},
                )
                self.assertEqual(200, moved_status)
                self.assertIn("Corridor", moved["text"])
            else:
                rejoined_status, rejoined = self.request(
                    self.token, "/api/v1/join", {"player": "Alice"},
                )
                self.assertEqual(200, rejoined_status)
                self.assertGreater(rejoined["room_entry_cursor"], joined["room_entry_cursor"])
        finally:
            allow_send.set()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(1, len(old_result))
        old_status, old_body = old_result[0]
        self.assertEqual(409, old_status, "Stale prior-room feed must require a fresh read")
        self.assertNotIn(secret, json.dumps(old_body))
        # New room may be read by the same token after movement, but this
        # does not settle the separate issue of same-token session exclusivity.
        new_status, new_body = self.request(
            self.token, "/api/v1/feed?player=Alice&after=0"
        )
        self.assertEqual(200, new_status)
        self.assertNotIn(secret, json.dumps(new_body))


class ScopedSameActorFeedResponseEpochTest(SameActorFeedResponseEpochTest):
    """Same sequence, but with a world-scoped bearer and writer-capability checks."""
    scoped = True


if __name__ == "__main__":
    unittest.main()
