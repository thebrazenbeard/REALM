"""Read-only host-wrapper checks with disposable credentials and SQLite worlds."""

from __future__ import annotations

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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from realm_readonly_entry import _minimal_environment, run_readonly, main, DRIVER  # noqa: E402


class EntrySafetyTests(unittest.TestCase):
    def test_disallows_all_mutating_and_unknown_tools_before_process(self):
        for name in ("realm_join", "realm_move", "realm_command", "realm_say",
                     "realm_inventory", "realm_feed", "realm_map", "rm -rf", "realm_look; echo x",
                     "realm_status --config=/tmp/pwn", None, 7):
            with self.subTest(name=name), patch("realm_readonly_entry.subprocess.run") as invoked:
                self.assertEqual(run_readonly(name)["error_code"], "READONLY_TOOL_DENIED")
                invoked.assert_not_called()

    def test_missing_host_config_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="realm-readonly-missing-") as temp:
            with patch("realm_readonly_entry.subprocess.run") as invoked:
                result = run_readonly("realm_status", config=Path(temp) / "missing.json")
                self.assertEqual(result["error_code"], "HOST_WRAPPER_NOT_QUALIFIED")
                invoked.assert_not_called()

    def test_reject_symlinked_config(self):
        with tempfile.TemporaryDirectory(prefix="realm-readonly-links-") as temp:
            root = Path(temp)
            target = root / "target.json"
            target.write_text("{}", encoding="utf8")
            link = root / "link.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("Symlinks unavailable on this host")
            with patch("realm_readonly_entry.subprocess.run") as invoked:
                self.assertEqual(run_readonly("realm_look", config=link)["error_code"],
                                 "HOST_WRAPPER_NOT_QUALIFIED")
                invoked.assert_not_called()

    def test_minimal_environment_excludes_ambient_api_secrets(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SENSITIVE", "PATH": "testpath"}):
            env = _minimal_environment()
        self.assertEqual(env["PATH"], "testpath")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_rejects_extra_cli_arguments(self):
        self.assertEqual(main(["realm_status", "--config", "/tmp/overridden.json"]), 2)

    def test_rejects_mutating_or_wrong_identity_driver_result(self):
        with tempfile.TemporaryDirectory(prefix="realm-readonly-response-") as temp:
            config = Path(temp) / "host.json"
            config.write_text("{}", encoding="utf8")

            class MockProcess:
                returncode = 0
                stdout = b'{"ok":true,"tool":"realm_look","mutating":true,"game_data":{"ok":true}}'

            with patch("realm_readonly_entry.subprocess.run", return_value=MockProcess()):
                self.assertEqual(run_readonly("realm_look", config=config)["error_code"],
                                 "MCP_RESPONSE_INVALID")

            config.write_text(json.dumps({"expected_world_id": "testing",
                                          "expected_actor": "Vera"}), encoding="utf-8")
            MockProcess.stdout = (b'{"ok":true,"tool":"realm_status","mutating":false,'
                                  b'"game_data":{"ok":true,"world_id":"elsewhere",'
                                  b'"player":"Vera"}}')
            with patch("realm_readonly_entry.subprocess.run", return_value=MockProcess()):
                self.assertEqual(run_readonly("realm_status", config=config)["error_code"],
                                 "MCP_RESPONSE_INVALID")

    def test_driver_source_drift_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory(prefix="realm-wrapper-source-pin-") as temp:
            root = Path(temp)
            config = root / "host.json"
            config.write_text("{}", encoding="utf8")
            counterfeit = root / "driver.py"
            counterfeit.write_text("print('unreviewed')\n", encoding="utf8")
            with patch("realm_readonly_entry.subprocess.run") as invoked:
                result = run_readonly("realm_status", config=config, driver=counterfeit)
                self.assertEqual(result["error_code"], "HOST_WRAPPER_NOT_QUALIFIED")
                invoked.assert_not_called()


@unittest.skipUnless(os.environ.get("REALM_SOURCE_ROOT"),
                     "Supply REALM_SOURCE_ROOT to run real disposable-engine read-only test")
class RealReadOnlyIntegrationTests(unittest.TestCase):
    def test_readonly_status_look_do_not_mutate_world(self):
        engine_root = Path(os.environ["REALM_SOURCE_ROOT"]).resolve(strict=True)
        sys.path.insert(0, str(engine_root / "src"))
        from realm.gateway import Gateway

        with tempfile.TemporaryDirectory(prefix="realm-readonly-integration-") as temp:
            root = Path(temp)
            db = root / "isolated.sqlite3"
            gateway = Gateway(db, world_id="testing")
            token = gateway.credentials.provision("Vera")
            gateway.join("Vera")
            tokenfile = root / "token.txt"
            tokenfile.write_text(token, encoding="ascii")
            config = root / "host.json"
            config.write_text(json.dumps({
                "schema": "realm.workbridge-mcp52.v1",
                "python_executable": sys.executable,
                "realm_root": str(engine_root),
                "world_db": str(db),
                "expected_world_id": "testing", "expected_actor": "Vera",
                "token_file": str(tokenfile),
            }), encoding="utf-8")

            def counts():
                with closing(sqlite3.connect(db)) as conn:
                    return (conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                            conn.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0])

            initial = counts()
            status = run_readonly("realm_status", config=config, driver=DRIVER)
            look = run_readonly("realm_look", config=config, driver=DRIVER)
            self.assertTrue(status["ok"], status)
            self.assertEqual(status["game_data"]["world_id"], "testing")
            self.assertEqual(status["game_data"]["player"], "Vera")
            self.assertTrue(look["ok"], look)
            self.assertIn("The Atrium", look["game_text"])
            self.assertEqual(counts(), initial)
            self.assertNotIn(token, json.dumps(status) + json.dumps(look))
            self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(), json.dumps(status))

            # Execute an actual isolated copy of the fixed-path CLI, not only
            # its importable helper with test-specific config overrides.
            launcher_dir = root / "host-launcher"
            launcher_dir.mkdir()
            (launcher_dir / "private").mkdir()
            original = Path(__file__).resolve().parents[1]
            for name in ("realm_readonly_entry.py", "workbridge_mcp52_driver.py"):
                shutil.copyfile(original / name, launcher_dir / name)
            shutil.copyfile(config, launcher_dir / "private" / "realm-host-config.json")
            for requested, should_pass in (("realm_status", True), ("realm_look", True),
                                           ("realm_move", False)):
                cli = subprocess.run([sys.executable, "-B", str(launcher_dir / "realm_readonly_entry.py"),
                                      requested], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, timeout=22, check=False)
                parsed = json.loads(cli.stdout)
                self.assertEqual(cli.returncode, 0 if should_pass else 2, cli.stderr)
                self.assertEqual(parsed["ok"], should_pass)
                self.assertNotIn(token, cli.stdout + cli.stderr)
            self.assertEqual(counts(), initial)


if __name__ == "__main__":
    unittest.main()
