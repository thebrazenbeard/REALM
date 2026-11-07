"""Host-only bearer-bound pilot command over a single world write transaction.

Existing REALM CredentialStore credentials are long-lived bearer tokens, not
short-lived sessions or device-bound proof. Only the registered ship owner can
pilot in this first slice; delegated crew authority is deliberately DENIED
until Geography can record the actual crew principal without impersonation.

No player HTTP route, world migration, or background ship movement is created.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .auth import CredentialStore
from .gateway import Gateway
from .ship_clock_dispatch import depart_and_schedule_in_transaction
from .world_clock import WorldClock


class AuthenticatedShipCommands:
    """Execute registered-owner departures after *same-transaction* token lookup."""

    def __init__(self, gateway: Gateway, clock: WorldClock):
        if not isinstance(gateway, Gateway) or not isinstance(clock, WorldClock):
            raise TypeError("Expected a bound Gateway and WorldClock")
        self.gateway = gateway
        self.clock = clock
        self.db_path = Path(gateway.engine.db_path).resolve()
        if Path(gateway.geography.db_path).resolve() != self.db_path:
            raise ValueError("Geography points to another world")
        if Path(clock.db_path).resolve() != self.db_path:
            raise ValueError("Clock points to another world")

    def depart(self, bearer_token: str, ship_id: str, destination: str, *,
               now: int, action_id: str) -> dict:
        """Host-invoked transactional action; caller may not supply an actor."""
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            return self.depart_in_transaction(
                db, bearer_token, ship_id, destination, now=now, action_id=action_id,
            )

    def depart_in_transaction(self, db: sqlite3.Connection, bearer_token: str,
                              ship_id: str, destination: str, *,
                              now: int, action_id: str) -> dict:
        """Bind token to principal inside caller-owned world write transaction.

        A token is resolved from `main.player_credentials` *after* acquiring a
        writer reservation; revocation and ship departure cannot race across
        independent connections. The caller must commit/rollback the outer tx.
        This does not implement credential expiry or delegated crew piloting.
        """
        if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
            raise RuntimeError("An existing SQLite transaction is required")
        files = [Path(row[2]).resolve() for row in db.execute("PRAGMA database_list")
                 if row[1] == "main" and row[2]]
        if files != [self.db_path]:
            raise ValueError("Connection does not belong to this world")
        if db.row_factory is not sqlite3.Row:
            raise ValueError("Connection must use sqlite3.Row")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Foreign keys must be enabled")
        if db.execute("PRAGMA query_only").fetchone()[0] != 0:
            raise RuntimeError("Read-only transaction cannot authorize a ship command")
        # Upgrade any deferred transaction to a writer lock without touching
        # world rows or firing UPDATE triggers. This serializes token rotation.
        db.execute("UPDATE main.world_meta SET value=value WHERE 0")
        world = db.execute(
            "SELECT value FROM main.world_meta WHERE key='world_id'"
        ).fetchone()
        if world is None or world[0] != self.gateway.world_id:
            raise ValueError("World identity mismatch")
        if (not isinstance(bearer_token, str) or
                not 16 <= len(bearer_token) <= 255 or not bearer_token.isascii()):
            raise PermissionError("Invalid or revoked ship command credential")
        principal = db.execute(
            "SELECT player_name FROM main.player_credentials WHERE token_digest=?",
            (CredentialStore._digest(bearer_token),),
        ).fetchone()
        if principal is None:
            raise PermissionError("Invalid or revoked ship command credential")
        actor = principal[0]
        joined = db.execute("SELECT 1 FROM main.players WHERE id=?", (actor,)).fetchone()
        if joined is None:
            raise PermissionError("Ship pilot must first join this world")
        # Geography verifies exact owner equality and records this true actor.
        # Delegated crew roles are not yet supported: never impersonate owner.
        departure = depart_and_schedule_in_transaction(
            self.clock, self.gateway.geography, db, ship_id, destination,
            actor=actor, now=now, action_id=action_id,
        )
        return {"principal": actor, **departure}
