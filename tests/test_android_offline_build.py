"""Pure Python, no-Android-toolchain regression checks for offline build preflight."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "clients/android/scripts/build_offline.py"
spec = spec_from_file_location("realm_android_offline_build", SCRIPT)
assert spec and spec.loader
offline = module_from_spec(spec)
spec.loader.exec_module(offline)


class AndroidOfflineBuildPreflightTests(unittest.TestCase):
    def test_java_version_dialect_does_not_require_network(self):
        for output in ('openjdk version "17.0.11" 2024-04-16', "openjdk 21.0.1 2023-10-17"):
            with self.subTest(output=output):
                with patch.object(offline.shutil, "which", return_value="/fake/java"), patch.object(
                    offline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", output)
                ) as invocation:
                    self.assertIn(
                        offline.checked_version("java", ["-version"], r'(?:openjdk|java)(?: version)?\s+"?(\d+)'),
                        ("17", "21"),
                    )
                    invocation.assert_called_once()

    def test_missing_sdk_fails_before_gradle_run(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"ANDROID_HOME": tmp}, clear=True):
            with patch.object(offline, "checked_version", side_effect=["17", "17", "8.13"]), patch.object(
                offline.shutil, "which", return_value="/fake/gradle"
            ):
                with self.assertRaisesRegex(offline.PreflightError, "SDK Platform 36"):
                    offline.preflight()

    def test_wrong_gradle_version_fails_before_sdk(self):
        with patch.object(offline, "checked_version", side_effect=["17", "17", "9.0"]):
            with self.assertRaisesRegex(offline.PreflightError, "Gradle 8.13"):
                offline.preflight()

    def test_script_help_works_without_an_android_toolchain(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--preflight", result.stdout)


if __name__ == "__main__":
    unittest.main()
