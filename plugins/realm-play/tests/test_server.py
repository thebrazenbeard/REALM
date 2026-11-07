"""Real disposable REALM engine integration for the local MCP stdio server."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from realm.engine import Engine

SERVER = ROOT / "plugins" / "realm-play" / "server" / "realm_mcp.py"


def request(number, method, params=None):
    data = {"jsonrpc": "2.0", "id": number, "method": method}
    if params is not None:
        data["params"] = params
    return data


class RealmMCPToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="realm-mcp-local-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "world.sqlite3"
        engine = Engine(self.path)
        engine.join("Moss")
        engine.join("Ember")

    def run_messages(self, messages, *, actor="Moss", path=None, source_root=None):
        env = os.environ.copy()
        env["REALM_DB"] = str(path or self.path)
        env["REALM_ACTOR"] = actor
        env.pop("REALM_ROOT", None)
        if source_root is not None:
            env["REALM_ROOT"] = str(source_root)
        encoded = "".join(json.dumps(m) + "\n" for m in messages)
        result = subprocess.run([sys.executable, "-B", str(SERVER)],
                                input=encoded, text=True, capture_output=True,
                                env=env, timeout=10, cwd=ROOT, check=False)
        output = [json.loads(line) for line in result.stdout.splitlines()]
        return result, output

    def count_events(self, actor, kind):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM events WHERE actor=? AND kind=?",
                              (actor, kind)).fetchone()[0]

    def test_mcp_handshake_tools_and_real_actor_scoped_gameplay(self):
        messages = [
            request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            request(2, "tools/list"),
            request(3, "tools/call", {"name": "realm_look", "arguments": {}}),
            request(4, "tools/call", {"name": "realm_move", "arguments": {"direction": "north"}}),
            request(5, "tools/call", {"name": "realm_say", "arguments": {"text": "Hello, world."}}),
            request(6, "tools/call", {"name": "realm_inventory", "arguments": {}}),
        ]
        process, replies = self.run_messages(messages)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual([1, 2, 3, 4, 5, 6], [r["id"] for r in replies])
        self.assertEqual("realm-play-local", replies[0]["result"]["serverInfo"]["name"])
        self.assertEqual({"realm_look", "realm_inventory", "realm_move", "realm_say"},
                         {tool["name"] for tool in replies[1]["result"]["tools"]})
        self.assertIn("The Atrium", replies[2]["result"]["content"][0]["text"])
        self.assertIn("The Corridor", replies[3]["result"]["content"][0]["text"])
        self.assertIn("You say", replies[4]["result"]["content"][0]["text"])
        self.assertIn("You carry", replies[5]["result"]["content"][0]["text"])
        self.assertEqual(1, self.count_events("Moss", "move"))
        self.assertEqual(1, self.count_events("Moss", "say"))
        self.assertEqual(0, self.count_events("Ember", "move"))
        self.assertEqual(0, self.count_events("Ember", "say"))
        with sqlite3.connect(self.path) as db:
            self.assertEqual("corridor", db.execute("SELECT room_id FROM players WHERE id='Moss'").fetchone()[0])
            self.assertEqual("atrium", db.execute("SELECT room_id FROM players WHERE id='Ember'").fetchone()[0])

    def test_injected_actor_command_and_controls_are_rejected(self):
        messages = [
            request(1, "tools/call", {"name": "realm_move", "arguments":
                    {"direction": "north", "actor": "Ember"}}),
            request(2, "tools/call", {"name": "realm_move", "arguments":
                    {"direction": "north; say Hacked"}}),
            request(3, "tools/call", {"name": "realm_say", "arguments":
                    {"text": "Message\nwith newline"}}),
            request(4, "tools/call", {"name": "realm_look", "arguments":
                    {"db": "other.sqlite3"}}),
            request(5, "tools/call", {"name": "unknown", "arguments": {}}),
            request(6, "ping"),
        ]
        process, replies = self.run_messages(messages)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertTrue(all(reply["result"]["isError"] for reply in replies[:5]))
        self.assertEqual({}, replies[5]["result"])
        self.assertEqual(0, self.count_events("Moss", "move"))
        self.assertEqual(0, self.count_events("Moss", "say"))

    def test_unjoined_actor_and_nonexistent_world_fail_before_mutation(self):
        process, messages = self.run_messages([request(1, "tools/list")], actor="NotJoined")
        self.assertEqual(2, process.returncode)
        self.assertEqual([], messages)
        self.assertIn("already be joined", process.stderr)
        absent = self.path.parent / "not-created.sqlite3"
        process2, messages2 = self.run_messages([request(1, "tools/list")], path=absent)
        self.assertEqual(2, process2.returncode)
        self.assertEqual([], messages2)
        self.assertFalse(absent.exists())

    def test_explicit_source_root_for_installed_plugin_and_invalid_root(self):
        process, replies = self.run_messages([request(1, "tools/list")], source_root=ROOT)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual("realm_move", replies[0]["result"]["tools"][2]["name"])
        process, replies = self.run_messages([request(1, "tools/list")],
                                             source_root=self.path.parent)
        self.assertEqual(2, process.returncode)
        self.assertEqual([], replies)
        self.assertIn("REALM_ROOT", process.stderr)


if __name__ == "__main__":
    unittest.main()
