"""Host-only same-transaction scheduling of REALM UTC follow-up events.

WorldClock.schedule() opens a separate SQLite connection; calling it inside
WorldClock.tick() would contend with the tick's BEGIN IMMEDIATE transaction.
This adapter validates and inserts using the caller's existing SAME-world
connection. It never begins, commits or rolls back the caller's outer
transaction; a SAVEPOINT protects its own failed insert. No daemon, remote
user authentication or gameplay authority is provided.

This module intentionally shares WorldClock's internal event validators and
canonical serializer. Its contract must be rerun against future clock changes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .world_clock import (
    DueEvent, WorldClock, _EVENT_ID, _KIND, _event, _payload, _stamp,
)


def schedule_in_transaction(
    clock: WorldClock, db: sqlite3.Connection, event_id: str, *,
    due_at: datetime, kind: str, payload: dict,
) -> DueEvent:
    """Enqueue a host-validated deadline inside the caller's write transaction.

    Intended for a trusted WorldClock.tick callback which receives db.
    This enqueues future work without spawning work or performing it immediately.
    Idempotent retries preserve previously stored events and their receipts.
    Caller must control world/actor authorization and own transaction rollback.
    """
    if not isinstance(clock, WorldClock):
        raise TypeError("Expected a WorldClock for this world")
    if not isinstance(db, sqlite3.Connection):
        raise TypeError("Expected an SQLite connection")
    if not db.in_transaction:
        raise RuntimeError("Existing SQLite transaction required")
    main_file = next(
        (row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"),
        None,
    )
    if not main_file or Path(main_file).resolve() != clock.db_path.resolve():
        raise ValueError("Connection points to a different world database")
    if db.row_factory is not sqlite3.Row:
        raise ValueError("Connection must use sqlite3.Row row_factory")
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("Shared scheduling requires enabled foreign keys")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clock_events'"
    ).fetchone() is None:
        raise RuntimeError("World clock must be explicitly enabled")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clock_state'"
    ).fetchone() is None:
        raise RuntimeError("World clock must be explicitly enabled")
    # Reject a manually forged/partial event table masquerading as enable().
    for table, expected in (
        ("clock_events", (
            ("event_id", "TEXT", 0, 1), ("due_utc", "TEXT", 1, 0),
            ("kind", "TEXT", 1, 0), ("payload_json", "TEXT", 1, 0),
            ("processed_utc", "TEXT", 0, 0),
        )),
        ("clock_state", (("id", "INTEGER", 0, 1), ("last_utc", "TEXT", 1, 0))),
    ):
        columns = tuple(
            (row["name"], row["type"].upper(), row["notnull"], row["pk"])
            for row in db.execute(f"PRAGMA table_info({table})")
        )
        if columns != expected:
            raise RuntimeError("World clock schema has not been explicitly enabled")
    bound = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_meta'"
    ).fetchone()
    if bound is None or db.execute(
        "SELECT 1 FROM world_meta WHERE key='world_id' AND length(value)>0"
    ).fetchone() is None:
        raise RuntimeError("World clock requires an initialized, bound world")

    if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
        raise ValueError("Invalid event_id")
    if not isinstance(kind, str) or _KIND.fullmatch(kind) is None:
        raise ValueError("Invalid event kind")
    due = _stamp(due_at)
    data = _payload(payload)
    existing = db.execute(
        "SELECT * FROM clock_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if existing is not None:
        if (existing["due_utc"], existing["kind"], existing["payload_json"]) != (
            due, kind, data
        ):
            raise ValueError("Event ID already used for a different event")
        return _event(existing)
    # A failed SQLite trigger can mutate unrelated rows *before* RAISE(FAIL).
    # Roll back this adapter's effects even if the trusted caller catches the
    # error and commits its outer transaction. The caller still owns that tx.
    db.execute("SAVEPOINT realm_clock_followup")
    try:
        db.execute(
            "INSERT INTO clock_events(event_id,due_utc,kind,payload_json) VALUES(?,?,?,?)",
            (event_id, due, kind, data),
        )
        result = DueEvent(event_id, datetime.fromisoformat(due),
                          kind, json.loads(data))
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT realm_clock_followup")
        db.execute("RELEASE SAVEPOINT realm_clock_followup")
        raise
    db.execute("RELEASE SAVEPOINT realm_clock_followup")
    return result
