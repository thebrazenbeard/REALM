"""Host-only bearer-bound pilot command over a single world write transaction.

Existing REALM CredentialStore credentials are long-lived bearer tokens, not
short-lived sessions or device-bound proof. Only the registered ship owner can
pilot in this first slice; delegated crew authority is deliberately DENIED
until Geography can record the actual crew principal without impersonation.

The legacy host-only API remains available. A separate opt-in Gateway HTTP
ship departure route calls its scoped credential path; it neither migrates
worlds nor runs background ship movement.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from .auth import CredentialStore
from .gateway import Gateway
from .scoped_credentials import CredentialStateError
from .ship_clock_dispatch import depart_and_schedule_in_transaction
from .ship_gateway_authority import GatewayShipAuthorityCustody
from .ship_history_custody import ShipHistoryCustody
from .world_clock import DueEvent, WorldClock


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
               now: int, action_id: str, scoped: bool = False) -> dict:
        """Host-invoked transactional action; caller may not supply an actor."""
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            return self.depart_in_transaction(
                db, bearer_token, ship_id, destination, now=now, action_id=action_id,
                scoped=scoped,
            )

    def depart_in_transaction(self, db: sqlite3.Connection, bearer_token: str,
                              ship_id: str, destination: str, *,
                              now: int, action_id: str, scoped: bool = False) -> dict:
        """Bind token to principal inside caller-owned world write transaction.

        A token is resolved from `main.player_credentials` *after* acquiring a
        writer reservation; revocation and ship departure cannot race across
        independent connections. The caller must commit/rollback the outer tx.
        The legacy branch retains long-lived credentials; the scoped branch checks
        UTC expiry and ship:depart capability in the same world writer. Neither
        branch permits delegated crew piloting.
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
        # Bound complete persistent ship history before credential, roster,
        # clock and departure work in this writer transaction.
        custody = ShipHistoryCustody(db)
        authority = GatewayShipAuthorityCustody(db, scoped=scoped)
        if (not isinstance(bearer_token, str) or
                not 16 <= len(bearer_token) <= 255 or not bearer_token.isascii()):
            raise PermissionError("Invalid or revoked ship command credential")
        if scoped:
            if self.gateway.scoped_credentials is None:
                raise PermissionError("Scoped ship credentials are not enabled")
            try:
                actor = self.gateway.scoped_credentials.authenticate_in_writer(
                    db, bearer_token, required_capability="ship:depart",
                )
            except CredentialStateError as exc:
                raise PermissionError("Invalid or revoked ship command credential") from exc
            if actor is None:
                raise PermissionError("Invalid or revoked ship command credential")
        else:
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
        if scoped:
            # Both tick state and departure are observed under the SAME writer.
            # Geography schedules integer UTC seconds; compare that exact event
            # instant to the full-precision persisted tick, never a floored tick.
            # A tick in the same fractional second conservatively rejects.
            has_clock = db.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type='table' "
                "AND name='clock_state'"
            ).fetchone()
            if has_clock:
                last = db.execute(
                    "SELECT last_utc FROM main.clock_state WHERE id=1"
                ).fetchone()
                if last:
                    try:
                        last_tick = datetime.fromisoformat(str(last[0]).replace("Z", "+00:00"))
                    except (ValueError, TypeError) as exc:
                        raise RuntimeError("Invalid persisted world clock timestamp") from exc
                    if last_tick.tzinfo is None:
                        raise RuntimeError("Persisted world clock timestamp has no timezone")
                    if datetime.fromtimestamp(now, timezone.utc) < last_tick.astimezone(timezone.utc):
                        raise RuntimeError("Host UTC precedes last processed world tick")
        # Geography verifies exact owner equality and records this true actor.
        # Delegated crew roles are not yet supported: never impersonate owner.
        # Snapshot the complete persistent ship ledger BEFORE departure DML.
        # Triggers on the authenticated gateway writer can otherwise insert a
        # second forged event or rewrite an older rival event while the HTTP
        # request returns a successful receipt.
        prior_receipt = (
            custody.receipts.get((ship_id, actor, action_id))
            if custody.enabled else None
        )
        departure = depart_and_schedule_in_transaction(
            self.clock, self.gateway.geography, db, ship_id, destination,
            actor=actor, now=now, action_id=action_id,
        )
        if prior_receipt is not None:
            # Exact action-ID replay is intentionally once-only and may occur
            # after a ship arrived. It makes NO new ship-state/history effect.
            if (prior_receipt[3] != "depart:" + destination
                    or json.loads(prior_receipt[4]) != departure["departure"]):
                raise ValueError("Ship writer replay receipt diverged from prior effect")
            custody.before_commit(db)
        else:
            # Reuse the same typed ship-effect verifier as WorldClock, with
            # the authenticated actor already constrained to persisted owner.
            # Only the host UTC timestamp and scoped action are synthetic;
            # no ship_departure clock event is enqueued by a gateway request.
            custody.after_handler(
                db, DueEvent(
                    event_id=action_id,
                    due_at=datetime.fromtimestamp(now, timezone.utc),
                    kind="ship_departure",
                    payload={"ship_id": ship_id, "destination": destination},
                ),
            )
            custody.before_commit(db)
        # Final read-only snapshot proves this authenticated POST did not also
        # rebind world identity, mutate credential permissions, alter earlier
        # UTC receipts, or enqueue a second forged deadline through triggers.
        authority.verify(db, departure=departure, replay=prior_receipt is not None)
        return {"principal": actor, **departure}
