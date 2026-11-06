"""Real entrypoint tests: terminal subprocess, HTTP commands, and event feed."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer


class EntrypointCraftingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "world.sqlite3"
        self.gateway = Gateway(self.db)
        self.gateway.router.seed_demo_crafting()
        self.server = GatewayServer(self.gateway, "local-test-token", (), port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _http(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer local-test-token"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(self.base + path, data=data, headers=headers)
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_http_gather_craft_and_visible_feed(self):
        self.assertEqual(200, self._http("/api/v1/join", {"player": "Patrick"})[0])
        self.assertEqual(200, self._http("/api/v1/join", {"player": "Vera"})[0])
        _, before = self._http("/api/v1/room?player=Vera")
        after = before["cursor"]
        request_body = {"player": "Patrick", "command": "gather stone 2", "action_id": "gather-http-1"}
        status, gathered = self._http("/api/v1/command", request_body)
        self.assertEqual(200, status)
        self.assertIn("stone x6", gathered["text"])
        self.assertEqual(gathered, self._http("/api/v1/command", request_body)[1])
        status, crafted = self._http("/api/v1/command", {
            "player": "Patrick", "command": "craft field knife", "action_id": "craft-http-1",
        })
        self.assertEqual(200, status)
        self.assertIn("field knife x1", crafted["text"])
        status, pouch = self._http("/api/v1/command", {"player": "Patrick", "command": "resources"})
        self.assertEqual(200, status)
        self.assertIn("stone x4", pouch["text"])
        self.assertIn("field knife x1", pouch["text"])
        status, feed = self._http("/api/v1/feed?" + parse.urlencode({"player": "Vera", "after": after}))
        self.assertEqual(200, status)
        self.assertEqual(1, sum(e["kind"] == "gather" for e in feed["events"]))
        self.assertTrue(any("gathered stone x6" in e["text"] for e in feed["events"]))
        self.assertTrue(any("crafted field knife" in e["text"] for e in feed["events"]))

    def test_http_build_and_replay_collision_with_core_command(self):
        self._http("/api/v1/join", {"player": "Patrick"})
        self._http("/api/v1/command", {"player": "Patrick", "command": "gather stone"})
        self._http("/api/v1/command", {"player": "Patrick", "command": "north"})
        self._http("/api/v1/command", {"player": "Patrick", "command": "gather wood"})
        status, made = self._http("/api/v1/command", {
            "player": "Patrick", "command": "build supply cache", "action_id": "build-1",
        })
        self.assertEqual(200, status)
        self.assertIn("build supply cache", made["text"])
        self.assertIn("supply cache (Patrick)", self._http("/api/v1/command", {
            "player": "Patrick", "command": "structures",
        })[1]["text"])

        status, wrong = self._http("/api/v1/command", {
            "player": "Patrick", "command": "say not a repeat", "action_id": "build-1",
        })
        self.assertEqual(400, status)
        self.assertIn("different command", wrong["error"])

    def test_cli_once_two_processes_share_crafting_state(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "src"))

        def run(command, demo=False):
            args = [sys.executable, "-m", "realm", "--db", str(self.db), "--player", "CliPlayer", "--once", command]
            if demo:
                args.append("--demo-crafting")
            result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=8)
            self.assertEqual(0, result.returncode, result.stderr)
            return result.stdout

        self.assertIn("stone x3", run("gather stone", demo=True))
        self.assertIn("field knife x1", run("craft field knife"))
        self.assertIn("field knife x1", run("resources"))
        self.assertIn("Brass token", run("look"))


if __name__ == "__main__":
    unittest.main()
