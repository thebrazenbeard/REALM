"""Authenticated, bounded long-poll feed integration on localhost."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib import error, parse, request
import json
import tempfile
import threading
import time
import unittest

from realm.event_stream import FeedWaitServer
from realm.gateway import Gateway


class FeedWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "world.sqlite3"
        self.gateway = Gateway(self.db, world_id="one")
        self.tokens = {
            name: self.gateway.credentials.provision(name)
            for name in ("Patrick", "Vera")
        }
        self.gateway.join("Patrick")
        self.gateway.join("Vera")
        self.server = FeedWaitServer(self.gateway, ("http://127.0.0.1:8000",), port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def send(self, path, *, token="Patrick", origin=None):
        url = f"http://127.0.0.1:{self.server.server_port}{path}"
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + self.tokens.get(token, token)
        if origin is not None:
            headers["Origin"] = origin
        req = request.Request(url, headers=headers)
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            with closing(exc):
                return exc.code, json.loads(exc.read())

    def url(self, *, after=0, wait_ms=200, player=None):
        params = {"after": str(after), "wait_ms": str(wait_ms)}
        if player is not None:
            params["player"] = player
        return "/api/v1/feed/wait?" + parse.urlencode(params)

    def test_empty_timeout_preserves_cursor(self):
        after = self.gateway.cursor()
        start = time.monotonic()
        status, body = self.send(self.url(after=after, wait_ms=180))
        self.assertEqual(200, status)
        self.assertEqual([], body["events"])
        self.assertEqual(after, body["cursor"])
        self.assertGreaterEqual(time.monotonic() - start, 0.13)
        self.assertLess(time.monotonic() - start, 2)

    def test_two_clients_receive_same_authorized_event(self):
        after_p = self.gateway.cursor()
        after_v = self.gateway.cursor()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.send, self.url(after=after_p, wait_ms=1500), token="Patrick")
            second = pool.submit(self.send, self.url(after=after_v, wait_ms=1500), token="Vera")
            time.sleep(0.15)
            self.gateway.command("Patrick", "say A synthetic hello.", "say-001")
            a_status, a = first.result(timeout=3)
            b_status, b = second.result(timeout=3)
        self.assertEqual((200, 200), (a_status, b_status))
        self.assertTrue(any("synthetic hello" in e["text"] for e in a["events"]))
        self.assertTrue(any("synthetic hello" in e["text"] for e in b["events"]))
        self.assertTrue(all(e["id"] > after_p for e in a["events"]))
        self.assertTrue(all(e["id"] > after_v for e in b["events"]))

    def test_player_token_cross_scope_and_origin_rejected(self):
        status, _ = self.send(self.url(player="Vera"), token="Patrick")
        self.assertEqual(403, status)
        status, _ = self.send(self.url(), token=None)
        self.assertEqual(401, status)
        status, _ = self.send(self.url(), token="bad-credential")
        self.assertEqual(401, status)
        status, _ = self.send(self.url(), origin="https://evil.example")
        self.assertEqual(403, status)

    def test_revoke_during_wait_never_delivers(self):
        cursor = self.gateway.cursor()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.send, self.url(after=cursor, wait_ms=1500))
            time.sleep(0.16)
            self.gateway.credentials.revoke("Patrick")
            self.gateway.command("Vera", "say Private new words.", "say-002")
            status, body = future.result(timeout=4)
        self.assertEqual(401, status)
        self.assertFalse(body["ok"])

    def test_invalid_cursors_and_waits_are_rejected(self):
        for path in (
            "/api/v1/feed/wait?after=-1",
            "/api/v1/feed/wait?after=99999999999999999999999999999999",
            "/api/v1/feed/wait?after=1&after=2",
            "/api/v1/feed/wait?wait_ms=50000",
            "/api/v1/feed/wait?wait_ms=nan",
            "/api/v1/feed/wait?unknown=1",
        ):
            with self.subTest(path=path):
                status, _ = self.send(path)
                self.assertEqual(400, status)

    def test_shutdown_interrupts_pending_wait(self):
        cursor = self.gateway.cursor()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.send, self.url(after=cursor, wait_ms=4000))
            time.sleep(0.15)
            start = time.monotonic()
            self.server.shutdown()
            status, _ = future.result(timeout=2)
        self.assertEqual(503, status)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_restart_preserves_personal_cursor_and_replay(self):
        after = self.gateway.cursor()
        self.gateway.command("Vera", "say restart witness", "say-003")
        first_status, first = self.send(self.url(after=after, wait_ms=0))
        self.assertEqual(200, first_status)
        self.assertTrue(first["events"])
        self._stop()
        next_gateway = Gateway(self.db, world_id="one")
        next_server = FeedWaitServer(next_gateway, (), port=0)
        self.addCleanup(next_server.server_close)
        thread = threading.Thread(
            target=next_server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (next_server.shutdown(), thread.join(timeout=2)))
        url = f"http://127.0.0.1:{next_server.server_port}{self.url(after=after, wait_ms=0)}"
        with request.urlopen(request.Request(url, headers={
            "Authorization": "Bearer " + self.tokens["Patrick"],
        }), timeout=3) as response:
            replay = json.loads(response.read())
        self.assertEqual(first["events"], replay["events"])

    def test_existing_routes_remain_available(self):
        status, body = self.send("/api/v1/feed?after=0")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])

    def test_legacy_feed_cannot_bypass_event_time_room_entry(self):
        self.gateway.command("Vera", "north", "legacy-vera-enters")
        self.gateway.command("Vera", "say Secret before Patrick enters", "legacy-before")
        self.gateway.command("Patrick", "north", "legacy-patrick-enters")
        for path in ("/api/v1/feed?after=0", self.url(after=0, wait_ms=0)):
            status, body = self.send(path)
            self.assertEqual(200, status)
            self.assertFalse(any("Secret before Patrick enters" in event["text"]
                                 for event in body["events"]))

        self.gateway.command("Patrick", "south", "legacy-patrick-exits")
        self.gateway.command("Vera", "say Secret while Patrick absent", "legacy-absent")
        self.gateway.command("Patrick", "north", "legacy-patrick-returns")
        for path in ("/api/v1/feed?after=0", self.url(after=0, wait_ms=0)):
            status, body = self.send(path)
            self.assertEqual(200, status)
            self.assertFalse(any("Secret while Patrick absent" in event["text"]
                                 for event in body["events"]))

    def test_room_isolation_even_with_cross_room_cursor(self):
        before = self.gateway.cursor()
        self.gateway.command("Vera", "north", "move-private")
        self.gateway.command("Vera", "say Private corridor message", "say-private")
        status, feed = self.send(self.url(after=before, wait_ms=110))
        self.assertEqual(200, status)
        self.assertFalse(any("Private corridor message" in e["text"] for e in feed["events"]))
        status, other = self.send(self.url(after=before, wait_ms=0), token="Vera")
        self.assertEqual(200, status)
        self.assertTrue(any("Private corridor message" in e["text"] for e in other["events"]))

    def test_does_not_replay_room_speech_before_player_first_enters(self):
        self.gateway.command("Vera", "north", "vera-enters")
        self.gateway.command("Vera", "say Before Patrick arrived", "before-enter")
        self.gateway.command("Patrick", "north", "patrick-enters")
        status, feed = self.send(self.url(after=0, wait_ms=0))
        self.assertEqual(200, status)
        self.assertFalse(any("Before Patrick arrived" in e["text"] for e in feed["events"]))
        self.assertTrue(any(e["actor"] == "Patrick" and e["kind"] == "move" for e in feed["events"]))

    def test_does_not_replay_absence_window_on_reentry(self):
        self.gateway.command("Vera", "north", "vera-enters")
        self.gateway.command("Patrick", "north", "patrick-enters")
        self.gateway.command("Patrick", "south", "patrick-exits")
        self.gateway.command("Vera", "say During Patrick absence", "during-absence")
        self.gateway.command("Patrick", "north", "patrick-reenters")
        status, feed = self.send(self.url(after=0, wait_ms=0))
        self.assertEqual(200, status)
        self.assertFalse(any("During Patrick absence" in e["text"] for e in feed["events"]))
        self.assertTrue(any(e["actor"] == "Patrick" and e["kind"] == "move" for e in feed["events"]))

    def test_absent_arrival_evidence_fails_closed(self):
        with closing(self.gateway.engine._connect()) as db, db:
            db.execute("DELETE FROM events WHERE actor='Patrick' AND kind='join'")
        status, result = self.send(self.url(after=0, wait_ms=0))
        self.assertEqual(400, status)
        self.assertIn("No provable current-room arrival", result["error"])

    def test_world_binding_stops_cross_world_replay(self):
        with self.assertRaisesRegex(ValueError, "different world_id"):
            Gateway(self.db, world_id="two")

    def test_bounded_waiters_reject_over_capacity(self):
        self.assertTrue(self.server.feed_slots.acquire(blocking=False))
        try:
            server_limit = self.server.feed_slots
            for _ in range(7):
                self.assertTrue(server_limit.acquire(blocking=False))
            try:
                status, result = self.send(self.url(wait_ms=10))
                self.assertEqual(429, status)
                self.assertFalse(result["ok"])
            finally:
                for _ in range(7):
                    server_limit.release()
        finally:
            self.server.feed_slots.release()

    def test_one_player_cannot_starve_another_waiter(self):
        cursor = self.gateway.cursor()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.send, self.url(after=cursor, wait_ms=1200))
            second = pool.submit(self.send, self.url(after=cursor, wait_ms=1200))
            time.sleep(0.18)
            with self.server.player_wait_lock:
                self.assertEqual(2, self.server.player_wait_counts.get("Patrick"))
            status, _ = self.send(self.url(after=cursor, wait_ms=50))
            self.assertEqual(429, status)
            # Vera must still receive an actual *waiting* slot, not just a
            # synchronous zero-wait bypass.
            status, response = self.send(self.url(after=cursor, wait_ms=90), token="Vera")
            self.assertEqual(200, status)
            self.assertEqual([], response["events"])
            self.assertEqual(200, first.result(timeout=2)[0])
            self.assertEqual(200, second.result(timeout=2)[0])
        self.assertEqual({}, self.server.player_wait_counts)

    def test_rapid_reconnect_releases_all_waiter_slots(self):
        cursor = self.gateway.cursor()
        for _ in range(20):
            status, response = self.send(self.url(after=cursor, wait_ms=1))
            self.assertEqual(200, status)
            self.assertEqual([], response["events"])
        self.assertEqual({}, self.server.player_wait_counts)
        self.assertEqual(8, self.server.feed_slots._value)

    def test_get_query_cannot_inject_gameplay_commands(self):
        before = self.gateway.cursor()
        status, _ = self.send("/api/v1/feed/wait?command=say+bad&wait_ms=0")
        self.assertEqual(400, status)
        self.assertEqual(before, self.gateway.cursor())

    def test_future_cursor_is_rejected_instead_of_starving(self):
        status, result = self.send(self.url(after=self.gateway.cursor() + 1000))
        self.assertEqual(400, status)
        self.assertFalse(result["ok"])

    def test_rotated_token_is_rejected_without_wait(self):
        previous = self.tokens["Patrick"]
        current = self.gateway.credentials.rotate("Patrick")
        self.tokens["Patrick"] = previous
        status, _ = self.send(self.url(wait_ms=0))
        self.assertEqual(401, status)
        self.tokens["Patrick"] = current
        status, _ = self.send(self.url(wait_ms=0))
        self.assertEqual(200, status)

    def test_replay_batch_is_bounded_and_cursor_monotonic(self):
        before = self.gateway.cursor()
        for number in range(103):
            self.gateway.command("Patrick", f"say Event {number}", f"batch-{number:03d}")
        first_status, first = self.send(self.url(after=before, wait_ms=0))
        self.assertEqual(200, first_status)
        self.assertEqual(100, len(first["events"]))
        ids = [item["id"] for item in first["events"]]
        self.assertEqual(sorted(ids), ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[-1], first["cursor"])
        next_status, next_batch = self.send(self.url(after=ids[-1], wait_ms=0))
        self.assertEqual(200, next_status)
        self.assertEqual(3, len(next_batch["events"]))
        self.assertTrue(all(e["id"] > ids[-1] for e in next_batch["events"]))

    def test_cross_world_token_does_not_authenticate(self):
        other = Gateway(Path(self.temp.name) / "other.sqlite3", world_id="two")
        other.join("Patrick")
        other_server = FeedWaitServer(other, (), port=0)
        thread = threading.Thread(target=other_server.serve_forever, daemon=True)
        thread.start()
        try:
            req = request.Request(
                f"http://127.0.0.1:{other_server.server_port}/api/v1/feed/wait?wait_ms=0",
                headers={"Authorization": "Bearer " + self.tokens["Patrick"]},
            )
            with self.assertRaises(error.HTTPError) as denied:
                request.urlopen(req, timeout=2)
            with closing(denied.exception):
                self.assertEqual(401, denied.exception.code)
        finally:
            other_server.shutdown()
            other_server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
