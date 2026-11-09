"""Synthetic protocol-spy checks for trusted host clock batch hooks.

WorldClock's atmosphere extensions live on a separate Draft branch. This
fixture verifies the adapter's callable-object protocol on the current stack;
real atmosphere/ship SQLite integration is qualified separately on a composed
source tree rather than falsely claiming it exists in this branch.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.clock_runner import UnknownEventKind, run_foreground
from realm.gateway import Gateway
from realm.world_clock import DueEvent, WorldClock


NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


class HookAwareClockSpy(WorldClock):
    """Exact hook ordering protocol, with no claim of real SQLite custody."""

    def __init__(self, db_path, selected):
        super().__init__(db_path)
        self.selected = tuple(selected)
        self.seen_callbacks = []

    def tick(self, now_utc, handler, *, limit=100, acknowledge_forward_jump=False):
        if not callable(getattr(handler, "begin_host_tick", None)):
            raise RuntimeError("Missing host tick begin hook")
        if not callable(getattr(handler, "verify_final_precommit", None)):
            raise RuntimeError("Missing host precommit finalizer")
        database_marker = object()
        handler.begin_host_tick(database_marker, self.selected)
        for event in self.selected:
            self.seen_callbacks.append(handler(database_marker, event))
        handler.verify_final_precommit(database_marker, self.selected)
        return list(self.selected)


class HookOwner:
    __hash__ = None

    def __init__(self, label, trace):
        self.label = label
        self.trace = trace
        self.database = None

    def __eq__(self, other):
        # Handler dedup must use identity, not equality or hashability.
        return isinstance(other, HookOwner)

    def begin_host_tick(self, db, due_events):
        self.database = db
        self.trace.append((self.label, "begin", tuple(e.event_id for e in due_events)))

    def __call__(self, db, event):
        if db is not self.database:
            raise RuntimeError("Handler used a different transaction")
        self.trace.append((self.label, "handle", event.event_id))
        return event.event_id

    def verify_final_precommit(self, db, processed):
        if db is not self.database:
            raise RuntimeError("Finalizer used a different transaction")
        self.trace.append((self.label, "final", tuple(e.event_id for e in processed)))


class ClockRunnerHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "one-world.sqlite3"
        Gateway(self.db, world_id="hook_test")
        WorldClock(self.db).enable()

    def fake_clock(self, *kinds):
        events = [DueEvent("hook_" + str(i), NOW, kind, {})
                  for i, kind in enumerate(kinds, start=1)]
        return HookAwareClockSpy(self.db, events)

    def test_registered_hook_owners_prepare_all_before_any_callback_and_finalize_once(self):
        trace = []
        atmosphere = HookOwner("atmosphere", trace)
        ship = HookOwner("ship", trace)
        def ordinary(db, event):
            trace.append(("ordinary", "handle", event.event_id))
            return event.event_id

        clock = self.fake_clock("atmosphere_tick", "audit", "ship_arrival", "extra_atmosphere")
        report = run_foreground(clock, {
            "atmosphere_tick": atmosphere,
            "extra_atmosphere": atmosphere,
            "audit": ordinary,
            "ship_arrival": ship,
        }, pulses=1, now=lambda: NOW, monotonic=lambda: 1.0, sleep=lambda _: None)
        batch = ("hook_1", "hook_2", "hook_3", "hook_4")
        self.assertEqual(batch, report.event_ids)
        self.assertEqual(list(batch), clock.seen_callbacks,
                         "Every trusted callback's typed return must reach WorldClock")
        self.assertEqual([
            ("atmosphere", "begin", batch),
            ("ship", "begin", batch),
            ("atmosphere", "handle", "hook_1"),
            ("ordinary", "handle", "hook_2"),
            ("ship", "handle", "hook_3"),
            ("atmosphere", "handle", "hook_4"),
            ("atmosphere", "final", batch),
            ("ship", "final", batch),
        ], trace)

    def test_unknown_kind_fails_closed_without_finalizer(self):
        trace = []
        trusted = HookOwner("trusted", trace)
        clock = self.fake_clock("trusted", "not_registered")
        with self.assertRaises(UnknownEventKind):
            run_foreground(clock, {"trusted": trusted}, pulses=1,
                           now=lambda: NOW, monotonic=lambda: 1.0,
                           sleep=lambda _: None)
        self.assertEqual([("trusted", "begin", ("hook_1", "hook_2")),
                          ("trusted", "handle", "hook_1")], trace)

    def test_noncallable_or_unpaired_hooks_are_rejected_before_tick(self):
        class BadHook:
            def __call__(self, db, event):
                return None
            begin_host_tick = "not callable"

        class UnpairedHook:
            def __call__(self, db, event):
                return None
            def begin_host_tick(self, db, due_events):
                return None

        for callback in (BadHook(), UnpairedHook()):
            with self.subTest(callback=type(callback).__name__):
                clock = self.fake_clock("trusted")
                with self.assertRaises(ValueError):
                    run_foreground(clock, {"trusted": callback}, pulses=1,
                                   now=lambda: NOW, monotonic=lambda: 1.0,
                                   sleep=lambda _: None)
                self.assertEqual([], clock.seen_callbacks)


if __name__ == "__main__":
    unittest.main()
