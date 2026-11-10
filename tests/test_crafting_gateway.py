"""Real entrypoint tests: terminal subprocess, HTTP commands, and event feed."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
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
        self.player_tokens = {
            name: self.gateway.credentials.provision(name)
            for name in ("Patrick", "Vera")
        }
        self.server = GatewayServer(self.gateway, (), port=0)
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

    def _http(self, path, body=None, *, as_player="Patrick"):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self.player_tokens[as_player]}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(self.base + path, data=data, headers=headers)
        try:
            with request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            with closing(exc):
                return exc.code, json.loads(exc.read())

    def test_http_gather_craft_and_visible_feed(self):
        self.assertEqual(200, self._http("/api/v1/join", {"player": "Patrick"})[0])
        self.assertEqual(200, self._http("/api/v1/join", {"player": "Vera"}, as_player="Vera")[0])
        _, before = self._http("/api/v1/room?player=Vera", as_player="Vera")
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
        status, feed = self._http("/api/v1/feed?" + parse.urlencode({"player": "Vera", "after": after}), as_player="Vera")
        self.assertEqual(200, status)
        self.assertEqual(1, sum(e["kind"] == "gather" for e in feed["events"]))
        self.assertTrue(any("gathered stone x6" in e["text"] for e in feed["events"]))
        self.assertTrue(any("crafted field knife" in e["text"] for e in feed["events"]))
        # A valid Vera token cannot impersonate Patrick, even when crafting.
        status, _ = self._http("/api/v1/command", request_body, as_player="Vera")
        self.assertEqual(403, status)

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

    def test_router_keeps_crafting_reads_fresh_and_core_mutations_once_only(self):
        """HTTP must route craft verbs before Engine can see shared action IDs."""
        self.assertEqual(200, self._http("/api/v1/join", {"player": "Patrick"})[0])
        self.assertEqual(200, self._http("/api/v1/join", {"player": "Vera"}, as_player="Vera")[0])

        read = {"player": "Patrick", "command": "gather", "action_id": "read-gather-1"}
        status, first = self._http("/api/v1/command", read)
        self.assertEqual(200, status)
        self.assertIn("stone (40)", first["text"])
        self.assertEqual(200, self._http("/api/v1/command", {
            "player": "Vera", "command": "gather stone", "action_id": "vera-gather-1",
        }, as_player="Vera")[0])
        status, newer = self._http("/api/v1/command", read)
        self.assertEqual(200, status)
        self.assertIn("stone (37)", newer["text"])
        with closing(sqlite3.connect(self.db)) as db:
            receipt = db.execute(
                "SELECT command,response FROM action_receipts "
                "WHERE actor='Patrick' AND action_id='read-gather-1'"
            ).fetchone()
        self.assertEqual(("gather", ""), receipt)

        status, mismatch = self._http("/api/v1/command", {
            "player": "Patrick", "command": "resources", "action_id": "read-gather-1",
        })
        self.assertEqual(400, status)
        self.assertIn("different command", mismatch["error"])

        said = {"player": "Patrick", "command": "say One event only", "action_id": "say-once-1"}
        status, original = self._http("/api/v1/command", said)
        self.assertEqual(200, status)
        status, repeated = self._http("/api/v1/command", said)
        self.assertEqual(200, status)
        self.assertEqual(original["text"], repeated["text"])
        with closing(sqlite3.connect(self.db)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM events WHERE actor='Patrick' AND kind='say'"
            ).fetchone()[0]
        self.assertEqual(1, count, "mutating core replay must not append speech")
        status, reverse_collision = self._http("/api/v1/command", {
            "player": "Patrick", "command": "resources", "action_id": "say-once-1",
        })
        self.assertEqual(400, status)
        self.assertIn("different command", reverse_collision["error"])

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
