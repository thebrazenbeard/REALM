"""Optional integration with exact PR #52 checkout and disposable SQLite only."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbridge_mcp52_driver import call  # noqa: E402


@unittest.skipUnless(os.environ.get("REALM_PR52_ROOT"), "REALM_PR52_ROOT not supplied")
class RealMCP52Integration(unittest.TestCase):
    def test_actual_backend_stable_speech_replay_and_identity(self):
        root = Path(os.environ["REALM_PR52_ROOT"]).resolve(strict=True)
        self.assertTrue((root / "src" / "realm" / "mcp_server.py").is_file())
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


if __name__ == "__main__":
    unittest.main()
