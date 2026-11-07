"""Optional disposable-world smoke against a separately cloned exact REALM PR51.

Set REALM_PR51_ROOT to a local checkout of PR51 head 4176775211d6... .
This test never interacts with Lappy or a valued game world.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbridge_mcp_driver import invoke  # noqa: E402


@unittest.skipUnless(os.environ.get("REALM_PR51_ROOT"), "REALM_PR51_ROOT not supplied")
class RealPr51Smoke(unittest.TestCase):
    def test_actual_stdio_server_joins_disposable_world_and_exposes_replay_gap(self):
        root = Path(os.environ["REALM_PR51_ROOT"]).resolve(strict=True)
        server = root / "plugins" / "realm-play" / "server" / "realm_mcp.py"
        self.assertTrue(server.is_file())
        sys.path.insert(0, str(root / "src"))
        from realm.gateway import Gateway  # noqa: E402

        with tempfile.TemporaryDirectory(prefix="realm-pr51-workbridge-") as temp:
            db_path = Path(temp) / "world.sqlite3"
            gateway = Gateway(db_path, world_id="testing")
            gateway.join("Vera")
            config = {
                "schema": "realm.workbridge-mcp-transport.v1",
                "python_executable": sys.executable,
                "realm_root": str(root),
                "server_script": str(server),
                "world_db": str(db_path),
                "expected_world_id": "testing",
                "actor": "Vera",
            }
            room = invoke(config, {"tool": "realm_look", "arguments": {}})
            self.assertTrue(room["ok"])
            self.assertFalse(room["mutating"])
            inventory = invoke(config, {"tool": "realm_inventory", "arguments": {}})
            self.assertTrue(inventory["ok"])

            def say_events():
                with sqlite3.connect(db_path) as db:
                    return db.execute("SELECT COUNT(*) FROM events WHERE kind='say'").fetchone()[0]

            before = say_events()
            first = invoke(config, {"tool": "realm_say", "arguments": {"text": "Testing only."}})
            second = invoke(config, {"tool": "realm_say", "arguments": {"text": "Testing only."}})
            self.assertTrue(first["ok"] and second["ok"])
            # Confirm PR51's known lack of stable action-id dedupe, not success.
            self.assertEqual(say_events() - before, 2)
            self.assertEqual(first["server_replay_contract"], "NOT_STABLE_ACTION_ID_QUALIFIED")


if __name__ == "__main__":
    unittest.main()
