"""Real loopback tests for authenticated admission quotas, not public DoS proof."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib import error, request
import json
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm import gateway as gateway_module
from realm.event_stream import FeedWaitServer
from realm.gateway import Gateway, GatewayHandler, GatewayServer


class GatewayRateLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "world.sqlite3", world_id="quota")
        self.tokens = {actor: self.gateway.credentials.provision(actor)
                       for actor in ("Patrick", "Vera")}

    def serve(self, *, rate=1, burst=2, max_principals=8, wait=False):
        if wait:
            server = FeedWaitServer(self.gateway, ("http://127.0.0.1:8000",), port=0)
            server._request_limiter = gateway_module._PrincipalRateLimiter(
                rate, burst, max_principals)
        else:
            server = GatewayServer(
                self.gateway, ("http://127.0.0.1:8000",), port=0,
                rate_limit_requests_per_minute=rate,
                rate_limit_burst=burst,
                rate_limit_max_principals=max_principals,
            )
        worker = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()

        def stop():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

        self.addCleanup(stop)
        return server, f"http://127.0.0.1:{server.server_port}"

    def send(self, base, path, *, actor="Patrick", token=None,
             origin=None, data=None, extra_headers=None):
        headers = dict(extra_headers or {})
        if actor is not None:
            headers["Authorization"] = "Bearer " + (token or self.tokens[actor])
        if origin is not None:
            headers["Origin"] = origin
        payload = json.dumps(data).encode() if data is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(base + path, headers=headers, data=payload)
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read()), response.headers
        except error.HTTPError as exc:
            with closing(exc):
                return exc.code, json.loads(exc.read()), exc.headers

    def test_quota_returns_429_retry_after_and_isolates_players(self):
        server, base = self.serve()
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        self.assertEqual(200, self.send(base, "/api/v1/room")[0])
        status, body, headers = self.send(base, "/api/v1/room")
        self.assertEqual(429, status)
        self.assertEqual("Rate limit exceeded", body["error"])
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        self.assertEqual("close", headers.get("Connection"))
        self.assertNotIn(self.tokens["Patrick"], str(body))
        self.assertLessEqual(len(server._request_limiter._entries), 8)
        self.assertEqual(200, self.send(base, "/api/v1/join", actor="Vera", data={"player": "Vera"})[0])

    def test_default_loopback_rejects_authenticated_request_flood(self):
        server = GatewayServer(self.gateway, (), port=0)
        worker = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()

        def stop():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

        self.addCleanup(stop)
        base = f"http://127.0.0.1:{server.server_port}"
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        statuses = [self.send(base, "/api/v1/room")[0] for _ in range(90)]
        self.assertIn(429, statuses)

    def test_bad_credentials_and_forbidden_origin_precede_throttling(self):
        _, base = self.serve(burst=1)
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        self.assertEqual(429, self.send(base, "/api/v1/room")[0])
        self.assertEqual(401, self.send(base, "/api/v1/room", actor=None)[0])
        self.assertEqual(401, self.send(base, "/api/v1/room", token="wrong-token")[0])
        self.assertEqual(403, self.send(base, "/api/v1/room", origin="https://evil.invalid")[0])
        self.assertEqual(200, self.send(base, "/api/v1/health", actor=None)[0])

    def test_over_quota_post_cannot_mutate_world(self):
        _, base = self.serve(burst=2)
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        self.assertEqual(200, self.send(base, "/api/v1/room")[0])
        status, _, _ = self.send(base, "/api/v1/command", data={
            "player": "Patrick", "command": "say Forbidden quota effect", "action_id": "limit-one",
        })
        self.assertEqual(429, status)
        with closing(self.gateway.engine._connect()) as db:
            count = db.execute("SELECT COUNT(*) FROM events WHERE kind='say'").fetchone()[0]
        self.assertEqual(0, count)

    def test_longpoll_consumes_one_admission_and_denies_over_quota(self):
        _, base = self.serve(wait=True, burst=2)
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        self.assertEqual(200, self.send(base, "/api/v1/feed/wait?after=0&wait_ms=0")[0])
        self.assertEqual(429, self.send(base, "/api/v1/feed/wait?after=0&wait_ms=5000")[0])

    def test_parallel_admission_has_shared_bounded_bucket(self):
        _, base = self.serve(burst=6)
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(lambda _: self.send(base, "/api/v1/room")[0], range(12)))
        self.assertEqual(5, statuses.count(200))
        self.assertEqual(7, statuses.count(429))

    def test_bearer_rotation_or_spoofed_forwarded_ip_cannot_reset_bucket(self):
        _, base = self.serve(burst=1)
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        self.tokens["Patrick"] = self.gateway.credentials.rotate("Patrick")
        self.assertEqual(429, self.send(base, "/api/v1/room",
                                        extra_headers={"X-Forwarded-For": "198.51.100.4"})[0])

    def test_bound_exhaustion_fails_closed_until_idle_buckets_expire(self):
        limiter = gateway_module._PrincipalRateLimiter(60, 2, 2)
        with patch.object(gateway_module.time, "monotonic", return_value=0.0):
            self.assertTrue(limiter.allow("Patrick")[0])
            self.assertTrue(limiter.allow("Vera")[0])
            self.assertEqual((False, 60), limiter.allow("Milo"))
            self.assertEqual(2, len(limiter._entries))
        with patch.object(gateway_module.time, "monotonic", return_value=60.0):
            self.assertEqual((True, 0), limiter.allow("Milo"))
            self.assertEqual(1, len(limiter._entries))

    def test_principal_case_variants_share_one_bucket_and_replenish(self):
        limiter = gateway_module._PrincipalRateLimiter(60, 2, 2)
        with patch.object(gateway_module.time, "monotonic", return_value=0.0):
            self.assertEqual((True, 0), limiter.allow("Patrick"))
            self.assertEqual((True, 0), limiter.allow("PATRICK"))
            self.assertEqual((False, 1), limiter.allow("patrick"))
            self.assertEqual(1, len(limiter._entries))
        with patch.object(gateway_module.time, "monotonic", return_value=1.1):
            self.assertEqual((True, 0), limiter.allow("pAtRiCk"))

    def test_rejects_invalid_configuration_without_starting_listener(self):
        for args in ((0, 2, 3), (120, 0, 3), (120, 2, 0),
                     (True, 2, 3), (120, 4097, 3), (120, 2, 1025)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                gateway_module._PrincipalRateLimiter(*args)

    def test_existing_send_override_still_receives_429(self):
        class ExistingHandler(GatewayHandler):
            def _send(self, status, obj):
                return super()._send(status, obj)

        server, base = self.serve(burst=1)
        server.RequestHandlerClass = ExistingHandler
        self.assertEqual(200, self.send(base, "/api/v1/join", data={"player": "Patrick"})[0])
        self.assertEqual(429, self.send(base, "/api/v1/room")[0])


if __name__ == "__main__":
    unittest.main()
