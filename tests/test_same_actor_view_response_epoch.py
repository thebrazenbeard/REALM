"""Falsify stale /room and /scene after a second same-token client moves/rejoins.

Physical loopback HTTP clients, world-scoped or legacy bearer, disposable
SQLite. The trusted host has not decided exclusive player-session semantics.
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


class ViewEpochTest(unittest.TestCase):
    scoped = False

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gateway = Gateway(
            Path(tmp.name) / "test.sqlite3",
            world_id="same_actor_room_view",
            scoped_credentials=self.scoped,
            scoped_guard_path=Path(tmp.name) / "scoped-guard.json",
        )
        store = self.gateway.scoped_credentials if self.scoped else self.gateway.credentials
        self.token = store.provision("Alice")
        self.server = GatewayServer(self.gateway, (), port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.stop)
        self.assertEqual(200, self.http("/api/v1/join", {"player": "Alice"})[0])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def http(self, path, body=None):
        headers = {"Authorization": "Bearer " + self.token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            headers=headers, data=data,
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def _exercise(self, endpoint, move):
        built = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        view_fn = getattr(self.gateway, endpoint)
        old_view = []
        def pause_after_snapshot(*args, **kwargs):
            result = view_fn(*args, **kwargs)
            if not built.is_set():
                old_view.append(result)
                built.set()
                if not release.wait(4):
                    raise RuntimeError("view snapshot was never released")
            return result
        setattr(self.gateway, endpoint, pause_after_snapshot)

        response = []
        client = threading.Thread(
            target=lambda: response.append(self.http("/api/v1/" + endpoint)),
            daemon=True,
        )
        client.start()
        try:
            self.assertTrue(built.wait(3))
            self.assertEqual(1, len(old_view))
            self.assertIn("Atrium", json.dumps(old_view[0]))
            # A SECOND HTTP connection with SAME ACTOR bearer performs
            # an authorized move or a new same-room arrival.
            if move:
                status, changed = self.http("/api/v1/command", {
                    "player": "Alice",
                    "command": "north",
                    "action_id": "racing-second-client-move",
                })
                self.assertIn("Corridor", changed["text"])
            else:
                status, changed = self.http("/api/v1/join", {"player": "Alice"})
                self.assertTrue(changed["room_entry_cursor"])
            self.assertEqual(200, status)
        finally:
            release.set()
            client.join(5)
        self.assertFalse(client.is_alive())
        self.assertEqual(1, len(response))
        code, payload = response[0]
        self.assertEqual(409, code, "Stale view must be discarded after newer actor arrival")
        self.assertNotIn("Atrium", json.dumps(payload))
        code, current = self.http("/api/v1/" + endpoint)
        self.assertEqual(200, code)
        if move:
            self.assertIn("Corridor", json.dumps(current))
            self.assertNotIn("Atrium", json.dumps(current))
        else:
            self.assertIn("Atrium", json.dumps(current))

    def test_room_moves_between_arrival_precheck_and_look(self):
        # Gateway.room is assembled from more than one database snapshot.
        # A second bearer connection must not let a pending look response
        # mix a new room with an older recorded arrival epoch.
        look_started = threading.Event()
        release_look = threading.Event()
        self.addCleanup(release_look.set)
        original_look = self.gateway.engine.act

        def held_look(player, command, *args, **kwargs):
            if player == "Alice" and command == "look" and not look_started.is_set():
                look_started.set()
                if not release_look.wait(4):
                    raise RuntimeError("Look was not released")
            return original_look(player, command, *args, **kwargs)

        self.gateway.engine.act = held_look
        responses = []
        old = threading.Thread(
            target=lambda: responses.append(self.http("/api/v1/room")),
            daemon=True,
        )
        old.start()
        try:
            self.assertTrue(look_started.wait(3))
            status, changed = self.http("/api/v1/command", {
                "player": "Alice", "command": "north",
                "action_id": "race-mid-look-move",
            })
            self.assertEqual(200, status)
            self.assertIn("Corridor", changed["text"])
        finally:
            release_look.set()
            old.join(5)
        self.assertFalse(old.is_alive())
        self.assertEqual(1, len(responses))
        status, payload = responses[0]
        self.assertEqual(409, status)
        self.assertNotIn("Corridor", json.dumps(payload))
        self.assertEqual(200, self.http("/api/v1/room")[0])

    def test_room_move(self):
        self._exercise("room", True)

    def test_room_rejoin(self):
        self._exercise("room", False)

    def test_scene_move(self):
        self._exercise("scene", True)

    def test_scene_rejoin(self):
        self._exercise("scene", False)


class ScopedViewEpochTest(ViewEpochTest):
    scoped = True


if __name__ == "__main__":
    unittest.main()
