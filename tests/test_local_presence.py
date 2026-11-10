"""Host-only session presence lease: negative auth, replay and lifetime tests."""

from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
import threading
from contextlib import closing
from pathlib import Path

from realm.auth import CredentialStore
from realm.gateway import Gateway
from realm.local_presence import LocalPresenceWitness, PresenceDenied


class LocalPresenceWitnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = Gateway(Path(self.temp.name) / "world.sqlite3", world_id="trial")
        self.engine = self.gateway.engine
        self.engine.join("Ada")
        self.engine.join("Bob")
        self.engine.join("Eve")
        self.tokens = {
            name: self.gateway.credentials.provision(name)
            for name in ("Ada", "Bob", "Eve")
        }
        self.now = [100.0]
        self.live = LocalPresenceWitness(
            self.engine, "trial", idle_seconds=10, monotonic=lambda: self.now[0]
        )

    def principal(self, name: str):
        digest = CredentialStore._digest(self.tokens[name])

        def resolve(db: sqlite3.Connection) -> str | None:
            row = db.execute(
                "SELECT player_name FROM main.player_credentials WHERE token_digest=?",
                (digest,),
            ).fetchone()
            return None if row is None else str(row[0])

        return resolve

    def lease(self, name: str, protocol: str = "telnet-player-credential"):
        return self.live.admit(
            name, protocol=protocol, resolve_principal=self.principal(name),
        )

    def require(self, *, witness: LocalPresenceWitness | None = None):
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            (witness or self.live).require_pair_in_writer(db, "Ada", "Bob")

    def test_missing_or_only_one_session_denied(self):
        with self.assertRaises(PresenceDenied):
            self.require()
        ada = self.lease("Ada")
        with self.assertRaises(PresenceDenied):
            self.require()
        ada.close()

    def test_both_authorized_living_leases_pass_with_casefold(self):
        ada = self.lease("Ada")
        bob = self.lease("Bob", "http-scoped-request")
        self.require()
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.live.require_pair_in_writer(db, "aDa", "bOb")
        ada.close()
        bob.close()
        with self.assertRaises(PresenceDenied):
            self.require()

    def test_wrong_principal_token_and_guard_without_identity_fail(self):
        with self.assertRaises(PresenceDenied):
            self.live.admit(
                "Bob", protocol="telnet-player-credential",
                resolve_principal=self.principal("Ada"),
            )
        with self.assertRaises(PresenceDenied):
            self.live.admit(
                "Ada", protocol="telnet-player-credential",
                resolve_principal=lambda db: None,
            )
        with self.assertRaises(PresenceDenied):
            self.live.admit(
                "Ada", protocol="telnet-player-credential",
                resolve_principal=lambda db: False,
            )
        with self.assertRaises(PresenceDenied):
            self.live.admit(
                "Ada", protocol="unknown",
                resolve_principal=self.principal("Ada"),
            )

    def test_revocation_and_rotation_fail_at_same_writer_boundary(self):
        ada = self.lease("Ada")
        bob = self.lease("Bob")
        self.require()
        self.gateway.credentials.rotate("Bob")
        with self.assertRaises(PresenceDenied):
            self.require()
        with self.assertRaises(PresenceDenied):
            bob.refresh()
        self.gateway.credentials.revoke("Ada")
        with self.assertRaises(PresenceDenied):
            ada.refresh()

    def test_idleness_cannot_be_resurrected_by_late_refresh(self):
        ada = self.lease("Ada")
        bob = self.lease("Bob")
        self.now[0] = 109.9
        ada.refresh()
        bob.refresh()
        self.require()
        self.now[0] = 120.0
        with self.assertRaises(PresenceDenied):
            self.require()
        with self.assertRaises(PresenceDenied):
            ada.refresh()
        with self.assertRaises(PresenceDenied):
            bob.refresh()

    def test_monotonic_rollback_does_not_refresh_stale_leases(self):
        ada = self.lease("Ada")
        bob = self.lease("Bob")
        self.now[0] = 99.0
        with self.assertRaises(PresenceDenied):
            self.require()
        with self.assertRaises(PresenceDenied):
            ada.refresh()
        with self.assertRaises(PresenceDenied):
            bob.refresh()

    def test_invalid_monotonic_clock_cannot_keep_a_lease_alive(self):
        self.lease("Ada")
        self.lease("Bob")
        self.now[0] = math.nan
        with self.assertRaises(PresenceDenied):
            self.require()
        with self.assertRaises(PresenceDenied):
            self.lease("Eve")

    def test_writer_then_refresh_cannot_deadlock_against_combat_check(self):
        """Deterministic ABBA: A holds DB writer; B tries to refresh.

        A then acquires the witness memory lock while B waits for the DB.
        A legacy refresh held the memory lock *before* trying the DB writer,
        deadlocking both paths. Worker timeout is a failing red control.
        """
        ada = self.lease("Ada")
        self.lease("Bob")
        have_writer = threading.Event()
        refresh_entered_connect = threading.Event()
        failures: list[BaseException] = []
        original_connect = self.engine._connect

        def traced_connect():
            if threading.current_thread().name == "presence-refresh":
                refresh_entered_connect.set()
            return original_connect()

        self.engine._connect = traced_connect

        def combat_writer():
            try:
                with closing(original_connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    have_writer.set()
                    if not refresh_entered_connect.wait(timeout=2):
                        raise AssertionError("Refresh did not reach SQLite connection")
                    self.live.require_pair_in_writer(db, "Ada", "Bob")
            except BaseException as exc:
                failures.append(exc)

        def refresh_worker():
            try:
                if not have_writer.wait(timeout=2):
                    raise AssertionError("Combat writer did not acquire SQLite")
                ada.refresh()
            except BaseException as exc:
                failures.append(exc)

        writer = threading.Thread(target=combat_writer, daemon=True, name="presence-combat")
        refresh = threading.Thread(target=refresh_worker, daemon=True, name="presence-refresh")
        writer.start()
        refresh.start()
        writer.join(timeout=3)
        refresh.join(timeout=3)
        self.assertFalse(
            writer.is_alive() or refresh.is_alive(),
            "ABBA: SQLite writer and in-process presence mutex deadlocked",
        )
        self.assertEqual([], failures)

    def test_deferred_combat_transaction_cannot_invert_writer_mutex_order(self):
        """A deferred BEGIN must reserve SQLite before presence's mutex."""
        ada = self.lease("Ada")
        self.lease("Bob")
        refresh_owns_writer = threading.Event()
        deferred_check_reached = threading.Event()
        failures: list[BaseException] = []
        original_world_bound = self.live._world_bound

        def traced_world_bound(db):
            if threading.current_thread().name == "presence-deferred-refresh":
                original_world_bound(db)
                refresh_owns_writer.set()
                if not deferred_check_reached.wait(timeout=2):
                    raise AssertionError("Deferred combat check was not scheduled")
                return
            if threading.current_thread().name == "presence-deferred-combat":
                deferred_check_reached.set()
            return original_world_bound(db)

        self.live._world_bound = traced_world_bound

        def refresh_worker():
            try:
                ada.refresh()
            except BaseException as exc:
                failures.append(exc)

        def deferred_combat():
            try:
                with closing(self.engine._connect()) as db, db:
                    db.execute("BEGIN")
                    self.live.require_pair_in_writer(db, "Ada", "Bob")
            except BaseException as exc:
                failures.append(exc)

        refresh = threading.Thread(
            target=refresh_worker, daemon=True, name="presence-deferred-refresh",
        )
        combat = threading.Thread(
            target=deferred_combat, daemon=True, name="presence-deferred-combat",
        )
        refresh.start()
        self.assertTrue(refresh_owns_writer.wait(timeout=2))
        combat.start()
        refresh.join(timeout=3)
        combat.join(timeout=3)
        self.assertFalse(
            refresh.is_alive() or combat.is_alive(),
            "ABBA: deferred combat attempted writer upgrade under presence mutex",
        )
        self.assertEqual([], failures)

    def test_close_while_refresh_waits_on_writer_cannot_regrant(self):
        ada = self.lease("Ada")
        entered_connect = threading.Event()
        failures: list[BaseException] = []
        original_connect = self.engine._connect

        def traced_connect():
            if threading.current_thread().name == "presence-refresh-close":
                entered_connect.set()
            return original_connect()

        self.engine._connect = traced_connect

        def refresh_worker():
            try:
                ada.refresh()
            except BaseException as exc:
                failures.append(exc)

        with closing(original_connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(
                target=refresh_worker, daemon=True, name="presence-refresh-close",
            )
            worker.start()
            self.assertTrue(entered_connect.wait(timeout=2))
            # A closed lease must not be resurrected by a refresh that had
            # started but was waiting on the credential writer reservation.
            ada.close()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], PresenceDenied)
        with self.assertRaises(PresenceDenied):
            ada.refresh()

    def test_rotation_while_refresh_waits_on_writer_denies_old_token(self):
        ada = self.lease("Ada")
        entered_connect = threading.Event()
        failures: list[BaseException] = []
        original_connect = self.engine._connect

        def traced_connect():
            if threading.current_thread().name == "presence-refresh-rotation":
                entered_connect.set()
            return original_connect()

        self.engine._connect = traced_connect

        def refresh_worker():
            try:
                ada.refresh()
            except BaseException as exc:
                failures.append(exc)

        with closing(original_connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(
                target=refresh_worker, daemon=True, name="presence-refresh-rotation",
            )
            worker.start()
            self.assertTrue(entered_connect.wait(timeout=2))
            db.execute(
                "UPDATE player_credentials SET token_digest=? WHERE player_name='Ada'",
                (CredentialStore._digest("rotated-test-token-not-issued-to-session"),),
            )
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], PresenceDenied)
        with self.assertRaises(PresenceDenied):
            ada.refresh()

    def test_idle_expiry_during_writer_wait_blocks_late_refresh(self):
        ada = self.lease("Ada")
        entered_connect = threading.Event()
        failures: list[BaseException] = []
        original_connect = self.engine._connect

        def traced_connect():
            if threading.current_thread().name == "presence-refresh-idle":
                entered_connect.set()
            return original_connect()

        self.engine._connect = traced_connect

        def refresh_worker():
            try:
                ada.refresh()
            except BaseException as exc:
                failures.append(exc)

        with closing(original_connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(
                target=refresh_worker, daemon=True, name="presence-refresh-idle",
            )
            worker.start()
            self.assertTrue(entered_connect.wait(timeout=2))
            self.now[0] = 111.0
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], PresenceDenied)

    def test_bounded_lingering_http_leases_prune_only_when_expired(self):
        live = LocalPresenceWitness(
            self.engine, "trial", idle_seconds=10, max_sessions=2,
            monotonic=lambda: self.now[0],
        )
        self.assertEqual(10, live.idle_seconds)
        self.assertEqual(2, live.max_sessions)
        def admit(name):
            return live.admit(
                name, protocol="http-scoped-request",
                resolve_principal=self.principal(name),
            )

        ada = admit("Ada")
        bob = admit("Bob")
        with self.assertRaisesRegex(PresenceDenied, "capacity"):
            admit("Eve")
        ada.close()
        eve = admit("Eve")
        self.now[0] = 111.0
        # A stale retained HTTP lease cannot consume capacity indefinitely.
        renewed = admit("Ada")
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(PresenceDenied):
                live.require_pair_in_writer(db, "Bob", "Eve")
        with self.assertRaises(PresenceDenied):
            bob.refresh()
        with self.assertRaises(PresenceDenied):
            eve.refresh()
        renewed.close()

    def test_failed_credential_refresh_releases_bounded_slot_immediately(self):
        live = LocalPresenceWitness(
            self.engine, "trial", idle_seconds=10, max_sessions=2,
            monotonic=lambda: self.now[0],
        )
        ada = live.admit(
            "Ada", protocol="http-scoped-request",
            resolve_principal=self.principal("Ada"),
        )
        bob = live.admit(
            "Bob", protocol="http-scoped-request",
            resolve_principal=self.principal("Bob"),
        )
        self.gateway.credentials.rotate("Bob")
        with self.assertRaises(PresenceDenied):
            bob.refresh()
        # Bob's invalid session must free capacity even before TTL elapses.
        eve = live.admit(
            "Eve", protocol="http-scoped-request",
            resolve_principal=self.principal("Eve"),
        )
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            live.require_pair_in_writer(db, "Ada", "Eve")
        ada.close()
        eve.close()

    def test_closed_foreign_and_replayed_lease_cannot_refresh(self):
        ada = self.lease("Ada")
        ada.close()
        ada.close()
        with self.assertRaises(PresenceDenied):
            ada.refresh()
        foreign = LocalPresenceWitness(self.engine, "trial", monotonic=lambda: self.now[0])
        with self.assertRaises(PresenceDenied):
            foreign.refresh(ada)
        with self.assertRaises(PresenceDenied):
            foreign.close(ada)
        with self.assertRaises(PresenceDenied):
            self.require(witness=foreign)

    def test_world_and_database_binding_deny_cross_world_reuse(self):
        self.lease("Ada")
        self.lease("Bob")
        wrong_world = LocalPresenceWitness(self.engine, "other", monotonic=lambda: self.now[0])
        with self.assertRaises(PresenceDenied):
            wrong_world.admit(
                "Ada", protocol="telnet-player-credential",
                resolve_principal=self.principal("Ada"),
            )
        other = Gateway(Path(self.temp.name) / "other.sqlite3", world_id="trial")
        other.engine.join("Ada")
        other.engine.join("Bob")
        with closing(other.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(PresenceDenied):
                self.live.require_pair_in_writer(db, "Ada", "Bob")

    def test_requires_writer_and_preexisting_join(self):
        with closing(self.engine._connect()) as db:
            with self.assertRaises(PresenceDenied):
                self.live.require_pair_in_writer(db, "Ada", "Bob")
        token = self.gateway.credentials.provision("Nojoin")
        digest = CredentialStore._digest(token)
        with self.assertRaises(PresenceDenied):
            self.live.admit(
                "Nojoin", protocol="telnet-player-credential",
                resolve_principal=lambda db: db.execute(
                    "SELECT player_name FROM player_credentials WHERE token_digest=?",
                    (digest,),
                ).fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
