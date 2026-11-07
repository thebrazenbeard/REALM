"""Disposable-only host-side transport tests for PR #52 WorkBridge candidate."""

from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbridge_mcp52_driver import DriverError, call, config_from_file  # noqa: E402


FAKE_SERVER = '''import hashlib, json, os, sys
from pathlib import Path
for line in sys.stdin:
    row = json.loads(line)
    ident, method = row["id"], row["method"]
    if method == "initialize":
        data = {"serverInfo": {"name": "realm-local", "version": "0.1"}}
    elif method == "tools/list":
        data = {"tools": [{"name": n} for n in
            ("realm_status", "realm_join", "realm_look", "realm_inventory", "realm_move", "realm_command", "realm_say", "realm_feed", "realm_map")]}
    else:
        with open(__file__ + ".calls", "a", encoding="utf8") as f:
            f.write(json.dumps({"name": row["params"]["name"],
                "arguments": row["params"]["arguments"],
                "world_id": os.environ["REALM_WORLD_ID"],
                "db": os.environ["REALM_DB_PATH"],
                "digest": hashlib.sha256(os.environ["REALM_PLAYER_TOKEN"].encode()).hexdigest()}) + "\\n")
        data = {"isError": False, "content": [{"type":"text", "text":"Ancient gate, covered in vines."}],
                "structuredContent": {"ok": True, "text":"Ancient gate, covered in vines.", "cursor": 17}}
    print(json.dumps({"jsonrpc":"2.0", "id": ident, "result": data}), flush=True)
'''


