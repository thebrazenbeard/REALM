"""Synthetic protocol-spy checks for trusted host clock batch hooks.

WorldClock's atmosphere extensions live on a separate Draft branch. This
fixture verifies the adapter's callable-object protocol on the current stack;
real atmosphere/ship SQLite integration is qualified separately on a composed
source tree rather than falsely claiming it exists in this branch.
"""
from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.clock_runner import UnknownEventKind, _TrustedRegistryDispatcher, run_foreground
from realm.gateway import Gateway
from realm.world_clock import DueEvent, WorldClock


NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


class HookAwareClockSpy(WorldClock):
    """Exact hook ordering protocol, with no claim of real SQLite custody."""

    def __init__(self, db_path, selected, *, read_only_before=False):
        super().__init__(db_path)
        self.selected = tuple(selected)
        self.seen_callbacks = []
        self.read_only_before = read_only_before
        self.query_only_after = None

    def tick(self, now_utc, handler, *, limit=100, acknowledge_forward_jump=False):
        if not callable(getattr(handler, "begin_host_tick", None)):
            raise RuntimeError("Missing host tick begin hook")
        if not callable(getattr(handler, "verify_final_precommit", None)):
            raise RuntimeError("Missing host precommit finalizer")
        with self._connect() as db:
            if self.read_only_before:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
            else:
                db.execute("BEGIN IMMEDIATE")
            try:
                handler.begin_host_tick(db, self.selected)
                for event in self.selected:
                    self.seen_callbacks.append(handler(db, event))
                handler.verify_final_precommit(db, self.selected)
            finally:
                self.query_only_after = db.execute("PRAGMA query_only").fetchone()[0]
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

    def fake_clock(self, *kinds, read_only_before=False):
        events = [DueEvent("hook_" + str(i), NOW, kind, {})
                  for i, kind in enumerate(kinds, start=1)]
        return HookAwareClockSpy(self.db, events, read_only_before=read_only_before)

    def test_live_registered_handler_cannot_escape_world_transaction(self):
        for operation in ("commit", "rollback", "commit_then_begin",
                          "rollback_then_begin", "sql_commit", "sql_rollback",
                          "unbound_commit", "unbound_rollback",
                          "cursor_commit", "cursor_rollback", "cursor_script"):
            with self.subTest(operation=operation):
                # Each adversarial variant starts from the same durable world
                # even when a deliberately broken predecessor leaked COMMIT.
                with closing(sqlite3.connect(self.db)) as baseline:
                    baseline.execute("UPDATE main.world_meta SET value='hook_test' "
                                     "WHERE key='world_id'")
                    baseline.commit()
                event_id = "handler_escape_" + operation
                clock = WorldClock(self.db)
                clock.schedule(event_id, due_at=NOW, kind="trusted_escape", payload={})

                def escaping(db, event):
                    db.execute("UPDATE main.world_meta SET value='escaped' "
                               "WHERE key='world_id'")
                    if operation.startswith("sql_"):
                        db.execute(operation.removeprefix("sql_").upper())
                    elif operation.startswith("unbound_"):
                        getattr(sqlite3.Connection, operation.removeprefix("unbound_"))(db)
                    elif operation.startswith("cursor_"):
                        cursor = db.cursor()
                        if operation == "cursor_script":
                            cursor.executescript("SELECT 1;")
                        else:
                            cursor.execute(operation.removeprefix("cursor_").upper())
                    else:
                        getattr(db, operation.split("_then_")[0])()
                    if operation.endswith("_then_begin"):
                        db.execute("BEGIN IMMEDIATE")
                    raise RuntimeError("injected after forbidden transaction control")

                with self.assertRaisesRegex(RuntimeError, "transaction control"):
                    run_foreground(clock, {"trusted_escape": escaping}, pulses=1,
                                   now=lambda: NOW)
                with closing(sqlite3.connect(self.db)) as db:
                    self.assertEqual("hook_test", db.execute(
                        "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
                    self.assertIsNone(db.execute(
                        "SELECT processed_utc FROM clock_events WHERE event_id=?",
                        (event_id,)).fetchone()[0])

    def test_live_preparation_hook_cannot_commit_without_utc_receipt(self):
        class EscapingPreparation:
            def begin_host_tick(self, db, due_events):
                db.execute("UPDATE main.world_meta SET value='precommitted' "
                           "WHERE key='world_id'")
                db.commit()
                db.execute("BEGIN IMMEDIATE")

            def __call__(self, db, event):
                return event.event_id

            def verify_final_precommit(self, db, processed):
                return None

        clock = WorldClock(self.db)
        clock.schedule("preparation_escape", due_at=NOW, kind="escape", payload={})
        with self.assertRaisesRegex(RuntimeError, "transaction control"):
            run_foreground(clock, {"escape": EscapingPreparation()}, pulses=1,
                           now=lambda: NOW)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("hook_test", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
            self.assertIsNone(db.execute(
                "SELECT processed_utc FROM clock_events "
                "WHERE event_id='preparation_escape'").fetchone()[0])

    def test_live_nested_savepoints_remain_allowed(self):
        clock = WorldClock(self.db)
        clock.schedule("safe_nested_savepoint", due_at=NOW,
                       kind="savepoint", payload={})

        def scoped(db, event):
            db.execute("SAVEPOINT realm_scoped_gameplay")
            db.execute("UPDATE main.world_meta SET value='safe_scoped' "
                       "WHERE key='world_id'")
            db.execute("RELEASE SAVEPOINT realm_scoped_gameplay")
            return event.event_id

        report = run_foreground(clock, {"savepoint": scoped}, pulses=1,
                                now=lambda: NOW)
        self.assertEqual(("safe_nested_savepoint",), report.event_ids)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("safe_scoped", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
            self.assertIsNotNone(db.execute(
                "SELECT processed_utc FROM clock_events "
                "WHERE event_id='safe_nested_savepoint'").fetchone()[0])

    def test_preinstalled_host_authorizer_survives_trusted_handler_guard(self):
        def deny_world_meta(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_UPDATE and arg1 == "world_meta":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        class PolicyClock(WorldClock):
            @contextmanager
            def _connect(self):
                with super()._connect() as db:
                    db.set_authorizer(deny_world_meta)
                    yield db

        clock = PolicyClock(self.db)
        clock.schedule("policy_host_action", due_at=NOW, kind="policy", payload={})

        def policy_handler(db, event):
            db.execute("UPDATE main.world_meta SET value='not_authorized' "
                       "WHERE key='world_id'")
            return event.event_id

        with self.assertRaises(sqlite3.DatabaseError):
            run_foreground(clock, {"policy": policy_handler}, pulses=1, now=lambda: NOW)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("hook_test", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
            self.assertIsNone(db.execute(
                "SELECT processed_utc FROM clock_events "
                "WHERE event_id='policy_host_action'").fetchone()[0])

    def test_begin_hook_host_authorizer_is_preserved_across_handler_and_finalizer(self):
        class HostPolicy:
            def begin_host_tick(self, db, due_events):
                def deny_world_meta(action, arg1, arg2, database, source):
                    if action == sqlite3.SQLITE_UPDATE and arg1 == "world_meta":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(deny_world_meta)

            def __call__(self, db, event):
                db.execute("UPDATE main.world_meta SET value='forbidden' "
                           "WHERE key='world_id'")
                return event.event_id

            def verify_final_precommit(self, db, processed):
                return None

        clock = WorldClock(self.db)
        clock.schedule("policy_begin_hook", due_at=NOW, kind="policy_hook", payload={})
        with self.assertRaises(sqlite3.DatabaseError):
            run_foreground(clock, {"policy_hook": HostPolicy()}, pulses=1, now=lambda: NOW)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("hook_test", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
            self.assertIsNone(db.execute(
                "SELECT processed_utc FROM clock_events "
                "WHERE event_id='policy_begin_hook'").fetchone()[0])

    def test_swallowed_transaction_denial_still_rejects_entire_tick(self):
        clock = WorldClock(self.db)
        clock.schedule("swallowed_commit", due_at=NOW, kind="swallower", payload={})

        def swallower(db, event):
            db.execute("UPDATE main.world_meta SET value='should_rollback' "
                       "WHERE key='world_id'")
            try:
                db.commit()
            except sqlite3.DatabaseError:
                pass
            return event.event_id

        with self.assertRaisesRegex(RuntimeError, "transaction control"):
            run_foreground(clock, {"swallower": swallower}, pulses=1,
                           now=lambda: NOW)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("hook_test", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
            self.assertIsNone(db.execute(
                "SELECT processed_utc FROM clock_events "
                "WHERE event_id='swallowed_commit'").fetchone()[0])

    def test_executescript_implicit_commit_is_denied_before_execution(self):
        clock = WorldClock(self.db)
        clock.schedule("script_escape", due_at=NOW, kind="script", payload={})

        def script(db, event):
            db.execute("UPDATE main.world_meta SET value='should_rollback' "
                       "WHERE key='world_id'")
            db.executescript("SELECT 1;")
            return event.event_id

        with self.assertRaisesRegex(RuntimeError, "transaction control"):
            run_foreground(clock, {"script": script}, pulses=1,
                           now=lambda: NOW)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("hook_test", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])
            self.assertIsNone(db.execute(
                "SELECT processed_utc FROM clock_events "
                "WHERE event_id='script_escape'").fetchone()[0])

    def test_finalizer_cannot_rollback_owner_transaction(self):
        # A native SQLite transaction, not the protocol-spy fake clock. The
        # combined #137 WorldClock path receives its own independent probe.
        class RollbackFinalizer(HookOwner):
            def verify_final_precommit(self, db, processed):
                db.rollback()
        dispatcher = _TrustedRegistryDispatcher({"trusted": RollbackFinalizer("rollback", [])})
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE main.world_meta SET value='transient' WHERE key='world_id'")
            with self.assertRaisesRegex(RuntimeError, "remain read-only"):
                dispatcher.verify_final_precommit(db, ())
            self.assertTrue(db.in_transaction)
            db.rollback()
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("hook_test", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])

    def test_finalizer_cannot_commit_before_crash(self):

        class CommitThenCrash(HookOwner):
            def verify_final_precommit(self, db, processed):
                db.commit()
                raise RuntimeError("injected host crash")

        dispatcher = _TrustedRegistryDispatcher({"trusted": CommitThenCrash("commit", [])})
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE main.world_meta SET value='transient' WHERE key='world_id'")
            with self.assertRaisesRegex(RuntimeError, "remain read-only"):
                dispatcher.verify_final_precommit(db, ())
            self.assertTrue(db.in_transaction)
            db.rollback()
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("hook_test", db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0])

    def test_finalizer_sql_transaction_control_is_denied_before_execution(self):
        for statement in ("COMMIT", "ROLLBACK", "END", "SAVEPOINT escaped",
                          "ROLLBACK TO SAVEPOINT escaped", "RELEASE SAVEPOINT escaped",
                          "BEGIN"):
            with self.subTest(statement=statement):
                class SqlTransactionFinalizer(HookOwner):
                    def verify_final_precommit(self, db, processed):
                        db.execute(statement)

                dispatcher = _TrustedRegistryDispatcher({
                    "trusted": SqlTransactionFinalizer("sql", [])})
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("BEGIN IMMEDIATE")
                    with self.assertRaisesRegex(RuntimeError, "remain read-only"):
                        dispatcher.verify_final_precommit(db, ())
                    self.assertTrue(db.in_transaction)
                    db.rollback()

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

    def test_later_trusted_finalizer_cannot_write_after_earlier_attestation(self):
        trace = []
        first = HookOwner("attestor", trace)
        class LateWriter(HookOwner):
            def verify_final_precommit(self, db, processed):
                trace.append(("late", "attempt-write", tuple(e.event_id for e in processed)))
                db.execute("INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
                           "VALUES(?,?,?,?,?)", (NOW.isoformat(), "host", "atrium", "say", '{}'))
        late = LateWriter("late", trace)
        clock = self.fake_clock("attested", "audit")
        with self.assertRaisesRegex(RuntimeError, "remain read-only"):
            run_foreground(clock, {"attested": first, "audit": late},
                           pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                           sleep=lambda _: None)
        self.assertEqual(0, clock.query_only_after,
                         "Read-only phase must restore the connection on error")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertEqual([("attestor", "begin", ("hook_1", "hook_2")),
                          ("late", "begin", ("hook_1", "hook_2")),
                          ("attestor", "handle", "hook_1"),
                          ("late", "handle", "hook_2"),
                          ("attestor", "final", ("hook_1", "hook_2")),
                          ("late", "attempt-write", ("hook_1", "hook_2"))], trace)
        # A genuine new host transaction can proceed after the rejected one.
        retry = self.fake_clock("attested")
        report = run_foreground(retry, {"attested": HookOwner("retry", [])},
                                pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                                sleep=lambda _: None)
        self.assertEqual(("hook_1",), report.event_ids)

    def test_readonly_finalizers_observe_protected_phase_and_prior_flag(self):
        for previous in (False, True):
            with self.subTest(previous=previous):
                seen = []
                class Reader(HookOwner):
                    def verify_final_precommit(self, db, processed):
                        seen.append((db.execute("PRAGMA query_only").fetchone()[0],
                                     db.execute("SELECT COUNT(*) FROM main.events").fetchone()[0]))
                owner = Reader("reader", [])
                clock = self.fake_clock("trusted", read_only_before=previous)
                report = run_foreground(clock, {"trusted": owner}, pulses=1,
                                        now=lambda: NOW, monotonic=lambda: 1.0,
                                        sleep=lambda _: None)
                self.assertEqual(("hook_1",), report.event_ids)
                self.assertEqual([(1, 0)], seen)
                self.assertEqual(int(previous), clock.query_only_after)

    def test_nested_trusted_registry_keeps_one_readonly_finalizer_phase(self):
        trace = []
        owner = HookOwner("nested", trace)
        inner = _TrustedRegistryDispatcher({"trusted": owner})
        clock = self.fake_clock("trusted")
        report = run_foreground(clock, {"trusted": inner}, pulses=1,
                                now=lambda: NOW, monotonic=lambda: 1.0,
                                sleep=lambda _: None)
        self.assertEqual(("hook_1",), report.event_ids)
        self.assertEqual([("nested", "begin", ("hook_1",)),
                          ("nested", "handle", "hook_1"),
                          ("nested", "final", ("hook_1",))], trace)
        self.assertEqual(0, clock.query_only_after)

    def test_finalizer_disabling_readonly_then_writing_still_rolls_back(self):
        for restore_before_return in (False, True):
            with self.subTest(restore_before_return=restore_before_return):
                class EvadingWriter(HookOwner):
                    def verify_final_precommit(self, db, processed):
                        db.execute("PRAGMA query_only=OFF")
                        db.execute("INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                                   "VALUES(?,?,?,?,?)", (NOW.isoformat(), "host", "atrium", "say", '{}'))
                        if restore_before_return:
                            db.execute("PRAGMA query_only=ON")
                clock = self.fake_clock("trusted")
                with self.assertRaisesRegex(RuntimeError, "remain read-only"):
                    run_foreground(clock, {"trusted": EvadingWriter("evading", [])},
                                   pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                                   sleep=lambda _: None)
                self.assertEqual(0, clock.query_only_after)
                with closing(sqlite3.connect(self.db)) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM main.events").fetchone()[0])

    def test_readonly_phase_rejects_temp_schema_and_attached_database_dml(self):
        # All SQLite schema namespaces share the connection's query_only flag.
        # ATTACH to an in-memory database avoids creating an external file.
        for sql in ("CREATE TEMP TABLE late_proof(value INTEGER)",
                    "ATTACH DATABASE ':memory:' AS readonly_attached",):
            with self.subTest(sql=sql):
                class SchemaWriter(HookOwner):
                    def verify_final_precommit(self, db, processed):
                        db.execute(sql)
                        if sql.startswith("ATTACH"):
                            db.execute("CREATE TABLE readonly_attached.late_proof(value INTEGER)")
                clock = self.fake_clock("trusted")
                with self.assertRaisesRegex(RuntimeError, "remain read-only"):
                    run_foreground(clock, {"trusted": SchemaWriter("writer", [])},
                                   pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                                   sleep=lambda _: None)
                self.assertEqual(0, clock.query_only_after)

    def test_finalizer_toggle_cannot_hide_durable_schema_or_pragma_changes(self):
        hostile_sql = (
            "CREATE TABLE main.finalizer_poison(value INTEGER)",
            "CREATE VIEW main.finalizer_poison AS SELECT 1 AS value",
            "CREATE INDEX main.finalizer_poison ON events(kind)",
            "CREATE TRIGGER main.finalizer_poison AFTER INSERT ON events BEGIN SELECT 1; END",
            "ALTER TABLE main.events ADD COLUMN finalizer_poison INTEGER",
            "DROP TABLE main.events",
            "CREATE TEMP TABLE finalizer_poison(value INTEGER)",
            "PRAGMA main.user_version=31337",
            "PRAGMA main.application_id=31337",
            "PRAGMA main.schema_version=31337",
        )
        for sql in hostile_sql:
            with self.subTest(sql=sql):
                class SchemaEvasion(HookOwner):
                    def verify_final_precommit(self, db, processed):
                        db.execute("PRAGMA query_only=OFF")
                        db.execute(sql)
                        db.execute("PRAGMA query_only=ON")
                clock = self.fake_clock("trusted")
                with self.assertRaisesRegex(RuntimeError, "remain read-only"):
                    run_foreground(clock, {"trusted": SchemaEvasion("schema", [])},
                                   pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                                   sleep=lambda _: None)
                self.assertEqual(0, clock.query_only_after)
                with closing(sqlite3.connect(self.db)) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM main.events").fetchone()[0])
                    self.assertFalse(db.execute("SELECT 1 FROM main.sqlite_schema "
                                                "WHERE name='finalizer_poison'").fetchone())
                    self.assertEqual(0, db.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual(0, db.execute("PRAGMA application_id").fetchone()[0])

    def test_new_attached_database_is_blocked_before_external_file_creation(self):
        external = self.db.parent / "trusted-callback-attached.sqlite3"
        class AttachedWriter(HookOwner):
            def verify_final_precommit(self, db, processed):
                db.execute("PRAGMA query_only=OFF")
                db.execute("ATTACH DATABASE ? AS injected", (str(external),))
                db.execute("CREATE TABLE injected.finalizer_poison(value INTEGER)")
                db.execute("PRAGMA query_only=ON")
        clock = self.fake_clock("trusted")
        with self.assertRaisesRegex(RuntimeError, "remain read-only"):
            run_foreground(clock, {"trusted": AttachedWriter("attached", [])},
                           pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                           sleep=lambda _: None)
        self.assertEqual(0, clock.query_only_after)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM main.events").fetchone()[0])
        # The authorizer denies mutating PRAGMA / ATTACH before execution.
        self.assertFalse(external.exists())

    def test_udf_ddl_is_rejected_when_trusted_finalizer_toggles_readonly(self):
        class UdfWriter(HookOwner):
            def verify_final_precommit(self, db, processed):
                db.create_function("finalizer_ddl", 0, lambda: db.execute(
                    "CREATE TABLE main.finalizer_poison(value INTEGER)").rowcount)
                db.execute("PRAGMA query_only=OFF")
                db.execute("SELECT finalizer_ddl()").fetchone()
                db.execute("PRAGMA query_only=ON")
        clock = self.fake_clock("trusted")
        with self.assertRaisesRegex(RuntimeError, "remain read-only"):
            run_foreground(clock, {"trusted": UdfWriter("udf", [])},
                           pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                           sleep=lambda _: None)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_schema "
                                        "WHERE name='finalizer_poison'").fetchone())

    def test_attached_file_side_effect_can_escape_world_receipt_custody(self):
        # Document the actual boundary, not a fictional SQLite/Python sandbox.
        # Unrestricted trusted Python can deliberately bypass the managed
        # sqlite3.Connection subclass using the native base-type method,
        # create a file, detach it, and leave no in-world world-state delta.
        external = self.db.parent / "trusted-attached-then-detached.sqlite3"
        class ExternalHostEffect(HookOwner):
            def verify_final_precommit(self, db, processed):
                sqlite3.Connection.set_authorizer(db, None)
                db.execute("PRAGMA query_only=OFF")
                db.execute("ATTACH DATABASE ? AS injected", (str(external),))
                db.execute("DETACH DATABASE injected")
                db.execute("PRAGMA query_only=ON")
        clock = self.fake_clock("trusted")
        report = run_foreground(clock, {"trusted": ExternalHostEffect("external", [])},
                                pulses=1, now=lambda: NOW, monotonic=lambda: 1.0,
                                sleep=lambda _: None)
        self.assertEqual(("hook_1",), report.event_ids)
        self.assertTrue(external.exists(), "External filesystem effect is outside world custody")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM main.events").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
