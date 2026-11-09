"""Host-curated per-ship appointments and structured identity presentation.

Freeform profile names are never authoritative rank evidence. Only the
appointment table supplies role/title, and a future UI must render that trusted
role as a separate badge using verified ship context. This source is not an
endpoint or proof of ship ownership.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from .engine import Engine, _PLAYER_RE
from .profiles import PlayerProfileStore


_SHIP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
_ROLE_TITLE = {"captain": "Capt.", "first_officer": "First Officer"}
_APPOINTMENT_SQL = """
CREATE TABLE IF NOT EXISTS ship_appointments (
    ship_id TEXT NOT NULL COLLATE NOCASE,
    player_name TEXT NOT NULL COLLATE NOCASE REFERENCES players(id),
    role TEXT NOT NULL CHECK(role IN ('captain','first_officer')),
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY(ship_id, player_name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ship_appointment_unique_active_role
    ON ship_appointments(ship_id, role) WHERE revoked_at IS NULL;
"""


def _actor(name: str) -> str:
    if not isinstance(name, str) or not _PLAYER_RE.fullmatch(name):
        raise ValueError("Invalid player principal")
    return name


def _ship(ship_id: str) -> str:
    if not isinstance(ship_id, str) or not _SHIP_RE.fullmatch(ship_id):
        raise ValueError("Invalid trusted ship ID")
    return ship_id.lower()


@dataclass(frozen=True, slots=True)
class CharacterIdentity:
    """Presentation data: display_name is untrusted for rank semantics.

    The role and title are separately read from active appointments, never
    parsed, cleaned, or inferred from display_name.
    """

    player_id: str
    display_name: str
    role: str | None
    title: str | None


class ShipAppointmentStore:
    """World-bound assignments; grant/revoke requires a trusted host caller."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.profiles = PlayerProfileStore(engine)
        with closing(engine._connect()) as db, db:
            db.executescript(_APPOINTMENT_SQL)

    def grant(self, player_name: str, ship_id: str, role: str) -> str:
        """Grant a joined player one role on one ship; never a public route."""
        actor, ship = _actor(player_name), _ship(ship_id)
        if not isinstance(role, str) or role not in _ROLE_TITLE:
            raise ValueError("Unsupported appointment role")
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            player = db.execute("SELECT id FROM players WHERE id=?", (actor,)).fetchone()
            if player is None:
                raise ValueError("Player must join this world before appointment")
            existing = db.execute(
                "SELECT role FROM ship_appointments "
                "WHERE ship_id=? AND player_name=? AND revoked_at IS NULL",
                (ship, player["id"]),
            ).fetchone()
            if existing is not None:
                if existing["role"] == role:
                    return role
                raise ValueError("Revoke existing position before appointing a new one")
            occupied = db.execute(
                "SELECT 1 FROM ship_appointments "
                "WHERE ship_id=? AND role=? AND revoked_at IS NULL",
                (ship, role),
            ).fetchone()
            if occupied:
                raise ValueError("Ship position already assigned; revoke first")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                db.execute(
                    "INSERT INTO ship_appointments (ship_id,player_name,role,granted_at,revoked_at) "
                    "VALUES (?,?,?,?,NULL) "
                    "ON CONFLICT(ship_id,player_name) DO UPDATE SET "
                    "role=excluded.role,granted_at=excluded.granted_at,revoked_at=NULL",
                    (ship, player["id"], role, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Conflicting ship appointment") from exc
        return role

    def revoke(self, player_name: str, ship_id: str) -> bool:
        """Revoke this player's active assignment, preserving the inactive row."""
        actor, ship = _actor(player_name), _ship(ship_id)
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE ship_appointments SET revoked_at=? "
                "WHERE ship_id=? AND player_name=? AND revoked_at IS NULL",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), ship, actor),
            )
            return changed.rowcount == 1

    def role(self, player_name: str, ship_id: str) -> str | None:
        """Return an active role scoped strictly to the supplied ship."""
        actor, ship = _actor(player_name), _ship(ship_id)
        with closing(self.engine._connect()) as db:
            row = db.execute(
                "SELECT role FROM ship_appointments "
                "WHERE player_name=? AND ship_id=? AND revoked_at IS NULL",
                (actor, ship),
            ).fetchone()
        return str(row["role"]) if row else None

    def identity(
        self, player_name: str, *, trusted_ship_id: str | None = None
    ) -> CharacterIdentity | None:
        """Return distinct principal, *freeform* name, and trusted scoped rank.

        Callers must never infer titles from display_name, including strings
        styled as 'Captain' or Unicode lookalikes. Only role/title are rank
        authority. A client-supplied ship ID is NOT trusted context.
        """
        actor = _actor(player_name)
        ship = _ship(trusted_ship_id) if trusted_ship_id is not None else None
        with closing(self.engine._connect()) as db:
            db.execute("BEGIN")
            player = db.execute(
                "SELECT p.id, COALESCE(r.display_name,p.id) AS display_name "
                "FROM players AS p LEFT JOIN player_profiles AS r ON r.player_name=p.id "
                "WHERE p.id=?", (actor,),
            ).fetchone()
            if player is None:
                return None
            rank = db.execute(
                "SELECT role FROM ship_appointments "
                "WHERE player_name=? AND ship_id=? AND revoked_at IS NULL",
                (player["id"], ship),
            ).fetchone() if ship is not None else None
        role = str(rank["role"]) if rank else None
        return CharacterIdentity(
            player_id=str(player["id"]),
            display_name=str(player["display_name"]),
            role=role,
            title=_ROLE_TITLE[role] if role else None,
        )
