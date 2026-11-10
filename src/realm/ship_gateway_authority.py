"""Sensitive-world and UTC-queue postconditions for scoped ship POST writers.

This is an in-transaction check against unintended host-side SQLite DML; it
does not make a privileged database owner or a later independent writer safe.
Only one exact pending ship arrival is authorized by the gateway departure.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

_CORE_TABLES = (
    "world_meta", "clock_state", "npc_roster_utc_attestations",
    "player_credentials", "players",
)
_SCOPED_TABLES = ("scoped_auth_guard", "scoped_player_credentials")


def _inventory(db: sqlite3.Connection, table: str) -> Counter:
    # Compare whole rows, preserving duplicates if schema changes; explicit
    # `main` protects against TEMP shadow tables as alternate authority.
    return Counter(tuple(row) for row in db.execute(f"SELECT * FROM main.{table}"))


def _clock_events(db: sqlite3.Connection) -> dict[str, tuple]:
    return {
        row["event_id"]: tuple(row)
        for row in db.execute(
            "SELECT event_id,due_utc,kind,payload_json,processed_utc "
            "FROM main.clock_events"
        )
    }


class GatewayShipAuthorityCustody:
    """Snapshot world/clock/credential authority before authenticated DML."""

    def __init__(self, db: sqlite3.Connection, *, scoped: bool):
        expected = set(_CORE_TABLES) | {"clock_events"}
        inspect = expected | set(_SCOPED_TABLES)
        present = {
            row["name"] for row in db.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' "
                "AND name IN (" + ",".join("?" for _ in inspect) + ")",
                tuple(inspect),
            )
        }
        optional = present & set(_SCOPED_TABLES)
        # Legacy host-only ship credentials exist without scoped tables.
        # If a scoped principal is requested, BOTH scoped authority tables are
        # required. If either table exists, capture both; partial is unsafe.
        if not {"clock_events", "clock_state", "npc_roster_utc_attestations"}.issubset(present):
            # Preserve the gateway's documented 409 when the opt-in world
            # clock is disabled, instead of misreporting a malformed POST.
            raise RuntimeError("Ship world clock is not enabled")
        if (not expected.issubset(present)
                or (scoped and optional != set(_SCOPED_TABLES))
                or (optional and optional != set(_SCOPED_TABLES))):
            raise ValueError("Ship gateway writer requires complete world authority tables")
        tables = _CORE_TABLES + (_SCOPED_TABLES if optional else ())
        self.authority = {table: _inventory(db, table) for table in tables}
        self.clock_events = _clock_events(db)

    def verify(self, db: sqlite3.Connection, *, departure: dict,
               replay: bool) -> None:
        """Reject all non-delegated authority/queue deltas before POST commit."""
        if any(_inventory(db, table) != prior for table, prior in self.authority.items()):
            raise ValueError("Ship gateway world, clock, or credential authority changed")

        expected = dict(self.clock_events)
        if not replay:
            event_id = departure["event_id"]
            if event_id in expected:
                raise ValueError("Ship gateway arrival queue reused preexisting event")
            try:
                arrival = departure["departure"]["arrival_at"]
                due = datetime.fromtimestamp(arrival, tz=timezone.utc).isoformat(
                    timespec="microseconds")
                ship_id = departure["departure"]["ship"]
            except (KeyError, TypeError, OverflowError, ValueError) as exc:
                raise ValueError("Ship gateway departure lacks bound UTC arrival") from exc
            expected[event_id] = (
                event_id, due, "ship_arrival",
                json.dumps({"ship_id": ship_id}, sort_keys=True,
                           separators=(",", ":")), None,
            )
        if _clock_events(db) != expected:
            raise ValueError("Ship gateway UTC queue changed beyond authorized arrival")
