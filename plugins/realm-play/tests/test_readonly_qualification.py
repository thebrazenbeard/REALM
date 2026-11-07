"""Real backend read-only qualification against disposable REALM worlds.

REALM_PR52_ROOT must select an exact source checkout of engine PR #52 until
that backend has landed on the plugin's base branch.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ.get("REALM_PR52_ROOT", str(ROOT))).resolve()
SCRIPT = ROOT / "plugins" / "realm-play" / "server" / "qualify_readonly.py"
sys.path.insert(0, str(SCRIPT.parent))
import qualify_readonly as qualifier  # noqa: E402

if (BACKEND_ROOT / "src" / "realm" / "mcp_server.py").is_file():
    sys.path.insert(0, str(BACKEND_ROOT / "src"))
    from realm.gateway import Gateway
else:
    Gateway = None


class ReadonlyQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Gateway is None:
            raise RuntimeError("PR #52 backend absent: set REALM_PR52_ROOT to an exact checkout")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="realm-readonly-qualification-")
        self.addCleanup(temporary.cleanup)
        self.world = Path(temporary.name) / "world.sqlite3"
        self.gateway = Gateway(self.world, world_id="sandbox")
        self.token = self.gateway.credentials.provision("Moss")

    def run_check(self, overrides: dict[str, str | None] | None = None) -> tuple[int, dict, str]:
        env = os.environ.copy()
        for name in ("REALM_ROOT", "REALM_DB_PATH", "REALM_WORLD_ID",
                     "REALM_PLAYER_TOKEN", "REALM_EXPECT_PLAYER"):
            env.pop(name, None)
        env.update({"REALM_ROOT": str(BACKEND_ROOT),
                    "REALM_DB_PATH": str(self.world),
                    "REALM_WORLD_ID": "sandbox",
                    "REALM_PLAYER_TOKEN": self.token,
                    "REALM_EXPECT_PLAYER": "Moss"})
        for key, value in (overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        result = subprocess.run([sys.executable, "-B", str(SCRIPT)],
                                env=env, text=True, capture_output=True,
                                cwd=ROOT, timeout=25, check=False)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(1, len(lines), f"only one sanitized response expected: {len(lines)}")
        return result.returncode, json.loads(lines[0]), result.stderr

    def counts(self):
        with closing(sqlite3.connect(self.world)) as db:
            return tuple(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                         for table in ("events", "players", "action_receipts"))

    def test_success_is_identity_only_and_world_state_unchanged(self):
        self.gateway.join("Moss")
        before = self.counts()
        exit_code, response, stderr = self.run_check()
        self.assertEqual(0, exit_code, stderr)
        self.assertEqual({"ok": True, "world_id": "sandbox", "player": "Moss"}, response)
        self.assertEqual(before, self.counts())
        self.assertEqual("", stderr)

    def test_expected_player_mismatch_fails_closed(self):
        self.gateway.join("Moss")
        exit_code, response, _ = self.run_check({"REALM_EXPECT_PLAYER": "Ember"})
        self.assertEqual(2, exit_code)
        self.assertEqual({"ok": False, "error_code": "BOUND_IDENTITY_MISMATCH"}, response)

    def test_missing_expected_player_fails_before_start(self):
        self.gateway.join("Moss")
        exit_code, response, _ = self.run_check({"REALM_EXPECT_PLAYER": None})
        self.assertEqual(2, exit_code)
        self.assertEqual({"ok": False, "error_code": "HOST_BINDING_MISSING"}, response)

    def test_unjoined_character_fails_without_automatic_join(self):
        before = self.counts()
        exit_code, response, _ = self.run_check()
        self.assertEqual(2, exit_code)
        self.assertEqual({"ok": False, "error_code": "LOOK_UNAVAILABLE"}, response)
        self.assertEqual(before, self.counts())

    def test_revoked_player_fails_closed(self):
        self.gateway.join("Moss")
        self.gateway.credentials.revoke("Moss")
        exit_code, response, _ = self.run_check()
        self.assertEqual(2, exit_code)
        self.assertEqual({"ok": False, "error_code": "MCP_PROCESS_UNAVAILABLE"}, response)

    def test_missing_world_never_creates_database(self):
        missing = self.world.parent / "not-a-world.sqlite3"
        exit_code, response, _ = self.run_check({"REALM_DB_PATH": str(missing)})
        self.assertEqual(2, exit_code)
        self.assertEqual({"ok": False, "error_code": "MCP_PROCESS_UNAVAILABLE"}, response)
        self.assertFalse(missing.exists())

    def test_only_initialize_status_and_look_are_ever_sent(self):
        envelope = lambda n, result: {"jsonrpc": "2.0", "id": n, "result": result}
        stdout = "\n".join(json.dumps(obj) for obj in (
            envelope(1, {"serverInfo": {"name": "realm-local"}}),
            envelope(2, {"isError": False, "structuredContent":
                         {"ok": True, "world_id": "sandbox", "player": "Moss"}}),
            envelope(3, {"isError": False, "structuredContent":
                         {"ok": True, "text": "A room"}}),
        )).encode() + b"\n"
        captured = {}

        def fake_run(*args, **kwargs):
            self.assertEqual([sys.executable, "-I", "-B", str(SCRIPT.parent / "realm_mcp.py")],
                             args[0])
            captured["messages"] = [json.loads(line) for line in kwargs["input"].decode().splitlines()]
            return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr=b"")

        env = {"REALM_ROOT": str(BACKEND_ROOT), "REALM_DB_PATH": str(self.world),
               "REALM_WORLD_ID": "sandbox", "REALM_PLAYER_TOKEN": self.token,
               "REALM_EXPECT_PLAYER": "Moss"}
        with mock.patch.object(qualifier.subprocess, "run", side_effect=fake_run):
            self.assertTrue(qualifier.qualify(env)["ok"])
        self.assertEqual(["initialize", "tools/call", "tools/call"],
                         [message["method"] for message in captured["messages"]])
        self.assertEqual(["realm_status", "realm_look"],
                         [message["params"]["name"] for message in captured["messages"][1:]])
        self.assertNotIn(self.token, json.dumps(captured["messages"]))

    def test_malformed_mcp_reply_does_not_pass(self):
        with self.assertRaisesRegex(qualifier.QualificationError, "MCP_RESPONSE_INVALID"):
            qualifier._validated_response(b"{this is not JSON}\n", "sandbox", "Moss")
        for ambiguous in (b'{"jsonrpc":"2.0","id":1,"id":1}\n',
                          b'{"jsonrpc":"2.0","id":NaN}\n'):
            with self.subTest(ambiguous=ambiguous):
                with self.assertRaisesRegex(qualifier.QualificationError, "MCP_RESPONSE_INVALID"):
                    qualifier._validated_response(ambiguous, "sandbox", "Moss")


if __name__ == "__main__":
    unittest.main()
