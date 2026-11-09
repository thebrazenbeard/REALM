"""Host-controlled foreground UTC pump for one isolated REALM world."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway
from realm.world_clock import WorldClock

BASE = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)


class ClockRunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "runner.sqlite3"
        Gateway(self.path, world_id="runner_world")
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def runner(self):
        try:
            from realm.clock_runner import run_foreground
        except ModuleNotFoundError:
            self.fail("Missing host-only clock foreground runner")
        return run_foreground
    def test_due_event_dispatches_registered_kind_once(self):
        self.clock.schedule("ping1", due_at=BASE, kind="ping", payload={"n": 1})
        seen = []
        def ping(db, event):
            seen.append((event.event_id, event.payload["n"]))
            db.execute("UPDATE world_meta SET value=value WHERE key='world_id'")
        report = self.runner()(
            self.clock, {"ping": ping}, pulses=1,
            now=lambda: BASE, monotonic=lambda: 1.0, sleep=lambda _: None,
        )
        self.assertEqual([("ping1", 1)], seen)
        self.assertEqual(("ping1",), report.event_ids)
        self.assertEqual(1, report.pulses)
        self.assertEqual("complete", report.reason)
        self.assertIsNotNone(self.clock.lookup("ping1").processed_at)
        again = self.runner()(
            self.clock, {"ping": ping}, pulses=1,
            now=lambda: BASE + timedelta(seconds=1),
            monotonic=lambda: 2.0, sleep=lambda _: None,
        )
        self.assertEqual((), again.event_ids)
        self.assertEqual([("ping1", 1)], seen)

    def test_unknown_kind_rolls_back_preceding_registered_effects(self):
        self.clock.schedule("a_known", due_at=BASE, kind="ping", payload={})
        self.clock.schedule("z_unknown", due_at=BASE, kind="unregistered", payload={})
        def effect(db, event):
            db.execute("UPDATE world_meta SET value='changed' WHERE key='world_id'")
        from realm.clock_runner import UnknownEventKind
        with self.assertRaises(UnknownEventKind):
            self.runner()(self.clock, {"ping": effect}, pulses=1,
                          now=lambda: BASE, monotonic=lambda: 0.0,
                          sleep=lambda _: None)
        self.assertIsNone(self.clock.lookup("a_known").processed_at)
        self.assertIsNone(self.clock.lookup("z_unknown").processed_at)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual("runner_world", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])

    def test_handler_abort_rolls_back_and_restart_retries(self):
        self.clock.schedule("once", due_at=BASE, kind="ping", payload={})
        def fails(db, event):
            db.execute("UPDATE world_meta SET value='changed' WHERE key='world_id'")
            raise RuntimeError("injected crash")
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.runner()(self.clock, {"ping": fails}, pulses=1,
                          now=lambda: BASE, monotonic=lambda: 0.0,
                          sleep=lambda _: None)
        self.assertIsNone(self.clock.lookup("once").processed_at)
        self.assertEqual(1, len(self.runner()(
            WorldClock(self.path), {"ping": lambda db, e: None}, pulses=1,
            now=lambda: BASE, monotonic=lambda: 0.0,
            sleep=lambda _: None).event_ids))
        self.assertIsNotNone(self.clock.lookup("once").processed_at)

    def test_monotonic_pacing_does_not_persist_monotonic_time(self):
        self.clock.schedule("next", due_at=BASE + timedelta(seconds=1),
                            kind="ping", payload={})
        ticks = iter([BASE, BASE + timedelta(seconds=1)])
        monotones = iter([101.0, 101.0])
        slept = []
        report = self.runner()(self.clock, {"ping": lambda db, e: None},
            pulses=2, interval_seconds=0.5, now=lambda: next(ticks),
            monotonic=lambda: next(monotones), sleep=slept.append)
        self.assertEqual([0.5], slept)
        self.assertEqual(2, report.pulses)
        self.assertEqual(("next",), report.event_ids)
        with closing(sqlite3.connect(self.path)) as db:
            value = db.execute("SELECT last_utc FROM clock_state WHERE id=1").fetchone()[0]
        self.assertEqual(BASE + timedelta(seconds=1), datetime.fromisoformat(value))

    def test_stop_before_and_between_pulses(self):
        self.clock.schedule("pending", due_at=BASE, kind="ping", payload={})
        early = self.runner()(self.clock, {"ping": lambda db, e: None},
            pulses=2, now=lambda: BASE, monotonic=lambda: 0.0,
            sleep=lambda _: None, stop=lambda: True)
        self.assertEqual((0, (), "stopped"),
                         (early.pulses, early.event_ids, early.reason))
        self.assertIsNone(self.clock.lookup("pending").processed_at)
        calls = 0
        def stop():
            nonlocal calls
            calls += 1
            return calls >= 3
        stopped = self.runner()(self.clock, {"ping": lambda db, e: None},
            pulses=3, now=lambda: BASE, monotonic=lambda: 0.0,
            sleep=lambda _: None, stop=stop)
        self.assertEqual((1, ("pending",), "stopped"),
                         (stopped.pulses, stopped.event_ids, stopped.reason))

    def test_clock_rollback_and_forward_jump_are_not_suppressed(self):
        from realm.world_clock import ClockSkewError
        self.runner()(self.clock, {"ping": lambda db, e: None},
            pulses=1, now=lambda: BASE, monotonic=lambda: 0.0,
            sleep=lambda _: None)
        with self.assertRaises(ClockSkewError):
            self.runner()(WorldClock(self.path), {"ping": lambda db, e: None},
                pulses=1, now=lambda: BASE - timedelta(seconds=1),
                monotonic=lambda: 0.0, sleep=lambda _: None)
        with self.assertRaises(ClockSkewError):
            self.runner()(WorldClock(self.path), {"ping": lambda db, e: None},
                pulses=1, now=lambda: BASE + timedelta(days=2),
                monotonic=lambda: 0.0, sleep=lambda _: None)
        yes = self.runner()(WorldClock(self.path), {"ping": lambda db, e: None},
            pulses=1, now=lambda: BASE + timedelta(days=2),
            monotonic=lambda: 0.0, sleep=lambda _: None,
            acknowledge_forward_jump=True)
        self.assertEqual(1, yes.pulses)

    def test_registry_and_bounds_reject_invalid_execution(self):
        run = self.runner()
        cases = (
            ({}, {"pulses": 1}),
            ({"ping": None}, {"pulses": 1}),
            ({"ping": lambda db, e: None}, {"pulses": True}),
            ({"ping": lambda db, e: None}, {"pulses": 1001}),
            ({"ping": lambda db, e: None}, {"pulses": 1, "interval_seconds": float("nan")}),
            ({"ping": lambda db, e: None}, {"pulses": 1, "batch_limit": 0}),
            ({"ping": lambda db, e: None}, {"pulses": 1000, "batch_limit": 1000}),
        )
        for registry, kwargs in cases:
            with self.subTest(registry=registry, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    run(self.clock, registry, now=lambda: BASE,
                        monotonic=lambda: 0.0, sleep=lambda _: None, **kwargs)
        self.assertFalse(self.clock.lookup("nonexistent"))

    def test_runner_executes_real_host_authorized_engine_action(self):
        from realm.engine import Engine
        from realm.crafting import Crafting
        from realm.clock_gameplay import perform_in_transaction
        engine = Engine(self.path)
        engine.join("BotRunner")
        crafting = Crafting(self.path)
        crafting.place_node("atrium", "copper", stock=3, batch_yield=1)
        self.clock.schedule("gather1", due_at=BASE, kind="trusted_game_action",
                            payload={"actor": "BotRunner", "command": "gather copper"})
        def validated_host_handler(db, event):
            self.assertEqual("BotRunner", event.payload["actor"])
            perform_in_transaction(db, engine=engine, crafting=crafting,
                actor="BotRunner", command="gather copper", action_id=event.event_id)
        result = self.runner()(self.clock, {"trusted_game_action": validated_host_handler},
            pulses=1, now=lambda: BASE, monotonic=lambda: 0.0, sleep=lambda _: None)
        self.assertEqual(("gather1",), result.event_ids)
        with closing(sqlite3.connect(self.path)) as db:
            stock = db.execute("SELECT remaining FROM gathering_nodes WHERE material='copper'").fetchone()[0]
        self.assertEqual(2, stock)
