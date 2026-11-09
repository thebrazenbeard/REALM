"""Real WorldClock finalizer transaction proof on the merged playable source.

The focused #152 parent predates #137's WorldClock effect-hook integration.
This regression exercises the hooks and transactional runner together.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.clock_runner import run_foreground
from realm.gateway import Gateway
from realm.world_clock import WorldClock


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


class HostOwner:
    def __init__(self, transaction_action=None):
        self.transaction_action = transaction_action
        self.called = 0

    def begin_host_tick(self, db, selected):
        pass

    def __call__(self, db, event):
        self.called += 1

    def verify_final_precommit(self, db, processed):
        if self.transaction_action is not None:
            self.transaction_action(db)


class ComposedTransactionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "world.sqlite3"
        Gateway(self.path, world_id="transaction_proof")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        self.clock.schedule("receipt", due_at=NOW, kind="trusted", payload={})

    def run_tick(self, owner):
        return run_foreground(self.clock, {"trusted": owner}, pulses=1,
                              now=lambda: NOW, monotonic=lambda: 1.0,
                              sleep=lambda _: None)

    def assert_retryable(self):
        self.assertIsNone(self.clock.lookup("receipt").processed_at)
        safe = HostOwner()
        self.assertEqual(("receipt",), self.run_tick(safe).event_ids)
        self.assertEqual(1, safe.called)
        self.assertIsNotNone(self.clock.lookup("receipt").processed_at)
        self.assertEqual((), self.run_tick(safe).event_ids)

    def test_rollback_with_false_ack_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "remain read-only"):
            self.run_tick(HostOwner(lambda db: db.rollback()))
        self.assert_retryable()

    def test_commit_then_crash_does_not_persist_receipt(self):
        def break_tx(db):
            db.commit()
            raise RuntimeError("injected host crash")
        with self.assertRaisesRegex(RuntimeError, "remain read-only"):
            self.run_tick(HostOwner(break_tx))
        self.assert_retryable()

    def test_rollback_then_reopen_does_not_hide_escaped_transaction(self):
        def break_tx(db):
            db.rollback()
            db.execute("BEGIN")
        with self.assertRaisesRegex(RuntimeError, "remain read-only"):
            self.run_tick(HostOwner(break_tx))
        self.assert_retryable()

    def test_commit_then_reopen_does_not_hide_escaped_transaction(self):
        def break_tx(db):
            db.commit()
            db.execute("BEGIN")
        with self.assertRaisesRegex(RuntimeError, "remain read-only"):
            self.run_tick(HostOwner(break_tx))
        self.assert_retryable()


if __name__ == "__main__":
    unittest.main()
