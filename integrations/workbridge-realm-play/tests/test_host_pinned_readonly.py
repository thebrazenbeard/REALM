"""Candidate fixed-binding host entrypoint tests; disposable files only."""

from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import host_pinned_readonly as pinned  # noqa: E402


FAKE_SERVER = '''import json, os, sys
from pathlib import Path
for line in sys.stdin:
    row = json.loads(line)
    method = row["method"]
    if method == "initialize":
        data = {"serverInfo": {"name": "realm-local"}}
    elif method == "tools/list":
        data = {"tools": [{"name": name} for name in ("realm_status", "realm_look")]}
    else:
        name = row["params"]["name"]
        with open(__file__ + ".calls", "a", encoding="utf8") as handle:
            handle.write(name + "\\n")
        value = ({"ok": True, "world_id": "testing", "player": "Vera", "cursor": 889,
                  "events": ["off-room private activity"]} if name == "realm_status" else
                 {"ok": True, "text": "Quiet atrium.", "cursor": 889,
                  "events": ["off-room private activity"]})
        data = {"isError": False, "content": [{"type": "text", "text": "Quiet atrium."}],
                "structuredContent": value}
    print(json.dumps({"jsonrpc": "2.0", "id": row["id"], "result": data}), flush=True)
'''


class HostPinnedReadonlyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="realm-host-pinned-readonly-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.wrapper_dir = self.root / "installed"
        self.wrapper_dir.mkdir()
        for name in ("host_pinned_readonly.py", "workbridge_mcp52_driver.py"):
            shutil.copyfile(SOURCE / name, self.wrapper_dir / name)
        self.wrapper = self.wrapper_dir / "host_pinned_readonly.py"
        self.repo = self.root / "REALM"
        self.server = self.repo / "src" / "realm" / "mcp_server.py"
        self.server.parent.mkdir(parents=True)
        self.server.write_text(FAKE_SERVER, encoding="utf-8")
        self.db = self.root / "world.sqlite3"
        self.token = "TEST_TOKEN_0123456789012345"
        self.token_file = self.root / "token.txt"
        self.token_file.write_text(self.token, encoding="ascii")
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE world_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("CREATE TABLE player_credentials(player_name TEXT, token_digest TEXT)")
            db.execute("INSERT INTO world_meta VALUES ('world_id','testing')")
            db.execute("INSERT INTO player_credentials VALUES ('Vera', ?)",
                       (hashlib.sha256(self.token.encode()).hexdigest(),))
            db.commit()
        self.config = {
            "schema": "realm.workbridge-mcp52.v1",
            "python_executable": sys.executable,
            "realm_root": str(self.repo),
            "world_db": str(self.db),
            "expected_world_id": "testing",
            "expected_actor": "Vera",
            "token_file": str(self.token_file),
        }
        self.binding = self.wrapper_dir / "host_binding.json"
        self.save_binding()

    def save_binding(self):
        self.binding.write_text(json.dumps(self.config), encoding="utf-8")

    def invoke(self, name="realm_status", args=None, *, payload=None, extra_args=None, env=None):
        request = {"tool": name, "arguments": {} if args is None else args}
        raw = json.dumps(request).encode() if payload is None else payload
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        command = [sys.executable, "-B", str(self.wrapper), "--request-b64=" + encoded]
        command.extend(extra_args or [])
        completed = subprocess.run(command, text=True, capture_output=True,
                                   timeout=10, env=env)
        self.assertNotIn(self.token, completed.stdout + completed.stderr)
        return completed.returncode, json.loads(completed.stdout) if completed.stdout else None

    def calls(self):
        trace = Path(str(self.server) + ".calls")
        return trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []

    def test_status_uses_fixed_world_and_character(self):
        exit_code, data = self.invoke()
        self.assertEqual(0, exit_code)
        self.assertTrue(data["ok"])
        self.assertEqual("testing", data["game_data"]["world_id"])
        self.assertEqual("Vera", data["game_data"]["player"])
        self.assertEqual({"ok": True, "world_id": "testing", "player": "Vera"},
                         data["game_data"])
        self.assertNotIn("cursor", json.dumps(data))
        self.assertNotIn("off-room private activity", json.dumps(data))
        self.assertEqual(["realm_status"], self.calls())

    def test_look_is_only_other_allowed_operation(self):
        exit_code, data = self.invoke("realm_look")
        self.assertEqual(0, exit_code)
        self.assertTrue(data["ok"])
        self.assertEqual("Quiet atrium.", data["game_data"]["text"])
        self.assertEqual({"ok": True, "text": "Quiet atrium."}, data["game_data"])
        self.assertEqual("Quiet atrium.", data["game_text"])
        self.assertNotIn("cursor", json.dumps(data))
        self.assertNotIn("off-room private activity", json.dumps(data))
        self.assertEqual(["realm_look"], self.calls())

    def test_join_command_feed_map_mutations_are_denied_before_credential_access(self):
        for name, args in (
            ("realm_join", {}), ("realm_feed", {}), ("realm_map", {}),
            ("realm_inventory", {}), ("realm_move", {"direction": "north", "action_id": "one"}),
            ("realm_say", {"message": "hi", "action_id": "one"}),
            ("realm_command", {"command": "history 50", "action_id": "one"}),
        ):
            with self.subTest(name=name):
                with mock.patch.object(pinned, "config_from_file") as config:
                    with self.assertRaises(pinned.DriverError) as err:
                        pinned.execute({"tool": name, "arguments": args}, binding=self.binding)
                    self.assertEqual("OPERATION_NOT_ALLOWED", err.exception.code)
                    config.assert_not_called()
        self.assertEqual([], self.calls())

    def test_caller_cannot_override_pinned_binding_with_config_argument(self):
        exit_code, _ = self.invoke(extra_args=["--config", str(self.root / "other.json")])
        self.assertEqual(2, exit_code)
        self.assertEqual([], self.calls())

    def test_game_request_and_environment_cannot_rebind_world_actor_or_backend(self):
        for request in (
            {"tool": "realm_status", "arguments": {"world_id": "other"}},
            {"tool": "realm_look", "arguments": {"actor": "Patrick"}},
            {"tool": "realm_status", "arguments": {}, "token": "injected"},
        ):
            with self.subTest(request=request):
                exit_code, data = self.invoke(payload=json.dumps(request).encode())
                self.assertEqual(2, exit_code)
                self.assertFalse(data["ok"])
        injected_env = dict(os.environ, REALM_WORLD_ID="other",
                            REALM_DB_PATH=str(self.root / "other.sqlite"),
                            REALM_PLAYER_TOKEN="attacker_token",
                            REALM_PR52_ROOT=str(self.root / "other_backend"))
        exit_code, data = self.invoke(env=injected_env)
        self.assertEqual(0, exit_code)
        self.assertEqual("testing", data["game_data"]["world_id"])
        self.assertEqual("Vera", data["game_data"]["player"])
        self.assertEqual(["realm_status"], self.calls())

    def test_wrong_actor_binding_is_rejected(self):
        self.config["expected_actor"] = "Patrick"
        self.save_binding()
        exit_code, data = self.invoke()
        self.assertEqual(2, exit_code)
        self.assertEqual("ACTOR_TOKEN_MISMATCH", data["error_code"])
        self.assertEqual([], self.calls())

    def test_missing_host_binding_fails_without_creating_world(self):
        self.binding.unlink()
        exit_code, data = self.invoke()
        self.assertEqual(2, exit_code)
        self.assertEqual("HOST_CONFIG_INVALID", data["error_code"])
        self.assertEqual([], self.calls())

    def test_duplicate_keys_and_large_input_are_rejected(self):
        for raw in (b'{"tool":"realm_status","tool":"realm_say","arguments":{}}',
                    b'{"tool":"realm_status","arguments":{},"noise":"' + b"A"*8200 + b'"}'):
            with self.subTest(size=len(raw)):
                exit_code, data = self.invoke(payload=raw)
                self.assertEqual(2, exit_code)
                self.assertFalse(data["ok"])
        self.assertEqual([], self.calls())

    def test_actual_mcp_backend_status_look_leave_disposable_world_unmodified(self):
        real_root = Path(os.environ.get("REALM_PR52_ROOT") or Path(__file__).resolve().parents[3]).resolve()
        self.assertTrue((real_root / "src" / "realm" / "mcp_server.py").is_file(),
                        "REALM MCP backend source is required; integration test must not skip")
        sys.path.insert(0, str(real_root / "src"))
        from realm.gateway import Gateway  # noqa: E402

        actual_db = self.root / "actual-world.sqlite3"
        game = Gateway(actual_db, world_id="testing")
        actual_token = game.credentials.provision("Vera")
        game.join("Vera")
        self.token_file.write_text(actual_token, encoding="ascii")
        self.config["realm_root"] = str(real_root)
        self.config["world_db"] = str(actual_db)
        self.save_binding()
        with closing(sqlite3.connect(actual_db)) as db:
            before = (db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                      db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0])

        for name in ("realm_status", "realm_look"):
            code, result = self.invoke(name)
            self.assertEqual(0, code)
            self.assertTrue(result["ok"])
            self.assertEqual(name, result["tool"])
            self.assertEqual("UNTRUSTED_IN_WORLD_CONTENT", result["game_data_trust"])
            self.assertIs(type(result["game_data"]), dict)
            self.assertNotIn("cursor", result["game_data"])
        with closing(sqlite3.connect(actual_db)) as db:
            after = (db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                     db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