class WorkBridgeMCP52Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="realm-mcp52-driver-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "REALM"
        self.server = self.repo / "src" / "realm" / "mcp_server.py"
        self.server.parent.mkdir(parents=True)
        self.server.write_text(FAKE_SERVER, encoding="utf-8")
        self.db = self.root / "world.sqlite3"
        self.token = "TEST_TOKEN_0123456789012345"
        self.tokenfile = self.root / "private_token.txt"
        self.tokenfile.write_text(self.token + "\n", encoding="ascii")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE world_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("CREATE TABLE player_credentials(player_name TEXT, token_digest TEXT)")
            conn.execute("INSERT INTO world_meta VALUES('world_id','testing')")
            conn.execute("INSERT INTO player_credentials VALUES('Vera', ?)",
                         (hashlib.sha256(self.token.encode()).hexdigest(),))
            conn.commit()
        self.config = {"schema":"realm.workbridge-mcp52.v1", "python_executable":sys.executable,
                       "realm_root":str(self.repo), "world_db":str(self.db),
                       "expected_world_id":"testing", "expected_actor":"Vera",
                       "token_file":str(self.tokenfile)}
        self.configfile = self.root / "config.json"
        self.save_config()

    def save_config(self):
        self.configfile.write_text(json.dumps(self.config), encoding="utf-8")

    def calls(self):
        path = Path(str(self.server) + ".calls")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def test_read_only_call_uses_host_token_without_exposing_it(self):
        result = call(config_from_file(self.configfile), {"tool":"realm_look", "arguments":{}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["game_text_trust"], "UNTRUSTED_IN_WORLD_CONTENT")
        self.assertEqual(result["game_data_trust"], "UNTRUSTED_IN_WORLD_CONTENT")
        self.assertEqual(result["game_data"]["cursor"], 17)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["digest"], hashlib.sha256(self.token.encode()).hexdigest())
        self.assertEqual(self.calls()[0]["world_id"], "testing")
        self.assertNotIn(self.token, json.dumps(result))

    def test_stable_action_id_passes_unchanged(self):
        request = {"tool":"realm_say", "arguments":{"message":"Hello there!", "action_id":"user-session-7"}}
        first = call(config_from_file(self.configfile), request)
        second = call(config_from_file(self.configfile), request)
        self.assertTrue(first["mutating"] and second["mutating"])
        self.assertEqual([x["arguments"]["action_id"] for x in self.calls()],
                         ["user-session-7", "user-session-7"])

    def test_move_action_id_and_inventory_ergonomics(self):
        inventory = call(config_from_file(self.configfile), {"tool":"realm_inventory", "arguments":{}})
        move = call(config_from_file(self.configfile), {"tool":"realm_move", "arguments":{
            "direction":"north", "action_id":"move-77"}})
        self.assertFalse(inventory["mutating"])
        self.assertTrue(move["mutating"])
        self.assertEqual(self.calls()[-1]["arguments"], {"direction":"north", "action_id":"move-77"})

    def test_tool_and_actor_injection_denied(self):
        for request in (
            {"tool":"realm_admin", "arguments":{}},
            {"tool":"realm_say", "arguments":{"message":"x", "action_id":"one", "actor":"Patrick"}},
            {"tool":"realm_command", "arguments":{"command":"north"}},
            {"tool":"realm_command", "arguments":{"command":"say hi", "action_id":"a1"}},
            {"tool":"realm_move", "arguments":{"direction":"n", "action_id":"x"}},
            {"tool":"realm_say", "arguments":{"message":"text ", "action_id":"x"}},
            {"tool":"realm_status", "arguments":{"player":"Patrick"}},
            {"tool":"realm_feed", "arguments":{"after":True}},
        ):
            with self.subTest(request=request), self.assertRaises(DriverError):
                call(config_from_file(self.configfile), request)
        self.assertEqual(self.calls(), [])

    def test_mismatched_world_preflight_denies_before_process(self):
        self.config["expected_world_id"] = "elsewhere"
        self.save_config()
        with self.assertRaises(DriverError) as cm:
            call(config_from_file(self.configfile), {"tool":"realm_look", "arguments":{}})
        self.assertEqual(cm.exception.code, "WORLD_IDENTITY_MISMATCH")
        self.assertEqual(self.calls(), [])

    def test_mismatched_actor_token_preflight_denies(self):
        self.config["expected_actor"] = "Patrick"
        self.save_config()
        with self.assertRaises(DriverError) as cm:
            call(config_from_file(self.configfile), {"tool":"realm_look", "arguments":{}})
        self.assertEqual(cm.exception.code, "ACTOR_TOKEN_MISMATCH")
        self.assertEqual(self.calls(), [])

    def test_cli_one_shot_base64url_no_token_in_output(self):
        script = Path(__file__).resolve().parents[1] / "workbridge_mcp52_driver.py"
        raw = json.dumps({"tool":"realm_look", "arguments":{}}).encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        result = subprocess.run([sys.executable, "-B", str(script), "--config", str(self.configfile),
                                 "--request-b64=" + encoded], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_cli_rejects_duplicate_json_keys_without_tool_call(self):
        script = Path(__file__).resolve().parents[1] / "workbridge_mcp52_driver.py"
        raw = b'{"tool":"realm_look","tool":"realm_say","arguments":{}}'
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        result = subprocess.run([sys.executable, "-B", str(script), "--config", str(self.configfile),
                                 "--request-b64=" + encoded], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(self.calls(), [])

    def test_conflicting_jsonrpc_error_and_result_is_indeterminate(self):
        self.server.write_text(
            'import json, sys\n'
            'for line in sys.stdin:\n'
            ' row=json.loads(line)\n'
            ' payload={"jsonrpc":"2.0", "id":row["id"], "result":{}}\n'
            ' if row["method"]=="initialize": payload["result"]={"serverInfo":{"name":"realm-local"}}\n'
            ' if row["method"]=="tools/list": payload["result"]={"tools":[{"name":"realm_say"}]}\n'
            ' if row["method"]=="tools/call":\n'
            '  payload["result"]={"isError":False,"content":[{"type":"text","text":"committed"}]}\n'
            '  payload["error"]={"code":-32603,"message":"contradictory"}\n'
            ' print(json.dumps(payload),flush=True)\n', encoding="utf-8")
        with self.assertRaises(DriverError) as cm:
            call(config_from_file(self.configfile), {"tool": "realm_say",
                 "arguments": {"message": "Hello", "action_id": "one"}})
        self.assertEqual(cm.exception.code, "MCP_OUTCOME_INDETERMINATE")

    def test_missing_or_invalid_structured_game_data_is_rejected(self):
        for replacement in (None, [], {"ok": False}, {"ok": True, "text": "contradiction"}):
            with self.subTest(replacement=replacement):
                # A superficially successful prose block must not silently
                # stand in for the authoritative typed game result.
                self.server.write_text(
                    'import json, sys\n'
                    'for line in sys.stdin:\n'
                    ' row=json.loads(line)\n'
                    ' result={"content":[{"type":"text","text":"Ancient gate, covered in vines."}],"isError":False}\n'
                    f' if row["method"]=="tools/call": result["structuredContent"]={replacement!r}\n'
                    ' if row["method"]=="initialize": result={"serverInfo":{"name":"realm-local"}}\n'
                    ' if row["method"]=="tools/list": result={"tools":[{"name":"realm_look"}]}\n'
                    ' print(json.dumps({"jsonrpc":"2.0","id":row["id"],"result":result}),flush=True)\n',
                    encoding="utf-8")
                with self.assertRaises(DriverError) as cm:
                    call(config_from_file(self.configfile), {"tool":"realm_look", "arguments":{}})
                self.assertEqual(cm.exception.code, "MCP_RESULT_INVALID")


if __name__ == "__main__":
    unittest.main()
