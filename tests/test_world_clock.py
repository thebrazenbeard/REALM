"""Tests for an explicitly enabled, durable host-owned UTC event clock."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.world_clock import ClockSkewError, WorldClock


UTC = timezone.utc
BASE = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "world.sqlite3"
        from realm.gateway import Gateway
        Gateway(self.path, world_id="testworld")
        self.clock = WorldClock(self.path)
        self.clock.enable()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE host_effects (event_id TEXT PRIMARY KEY, kind TEXT)")

    def _effect(self, db, event):
        db.execute("INSERT INTO host_effects VALUES (?,?)", (event.event_id, event.kind))

    def _effect_ids(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [x[0] for x in db.execute("SELECT event_id FROM host_effects ORDER BY event_id")]

    def test_constructor_has_no_schema_side_effect_and_enable_is_explicit(self):
        other = Path(self.temp.name) / "fresh.sqlite3"
        dormant = WorldClock(other)
        self.assertFalse(other.exists())
        with self.assertRaisesRegex(RuntimeError, "enabled"):
            dormant.schedule("later", due_at=BASE, kind="travel", payload={})
        self.assertFalse(other.exists())
        with self.assertRaisesRegex(RuntimeError, "bound"):
            dormant.enable()
        self.assertFalse(other.exists())
        from realm.gateway import Gateway
        Gateway(other, world_id="demo")
        dormant.enable()
        from realm.engine import Engine
        engine = Engine(other)
        self.assertIn("Welcome", engine.join("Alice"))

    def test_dormant_constructor_does_not_poison_worldpack_import(self):
        import json
        from realm.worldpack import import_worldpack
        path = Path(self.temp.name) / "imported.sqlite3"
        source = Path(self.temp.name) / "sample.json"
        source.write_text(json.dumps({
            "schema": "realm.worldpack.v1", "world_id": "sample",
            "name": "Sample World", "start_room": "shore",
            "rooms": [{"id": "shore", "name": "Shore",
                       "description": "A quiet inlet.", "exits": {}}],
            "items": []
        }), encoding="utf-8")
        dormant = WorldClock(path)
        self.assertFalse(path.exists())
        import_worldpack(path, source)
        dormant.enable()
        self.assertIsNone(dormant.lookup("not_here"))

    def test_deadline_equality_and_equal_time_order_are_deterministic(self):
        self.clock.schedule("zeta", due_at=BASE, kind="npc", payload={})
        self.clock.schedule("alpha", due_at=BASE, kind="npc", payload={})
        self.assertEqual([], self.clock.tick(BASE - timedelta(microseconds=1), self._effect))
        first = self.clock.tick(BASE, self._effect, limit=1)
        second = self.clock.tick(BASE, self._effect, limit=1)
        self.assertEqual(["alpha", "zeta"],
                         [event.event_id for event in first + second])

    def test_due_events_are_atomic_bounded_and_replay_safe_across_restarts(self):
        self.clock.schedule("later", due_at=BASE + timedelta(seconds=15),
                            kind="travel", payload={"vessel": "alpha"})
        self.clock.schedule("first", due_at=BASE, kind="weather", payload={"wind": 2})
        self.assertEqual([], self.clock.tick(BASE - timedelta(seconds=1), self._effect))
        one = self.clock.tick(BASE, self._effect, limit=1)
        self.assertEqual(["first"], [event.event_id for event in one])
        self.assertEqual(["first"], self._effect_ids())
        restarted = WorldClock(self.path)
        self.assertEqual([], restarted.tick(BASE, self._effect))
        self.assertEqual([], restarted.tick(BASE + timedelta(seconds=14), self._effect))
        two = restarted.tick(BASE + timedelta(seconds=15), self._effect)
        self.assertEqual(["later"], [event.event_id for event in two])
        self.assertEqual([], restarted.tick(BASE + timedelta(seconds=15), self._effect))
        self.assertEqual(["first", "later"], self._effect_ids())

    def test_same_idempotency_key_conflict_cannot_mutate_original_event(self):
        original = self.clock.schedule("ship_arrival", due_at=BASE, kind="travel",
                                       payload={"to": "moon"})
        retry = self.clock.schedule("ship_arrival", due_at=BASE, kind="travel",
                                    payload={"to": "moon"})
        self.assertEqual(original, retry)
        with self.assertRaisesRegex(ValueError, "different"):
            self.clock.schedule("ship_arrival", due_at=BASE, kind="travel",
                                payload={"to": "other"})
        self.assertEqual("moon", self.clock.lookup("ship_arrival").payload["to"])

    def test_failed_host_effect_rolls_back_both_receipt_and_effect(self):
        self.clock.schedule("danger", due_at=BASE, kind="injury", payload={})
        def fail_after_sql(db, event):
            self._effect(db, event)
            raise RuntimeError("host effect failed")
        with self.assertRaisesRegex(RuntimeError, "host effect failed"):
            self.clock.tick(BASE, fail_after_sql)
        self.assertEqual([], self._effect_ids())
        self.assertIsNone(self.clock.lookup("danger").processed_at)
        self.assertEqual(["danger"], [e.event_id for e in self.clock.tick(BASE, self._effect)])
        self.assertEqual(["danger"], self._effect_ids())

    def test_tick_handler_keeps_world_foreign_key_constraints(self):
        self.clock.schedule("invalid_host_effect", due_at=BASE, kind="npc", payload={})
        def invalid_player(db, event):
            db.execute("INSERT INTO players(id,room_id,created_at) VALUES (?,?,?)",
                       ("Ghost", "missing_room", BASE.isoformat()))
        with self.assertRaises(sqlite3.IntegrityError):
            self.clock.tick(BASE, invalid_player)
        self.assertIsNone(self.clock.lookup("invalid_host_effect").processed_at)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM players").fetchone()[0])

    def test_utc_normalization_and_invalid_inputs(self):
        eastern = timezone(timedelta(hours=-5))
        same_instant = BASE.astimezone(eastern)
        event = self.clock.schedule("normalized", due_at=same_instant,
                                    kind="weather", payload={"a": "b"})
        self.assertEqual(BASE, event.due_at)
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.clock.schedule("naive", due_at=BASE.replace(tzinfo=None),
                                kind="weather", payload={})
        with self.assertRaisesRegex(ValueError, "event_id"):
            self.clock.schedule("../evil", due_at=BASE, kind="weather", payload={})
        with self.assertRaisesRegex(ValueError, "payload"):
            self.clock.schedule("not_json", due_at=BASE, kind="weather", payload={"bad": float("nan")})
        with self.assertRaisesRegex(ValueError, "payload"):
            self.clock.schedule("bad_unicode", due_at=BASE, kind="weather", payload={"bad": "\ud800"})
        with self.assertRaisesRegex(ValueError, "limit"):
            self.clock.tick(BASE, self._effect, limit=0)

    def test_clock_rollback_and_large_leap_require_host_acknowledgment(self):
        self.assertEqual([], self.clock.tick(BASE, self._effect))
        with self.assertRaises(ClockSkewError):
            self.clock.tick(BASE - timedelta(microseconds=1), self._effect)
        with self.assertRaises(ClockSkewError):
            self.clock.tick(BASE + timedelta(days=3), self._effect)
        with self.assertRaises(ValueError):
            self.clock.tick(BASE + timedelta(days=3), self._effect,
                            acknowledge_forward_jump="false")
        with self.assertRaises(ValueError):
            self.clock.tick(BASE + timedelta(days=3), self._effect,
                            acknowledge_forward_jump=1)
        self.assertEqual([], self.clock.tick(BASE + timedelta(days=3),
                                             self._effect, acknowledge_forward_jump=True))
        with self.assertRaises(ClockSkewError):
            self.clock.tick(BASE + timedelta(days=2), self._effect)

    def test_concurrent_hosts_only_apply_each_due_effect_once(self):
        self.clock.schedule("shared", due_at=BASE, kind="npc", payload={})
        def compete(_):
            return WorldClock(self.path).tick(BASE, self._effect)
        with ThreadPoolExecutor(max_workers=2) as pool:
            wins = list(pool.map(compete, range(2)))
        self.assertEqual(1, sum(len(items) for items in wins))
        self.assertEqual(["shared"], self._effect_ids())

    def test_separate_world_databases_do_not_share_deadlines(self):
        other_path = Path(self.temp.name) / "world2.sqlite3"
        from realm.gateway import Gateway
        Gateway(other_path, world_id="secondworld")
        other = WorldClock(other_path)
        other.enable()
        self.clock.schedule("first", due_at=BASE, kind="npc", payload={})
        self.assertIsNone(other.lookup("first"))
        other.schedule("second", due_at=BASE, kind="npc", payload={})
        self.assertIsNone(self.clock.lookup("second"))
        self.assertEqual(["first"], [e.event_id for e in self.clock.tick(BASE, self._effect)])
        with closing(sqlite3.connect(other_path)) as db:
            ids = [row[0] for row in db.execute("SELECT event_id FROM clock_events")]
        self.assertEqual(["second"], ids)


if __name__ == "__main__":
    unittest.main()
