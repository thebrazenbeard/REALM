"""Cross-process qualification for REALM scoped credential time observations.

Issue #191: ordinary valid authentication observes time in process memory, not
necessarily in the durable SQLite/external witness. These tests never adjust an
OS clock, touch live worlds, or create public listeners.

The expected-failure case specifies the stronger desired behavior and remains a
visible release HOLD until a durable clock policy is implemented and validated.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from realm.gateway import Gateway
from realm.scoped_credentials import ScopedCredentialStore


# A genuinely new interpreter is required: merely constructing a second store
# does not clear module-level _PROCESS_HIGH_WATER.
CHILD_AUTH = """
import sys
from datetime import datetime
from realm.engine import Engine
from realm.scoped_credentials import CredentialStateError, ScopedCredentialStore

db_path, guard_path, instant, token = sys.argv[1:]
try:
    store = ScopedCredentialStore(
        Engine(db_path), "restart_probe", guard_path=guard_path,
        clock=lambda: datetime.fromisoformat(instant),
    )
    actor = store.authenticate(token, required_capability="feed:read")
except CredentialStateError:
    actor = None
print(actor if actor is not None else "DENIED")
"""


class ScopedClockRestartProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        self.db_path = home / "world.sqlite3"
        self.guard_path = home / "guard.json"
        self.t0 = datetime(2031, 2, 1, 12, 0, tzinfo=timezone.utc)
        gate = Gateway(self.db_path, world_id="restart_probe")
        store = ScopedCredentialStore(
            gate.engine, "restart_probe",
            guard_path=self.guard_path, clock=lambda: self.t0,
        )
        self.token = store.provision("Moss", ttl_seconds=3600)

    def auth_in_new_process(self, instant):
        env = os.environ.copy()
        source_root = str(ROOT / "src")
        env["PYTHONPATH"] = source_root + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        completed = subprocess.run(
            [
                sys.executable, "-c", CHILD_AUTH,
                str(self.db_path), str(self.guard_path),
                instant.isoformat(), self.token,
            ],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True, timeout=20,
        )
        return completed.stdout.strip()

    def test_future_observation_in_fresh_interpreter_succeeds(self):
        # Ensures the separate-process harness is real and can authenticate
        # just before the original one-hour expiry.
        self.assertEqual(
            "Moss", self.auth_in_new_process(self.t0 + timedelta(seconds=3500))
        )

    @unittest.expectedFailure
    def test_restart_after_future_observation_must_deny_backward_clock(self):
        # DESIRED PROPERTY, presently unqualified (#191). A future valid read
        # should not be forgotten when the process restarts and its synthetic
        # clock moves back. A positive result here would mean true acceptance,
        # not an actual OS clock manipulation or a public-host exploit.
        self.assertEqual(
            "Moss", self.auth_in_new_process(self.t0 + timedelta(seconds=3500))
        )
        self.assertEqual(
            "DENIED", self.auth_in_new_process(self.t0 + timedelta(seconds=100))
        )


if __name__ == "__main__":
    unittest.main()
