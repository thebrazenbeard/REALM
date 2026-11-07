"""Opt-in, host-driven REALM deadline queue using UTC and one world SQLite DB.

The host must call enable() explicitly *after* initial world import. No thread,
timer, daemon, player route or public action is created here. Each tick calls a
trusted host handler in the SAME SQLite transaction as its completion receipt.
Only SQLite effects made through that connection are atomic with the receipt.
External messages, model output, RPC and network effects are NOT exactly once;
use a transactional outbox plus idempotent external consumer for those.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_EVENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_KIND = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_PAYLOAD_BYTES = 4096
_MAX_TICK_BATCH = 1000
_MAX_FORWARD_SECONDS = 86400
_UTC = timezone.utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clock_events (
    event_id TEXT PRIMARY KEY,
    due_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    processed_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_clock_due
    ON clock_events(due_utc, event_id) WHERE processed_utc IS NULL;
CREATE TABLE IF NOT EXISTS clock_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    last_utc TEXT NOT NULL
);
"""


class ClockSkewError(ValueError):
    """UTC wall clock moved backward or unusually far forward."""


@dataclass(frozen=True)
class DueEvent:
    event_id: str
    due_at: datetime
    kind: str
    payload: dict
    processed_at: datetime | None = None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("A timezone-aware UTC datetime is required")
    return value.astimezone(_UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _payload(value: dict) -> str:
    if type(value) is not dict:
        raise ValueError("Event payload must be a JSON object")
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Event payload must be finite valid JSON") from exc
    try:
        encoded = canonical.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Event payload contains invalid Unicode") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Event payload exceeds 4096 UTF-8 bytes")
    return canonical


def _event(row: sqlite3.Row) -> DueEvent:
    return DueEvent(
        event_id=row["event_id"], due_at=datetime.fromisoformat(row["due_utc"]),
        kind=row["kind"], payload=json.loads(row["payload_json"]),
        processed_at=(datetime.fromisoformat(row["processed_utc"])
                      if row["processed_utc"] is not None else None),
    )


