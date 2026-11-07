"""Disposable stdio and SQLite acceptance for WorkBridge-only REALM transport."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbridge_mcp_driver import BridgeError, invoke, _object  # noqa: E402


FAKE_MCP = '''import json, os, sys
from pathlib import Path
for line in sys.stdin:
    row = json.loads(line)
    ident = row.get("id")
    method = row.get("method")
    if method == "initialize":
        answer = {"serverInfo": {"name": "realm-play-local", "version": "0.1.0"}}
    elif method == "tools/list":
        answer = {"tools": [{"name": item} for item in
                  ("realm_look", "realm_inventory", "realm_move", "realm_say") ]}
    elif method == "tools/call":
        capture = Path(__file__ + ".calls")
        with capture.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"params": row["params"], "actor": os.environ["REALM_ACTOR"],
                                "db": os.environ["REALM_DB"]}) + "\\n")
        answer = {"content": [{"type": "text", "text": "The cobblestones are cold."}],
                  "isError": False}
    else:
        continue
    print(json.dumps({"jsonrpc":"2.0", "id":ident, "result":answer}), flush=True)
'''


class WorkBridgeDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.root = self.dir / "REALM"
        (self.root / "src" / "realm").mkdir(parents=True)
        (self.root / "src" / "realm" / "engine.py").write_text("# test stub\n")
        self.server = self.dir / "fake_mcp.py"
        self.server.write_text(FAKE_MCP, encoding="utf-8")
        self.db = self.dir / "world.sqlite3"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE world_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("CREATE TABLE players(id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO world_meta VALUES('world_id','testing')")
            db.execute("INSERT INTO players VALUES('Vera')")
        self.config = {
            "schema": "realm.workbridge-mcp-transport.v1",
            "python_executable": sys.executable,
            "realm_root": str(self.root),
            "server_script": str(self.server),
            "world_db": str(self.db),
            "expected_world_id": "testing", "actor": "Vera",
        }
        self.config_path = self.dir / "config.json"
        self._save_config()

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def _calls(self):
        path = Path(str(self.server) + ".calls")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_read_only_tool_uses_existing_actor_and_disposable_stdio_server(self):
        outcome = invoke(_object(self.config_path), {"tool": "realm_look", "arguments": {}})
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["mutating"])
        self.assertEqual(outcome["game_text_trust"], "UNTRUSTED_IN_WORLD_CONTENT")
        self.assertIn("cobblestones", outcome["game_text"])
        calls = self._calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["actor"], "Vera")
        self.assertEqual(calls[0]["db"], str(self.db))
        self.assertEqual(calls[0]["params"]["name"], "realm_look")

    def test_world_identity_mismatch_fails_before_server_launch(self):
        self.config["expected_world_id"] = "another"
        self._save_config()
        with self.assertRaises(BridgeError) as cm:
            invoke(_object(self.config_path), {"tool": "realm_look", "arguments": {}})
        self.assertEqual(cm.exception.code, "WORLD_IDENTITY_MISMATCH")
        self.assertEqual(self._calls(), [])

    def test_unjoined_actor_fails_before_server_launch(self):
        self.config["actor"] = "Patrick"
        self._save_config()
        with self.assertRaises(BridgeError) as cm:
            invoke(_object(self.config_path), {"tool": "realm_look", "arguments": {}})
        self.assertEqual(cm.exception.code, "ACTOR_NOT_JOINED")
        self.assertEqual(self._calls(), [])

    def test_mutation_contract_does_not_falsely_claim_replay_safety(self):
        result = invoke(_object(self.config_path), {"tool": "realm_say", "arguments": {"text": "Hello."}})
        self.assertTrue(result["mutating"])
        self.assertEqual(result["server_replay_contract"], "NOT_STABLE_ACTION_ID_QUALIFIED")
        self.assertEqual(len(self._calls()), 1)
        self.assertEqual(self._calls()[0]["params"]["arguments"], {"text": "Hello."})

    def test_unknown_tool_and_identity_injection_are_rejected_without_side_effect(self):
        for request in (
            {"tool": "realm_admin", "arguments": {}},
            {"tool": "realm_move", "arguments": {"direction": "north", "actor": "Patrick"}},
            {"tool": "realm_say", "arguments": {"text": "hello", "world_id": "another"}},
            {"tool": "realm_say", "arguments": {"text": "hi\nquit"}},
            {"tool": "realm_look", "arguments": {"command": "drop all"}},
        ):
            with self.subTest(request=request), self.assertRaises(BridgeError):
                invoke(_object(self.config_path), request)
        self.assertEqual(self._calls(), [])

    def test_cli_json_stdin_does_not_print_host_configuration(self):
        entry = Path(__file__).resolve().parents[1] / "workbridge_mcp_driver.py"
        outcome = subprocess.run([sys.executable, "-B", str(entry), "--config", str(self.config_path)],
                                 input=json.dumps({"tool": "realm_inventory", "arguments": {}}),
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        response = json.loads(outcome.stdout)
        self.assertTrue(response["ok"])
        self.assertNotIn(str(self.db), outcome.stdout)
        self.assertNotIn(str(self.root), outcome.stdout)
        self.assertEqual(len(self._calls()), 1)

    def test_cli_one_shot_base64url_request_for_workbridge(self):
        entry = Path(__file__).resolve().parents[1] / "workbridge_mcp_driver.py"
        raw = json.dumps({"tool": "realm_look", "arguments": {}}, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        outcome = subprocess.run([sys.executable, "-B", str(entry), "--config", str(self.config_path),
                                  "--request-b64", encoded],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertTrue(json.loads(outcome.stdout)["ok"])
        self.assertEqual(len(self._calls()), 1)

    def test_cli_rejects_invalid_base64url_without_tool_call(self):
        entry = Path(__file__).resolve().parents[1] / "workbridge_mcp_driver.py"
        for encoded in ("not!base64", "-" * 12000, "YQ"):
            with self.subTest(encoded=encoded[:20]):
                # The equals form is necessary for encoded strings beginning in '-':
                # argparse otherwise mistakes the value for a new option.
                outcome = subprocess.run([sys.executable, "-B", str(entry), "--config", str(self.config_path),
                                          "--request-b64=" + encoded], capture_output=True, text=True, timeout=10)
                self.assertEqual(outcome.returncode, 2)
                self.assertFalse(json.loads(outcome.stdout)["ok"])
        self.assertEqual(self._calls(), [])

    def test_malformed_stdio_reply_is_reported_as_indeterminate(self):
        # A broken/compromised host-selected backend must not crash the shim
        # or cause a caller to assume a gameplay mutation was safely rejected.
        self.server.write_text(
            'import json, sys\n'
            'for i, _ in enumerate(sys.stdin, 101):\n'
            ' print(json.dumps(42 if i == 103 else {"id": i, "result": {}}), flush=True)\n',
            encoding="utf-8",
        )
        with self.assertRaises(BridgeError) as cm:
            invoke(_object(self.config_path), {"tool": "realm_say", "arguments": {"text": "Hello."}})
        self.assertEqual(cm.exception.code, "MCP_OUTCOME_INDETERMINATE")


if __name__ == "__main__":
    unittest.main()
