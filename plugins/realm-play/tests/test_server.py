"""Plugin launcher integration against the separately developed REALM MCP backend.

These are deliberately cross-PR tests. Run with REALM_PR52_ROOT set to a
checkout containing src/realm/mcp_server.py until PR #52 is integrated.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ.get("REALM_PR52_ROOT", str(ROOT))).resolve()
SERVER = ROOT / "plugins" / "realm-play" / "server" / "realm_mcp.py"

if (BACKEND_ROOT / "src" / "realm" / "mcp_server.py").is_file():
    sys.path.insert(0, str(BACKEND_ROOT / "src"))
    from realm.gateway import Gateway
else:
    Gateway = None


def request(number, method, params=None):
    message = {"jsonrpc": "2.0", "id": number, "method": method}
    if params is not None:
        message["params"] = params
    return message


def invoke(name, arguments):
    return {"name": name, "arguments": arguments}


class RealmPluginLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Gateway is None:
            raise RuntimeError(
                "REALM MCP backend is absent: check out PR #52 or set "
                "REALM_PR52_ROOT to its exact source. Integration tests "
                "must not silently pass by skipping gameplay."
            )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="realm-play-bridge-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "world.sqlite3"
        self.host = Gateway(self.path, world_id="sandbox")
        self.token = self.host.credentials.provision("Moss")
        self.host.credentials.provision("Ember")
        self.host.join("Ember")

    def run_messages(self, messages, *, token=None, world_id="sandbox",
                     database=None, backend_root=None, unset_credentials=False):
        env = os.environ.copy()
        for key in ("REALM_DB_PATH", "REALM_WORLD_ID", "REALM_PLAYER_TOKEN",
                    "REALM_DB", "REALM_ACTOR", "PYTHONPATH"):
            env.pop(key, None)
        env["REALM_ROOT"] = str(backend_root or BACKEND_ROOT)
        env["REALM_DB_PATH"] = str(database or self.path)
        env["REALM_WORLD_ID"] = world_id
        if not unset_credentials:
            env["REALM_PLAYER_TOKEN"] = token or self.token
        result = subprocess.run(
            [sys.executable, "-B", str(SERVER)],
            input="".join(json.dumps(item) + "\n" for item in messages),
            text=True, capture_output=True, env=env, timeout=20,
            cwd=ROOT, check=False,
        )
        replies = [json.loads(row) for row in result.stdout.splitlines()]
        self.assertNotIn(self.token, result.stdout + result.stderr)
        return result, replies

    def count_events(self, actor, kind):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT COUNT(*) FROM events WHERE actor=? AND kind=?",
                (actor, kind)).fetchone()[0]

    def test_single_backend_handshake_inventory_take_move_say_and_replay(self):
        messages = [
            request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            request(2, "tools/list"),
            request(3, "tools/call", invoke("realm_status", {})),
            request(4, "tools/call", invoke("realm_join", {})),
            request(5, "tools/call", invoke("realm_look", {})),
            request(6, "tools/call", invoke(
                "realm_command", {"command": "take token", "action_id": "take-1"})),
            request(7, "tools/call", invoke(
                "realm_command", {"command": "take token", "action_id": "take-1"})),
            request(8, "tools/call", invoke("realm_inventory", {})),
            request(9, "tools/call", invoke(
                "realm_move", {"direction": "north", "action_id": "move-1"})),
            request(10, "tools/call", invoke(
                "realm_move", {"direction": "north", "action_id": "move-1"})),
            request(11, "tools/call", invoke(
                "realm_say", {"message": "Hello from the plugin!", "action_id": "say-1"})),
            request(12, "tools/call", invoke(
                "realm_say", {"message": "Hello from the plugin!", "action_id": "say-1"})),
            request(13, "tools/call", invoke("realm_look", {})),
        ]
        process, replies = self.run_messages(messages)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual(list(range(1, 14)), [reply["id"] for reply in replies])
        self.assertEqual("realm-local", replies[0]["result"]["serverInfo"]["name"])
        tools = {item["name"] for item in replies[1]["result"]["tools"]}
        self.assertEqual({
            "realm_status", "realm_join", "realm_look", "realm_inventory",
            "realm_move", "realm_command", "realm_say", "realm_feed",
            "realm_map"}, tools)
        self.assertEqual("sandbox", replies[2]["result"]["structuredContent"]["world_id"])
        self.assertEqual("Moss", replies[2]["result"]["structuredContent"]["player"])
        self.assertIn("The Atrium", replies[4]["result"]["content"][0]["text"])
        self.assertIn("You take Brass token", replies[5]["result"]["content"][0]["text"])
        self.assertEqual(replies[5]["result"], replies[6]["result"])
        self.assertIn("Brass token", replies[7]["result"]["content"][0]["text"])
        self.assertIn("Corridor", replies[8]["result"]["content"][0]["text"])
        self.assertEqual(replies[8]["result"], replies[9]["result"])
        self.assertIn("You say", replies[10]["result"]["content"][0]["text"])
        self.assertEqual(replies[10]["result"], replies[11]["result"])
        self.assertIn("The Corridor", replies[12]["result"]["content"][0]["text"])
        self.assertEqual(1, self.count_events("Moss", "take"))
        self.assertEqual(1, self.count_events("Moss", "move"))
        self.assertEqual(1, self.count_events("Moss", "say"))
        self.assertEqual(0, self.count_events("Ember", "move"))
        self.assertEqual(0, self.count_events("Ember", "say"))

    def test_tool_cannot_rebind_actor_or_world(self):
        process, replies = self.run_messages([
            request(1, "tools/call", invoke("realm_join", {"player": "Ember"})),
            request(2, "tools/call", invoke(
                "realm_command", {"command": "north", "action_id": "a",
                                  "player": "Ember"})),
            request(3, "tools/call", invoke("realm_status", {"world_id": "other"})),
            request(4, "tools/call", invoke("realm_move", {"direction": "north"})),
            request(5, "tools/call", invoke("realm_say", {"message": "No ID"})),
        ])
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertTrue(all(reply["result"]["isError"] for reply in replies))
        self.assertEqual(0, self.count_events("Moss", "move"))
        self.assertEqual(0, self.count_events("Ember", "move"))

    def test_wrong_world_preflight_rejected_before_mutation(self):
        process, replies = self.run_messages(
            [request(1, "tools/list")], world_id="otherworld")
        self.assertEqual(2, process.returncode)
        self.assertEqual([], replies)
        self.assertIn("requires an existing bound world", process.stderr)
        self.assertEqual(0, self.count_events("Moss", "move"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual("sandbox", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])

    def test_revoked_player_and_missing_world_are_rejected(self):
        self.host.credentials.revoke("Moss")
        process, replies = self.run_messages([request(1, "tools/list")])
        self.assertEqual(2, process.returncode)
        self.assertEqual([], replies)
        missing = self.path.parent / "not-a-world.sqlite3"
        process, replies = self.run_messages(
            [request(1, "tools/list")], database=missing)
        self.assertEqual(2, process.returncode)
        self.assertEqual([], replies)
        self.assertFalse(missing.exists())

    def test_bad_root_and_missing_host_token_fail_closed(self):
        process, replies = self.run_messages(
            [request(1, "tools/list")], backend_root=self.path.parent)
        self.assertEqual(2, process.returncode)
        self.assertEqual([], replies)
        self.assertIn("REALM_ROOT", process.stderr)
        process, replies = self.run_messages(
            [request(1, "tools/list")], unset_credentials=True)
        self.assertEqual(2, process.returncode)
        self.assertEqual([], replies)
        self.assertIn("requires an existing bound world", process.stderr)


if __name__ == "__main__":
    unittest.main()
