"""Host-only atomic ship departure and UTC arrival deadline registration.

This does not authenticate actors, choose fictional propulsion speeds, run a
clock daemon, or expose a player endpoint. The caller owns one world-bound
SQLite write transaction and its final commit/rollback.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .clock_dispatch import schedule_in_transaction
from .geography import Geography
from .world_clock import WorldClock


def depart_and_schedule_in_transaction(
    clock: WorldClock, geography: Geography, db: sqlite3.Connection,
    ship_id: str, destination: str, *, actor: str, now: int,
    action_id: str,
) -> dict:
    """Move a host-authorized ship into transit and enqueue one exact arrival.

    Both effects use the same existing transaction, protected by a joint
    SAVEPOINT so even a caught scheduling error cannot strand the ship.
    A stable action_id derives an idempotent per-world arrival event ID.
    """
    if not isinstance(geography, Geography):
        raise TypeError("Expected Geography for this world")
    if not isinstance(clock, WorldClock):
        raise TypeError("Expected WorldClock for this world")
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise RuntimeError("Existing SQLite transaction required")
    if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
        raise ValueError("Stable departure action_id required")
    event_key = json.dumps(
        [ship_id, actor, action_id], ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    event_id = "ship_arrival_" + hashlib.sha256(event_key).hexdigest()[:32]
    db.execute("SAVEPOINT realm_ship_depart_arrival")
    try:
        departure = geography.depart_in_transaction(
            db, ship_id, destination, actor=actor, now=now,
            action_id=action_id,
        )
        try:
            arrival_utc = datetime.fromtimestamp(
                departure["arrival_at"], tz=timezone.utc
            )
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("Arrival epoch exceeds representable UTC datetime") from exc
        event = schedule_in_transaction(
            clock, db, event_id, due_at=arrival_utc,
            kind="ship_arrival", payload={"ship_id": ship_id},
        )
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT realm_ship_depart_arrival")
        db.execute("RELEASE SAVEPOINT realm_ship_depart_arrival")
        raise
    db.execute("RELEASE SAVEPOINT realm_ship_depart_arrival")
    return {"departure": departure, "event_id": event.event_id}
