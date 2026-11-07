"""Required integration with the REALM MCP backend and disposable SQLite only."""

from __future__ import annotations

import base64
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbridge_mcp52_driver import DriverError, call  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ.get("REALM_PR52_ROOT") or REPO_ROOT).resolve()


class RealMCP52Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (BACKEND_ROOT / "src" / "realm" / "mcp_server.py").is_file():
            raise RuntimeError(
                "REALM MCP backend missing: use the current PR #52-inclusive "
                "checkout or set REALM_PR52_ROOT to its exact source; "
                "integration tests must not silently skip."
            )

    def test_actual_backend_stable_speech_replay_and_identity(self):
        root = BACKEND_ROOT
        sys.path.insert(0, str(root / "src"))
        from realm.gateway import Gateway  # noqa: E402

        with tempfile.TemporaryDirectory(prefix="realm-pr52-driver-") as temp:
            temporary = Path(temp)
            db_path = temporary / "world.sqlite3"
            gateway = Gateway(db_path, world_id="testing")
            token = gateway.credentials.provision("Vera")
            gateway.join("Vera")
            token_path = temporary / "credential.txt"
            token_path.write_text(token, encoding="ascii")
            config = {"schema":"realm.workbridge-mcp52.v1", "python_executable":sys.executable,
                      "realm_root":str(root), "world_db":str(db_path),
                      "expected_world_id":"testing", "expected_actor":"Vera",
                      "token_file":str(token_path)}

            status = call(config, {"tool":"realm_status", "arguments":{}})
            self.assertTrue(status["ok"])
            self.assertIn("testing", status["game_text"])
            self.assertEqual(status["game_data"]["world_id"], "testing")
            self.assertEqual(status["game_data"]["player"], "Vera")
            room = call(config, {"tool":"realm_look", "arguments":{}})
            self.assertTrue(room["ok"])
            self.assertIs(type(room["game_data"]["cursor"]), int)
            feed = call(config, {"tool":"realm_feed", "arguments":{"after":0}})
            self.assertTrue(feed["ok"])
            self.assertIs(type(feed["game_data"]["cursor"]), int)
            self.assertIs(type(feed["game_data"]["events"]), list)
            inventory = call(config, {"tool":"realm_inventory", "arguments":{}})
            self.assertTrue(inventory["ok"])

            move_request = {"tool":"realm_move", "arguments":{
                "direction":"north", "action_id":"workbridge-move-001"}}
            first_move = call(config, move_request)
            second_move = call(config, move_request)
            self.assertTrue(first_move["ok"] and second_move["ok"])
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM action_receipts WHERE actor='Vera' AND action_id='workbridge-move-001'"
                ).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT room_id FROM players WHERE id='Vera'").fetchone()[0],
                                 "corridor")

            def count_speech():
                with closing(sqlite3.connect(db_path)) as db:
                    return db.execute("SELECT COUNT(*) FROM events WHERE kind='say'").fetchone()[0]

            before = count_speech()
            request = {"tool":"realm_say", "arguments":{
                "message":"Testing stable replay.", "action_id":"workbridge-int-001"}}
            first = call(config, request)
            second = call(config, request)
            self.assertTrue(first["ok"] and second["ok"])
            self.assertEqual(count_speech() - before, 1)

    def test_committed_move_oversized_reply_is_indeterminate_not_rejected(self):
        root = Path(os.environ.get("REALM_PR52_ROOT") or Path(__file__).resolve().parents[3]).resolve(strict=True)
        sys.path.insert(0, str(root / "src"))
        from realm.gateway import Gateway  # noqa: E402

        with tempfile.TemporaryDirectory(prefix="realm-mcp-indeterminate-") as temp:
            temporary = Path(temp)
            db_path = temporary / "world.sqlite3"
            gateway = Gateway(db_path, world_id="testing")
            token = gateway.credentials.provision("Vera")
            gateway.join("Vera")
            # A valid move commits the room change and receipt, then the
            # destination's duplicated MCP envelope exceeds its wire limit.
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("UPDATE rooms SET description=? WHERE id='corridor'", ("x" * 40000,))
            token_path = temporary / "credential.txt"
            token_path.write_text(token, encoding="ascii")
            config = {"schema": "realm.workbridge-mcp52.v1", "python_executable": sys.executable,
                      "realm_root": str(root), "world_db": str(db_path),
                      "expected_world_id": "testing", "expected_actor": "Vera",
                      "token_file": str(token_path)}

            rejected_before_dispatch = {"tool": "realm_move", "arguments": {
                "direction": "teleport", "action_id": "oversized-move-001"}}
            with self.assertRaises(DriverError) as caught:
                call(config, rejected_before_dispatch)
            self.assertEqual(caught.exception.code, "REQUEST_SCHEMA_INVALID")
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 0)

            request = {"tool": "realm_move", "arguments": {
                "direction": "north", "action_id": "oversized-move-001"}}
            for _ in range(2):
                with self.assertRaises(DriverError) as caught:
                    call(config, request)
                self.assertEqual(caught.exception.code, "MCP_OUTCOME_INDETERMINATE")
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute("SELECT room_id FROM players WHERE id='Vera'").fetchone()[0],
                                 "corridor")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='move'").fetchone()[0], 1)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM action_receipts WHERE actor='Vera' AND action_id='oversized-move-001'"
                ).fetchone()[0], 1)

            # The CLI's error remains strictly bounded and cannot contain
            # the oversized room description, player token or game narrative.
            config_path = temporary / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode().rstrip("=")
            script = Path(__file__).resolve().parents[1] / "workbridge_mcp52_driver.py"
            completed = subprocess.run(
                [sys.executable, "-B", str(script), "--config", str(config_path),
                 "--request-b64=" + encoded], capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(json.loads(completed.stdout),
                             {"ok": False, "error_code": "MCP_OUTCOME_INDETERMINATE"})
            self.assertLess(len(completed.stdout.encode()), 128)
            self.assertNotIn(token, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