class WorldClock:
    """One per-world database; host-only, disabled until explicit enable().

    The host must perform worldpack import before enable(). Rollback of the
    *entire SQLite file* can roll back queue state; no external witness exists.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def enable(self) -> None:
        """Opt in only after an initialized world is bound/imported."""
        if not self.db_path.is_file():
            raise RuntimeError("Clock requires an initialized, bound world before enable")
        with closing(sqlite3.connect(str(self.db_path), timeout=10)) as db, db:
            db.execute("PRAGMA busy_timeout=10000")
            has_meta = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_meta'"
            ).fetchone()
            if not has_meta or not db.execute(
                "SELECT 1 FROM world_meta WHERE key='world_id' AND length(value)>0"
            ).fetchone():
                raise RuntimeError("Clock requires an initialized, bound world before enable")
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        if not self.db_path.is_file():
            raise RuntimeError("World clock must be explicitly enabled")
        db = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clock_events'"
            ).fetchone()
            if exists is None:
                raise RuntimeError("World clock must be explicitly enabled")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
        finally:
            db.close()

    def schedule(self, event_id: str, *, due_at: datetime,
                 kind: str, payload: dict) -> DueEvent:
        """Idempotently enqueue a host-validated event, or reject key reuse."""
        if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
            raise ValueError("Invalid event_id")
        if not isinstance(kind, str) or _KIND.fullmatch(kind) is None:
            raise ValueError("Invalid event kind")
        due = _stamp(due_at)
        data = _payload(payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM clock_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if (existing["due_utc"], existing["kind"], existing["payload_json"]) != (due, kind, data):
                    raise ValueError("Event ID already used for a different event")
                return _event(existing)
            db.execute(
                "INSERT INTO clock_events(event_id,due_utc,kind,payload_json) VALUES(?,?,?,?)",
                (event_id, due, kind, data),
            )
            return DueEvent(event_id, datetime.fromisoformat(due), kind, json.loads(data))

    def lookup(self, event_id: str) -> DueEvent | None:
        """Read a queued/completed event without modifying world state."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM clock_events WHERE event_id=?", (event_id,)
            ).fetchone()
            return _event(row) if row else None

    def tick(self, now_utc: datetime, handler: Callable[[sqlite3.Connection, DueEvent], None],
             *, limit: int = 100, acknowledge_forward_jump: bool = False) -> list[DueEvent]:
        """Apply at most limit due effects AND receipts in one SQLite transaction.

        Handler is host-trusted, must use the supplied db and MUST NOT commit,
        roll back, close, attach unrelated databases, or emit external effects.
        Exceptions abort the whole batch. Handler's work is not authorization.
        """
        now = _utc(now_utc)
        if type(acknowledge_forward_jump) is not bool:
            raise ValueError("Forward-jump acknowledgment must be a boolean")
        if type(limit) is not int or not 1 <= limit <= _MAX_TICK_BATCH:
            raise ValueError("Tick limit must be an integer from 1 to 1000")
        if not callable(handler):
            raise ValueError("Tick handler must be callable")
        stamp = _stamp(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT last_utc FROM main.clock_state WHERE id=1").fetchone()
            if state is not None:
                prior = datetime.fromisoformat(state["last_utc"])
                elapsed = (now - prior).total_seconds()
                if elapsed < 0:
                    raise ClockSkewError("UTC wall clock moved backward")
                if elapsed > _MAX_FORWARD_SECONDS and not acknowledge_forward_jump:
                    raise ClockSkewError("UTC advanced more than 24 hours; host acknowledgment required")
            # Snapshot every pre-existing scheduler row in Python memory.
            # A callback's TEMP trigger could otherwise pre-complete, delete,
            # or rewrite an event outside this tick's selected batch.
            before_events = {
                row["event_id"]: (
                    row["due_utc"], row["kind"], row["payload_json"], row["processed_utc"]
                )
                for row in db.execute(
                    "SELECT event_id,due_utc,kind,payload_json,processed_utc "
                    "FROM main.clock_events"
                )
            }
            rows = db.execute(
                "SELECT * FROM main.clock_events WHERE processed_utc IS NULL AND due_utc <= ? "
                "ORDER BY due_utc, event_id LIMIT ?", (stamp, limit),
            ).fetchall()
            processed = []
            for row in rows:
                event = _event(row)
                handler(db, event)
                updated = db.execute(
                    "UPDATE main.clock_events SET processed_utc=? "
                    "WHERE event_id=? AND processed_utc IS NULL",
                    (stamp, event.event_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("A clock receipt changed during host transaction")
                processed.append(event)
            db.execute(
                "INSERT INTO main.clock_state(id,last_utc) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET last_utc=excluded.last_utc",
                (stamp,),
            )
            # Handler-created TEMP triggers can alter the durable receipts or
            # clock watermark after UPDATE rowcount checks succeeded. Verify
            # the final main-schema state, including earlier batch members,
            # while still inside the same rollback-capable transaction.
            state_after = db.execute(
                "SELECT last_utc FROM main.clock_state WHERE id=1"
            ).fetchone()
            if state_after is None or state_after["last_utc"] != stamp:
                raise RuntimeError("Clock state postcondition failed during host transaction")
            expected_receipts = {event.event_id: stamp for event in processed}
            unseen = set(before_events)
            for row in db.execute(
                "SELECT event_id,due_utc,kind,payload_json,processed_utc "
                "FROM main.clock_events"
            ):
                event_id = row["event_id"]
                if event_id in before_events:
                    unseen.remove(event_id)
                    before = before_events[event_id]
                    expected = (
                        before[0], before[1], before[2],
                        expected_receipts.get(event_id, before[3]),
                    )
                    actual = (
                        row["due_utc"], row["kind"], row["payload_json"],
                        row["processed_utc"],
                    )
                    if actual != expected:
                        raise RuntimeError("Clock event receipt/metadata postcondition failed during host transaction")
                elif row["processed_utc"] is not None:
                    # A trusted host can enqueue a new pending future event
                    # in its transaction, but cannot forge its completion.
                    raise RuntimeError("Clock new event postcondition failed during host transaction")
            if unseen:
                raise RuntimeError("Clock event deletion postcondition failed during host transaction")
            return processed
