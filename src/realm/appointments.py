"""Host-curated per-ship appointments and effective visible title labels.

This is a source-only role projection, not ship ownership proof, an endpoint,
or an authority to choose current ship context. The runtime must supply a
trusted ship ID from its verified ship ledger before rendering a title.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone

from .engine import Engine, _PLAYER_RE
from .profiles import PlayerProfileStore


_SHIP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
_ROLE_TITLE = {"captain": "Capt.", "first_officer": "First Officer"}
_RESERVED_PREFIX = re.compile(
    r"^(?:(?:capt(?:ain)?\.?|first[ -]+officer)(?: +|$)|capt(?:ain)?\.)"
)
# Common Unicode title lookalikes; this is *not* a complete confusables database.
_TITLE_LOOKALIKES = str.maketrans(
    {"с": "c", "ϲ": "c", "а": "a", "ɑ": "a", "р": "p", "т": "t",
     "і": "i", "ι": "i", "о": "o", "ο": "o", "е": "e", "ε": "e",
     "ѕ": "s", "ғ": "f", "һ": "h", "н": "h", "г": "r",
     "ᴀ": "a", "ᴄ": "c", "ᴘ": "p", "ᴛ": "t", "ɪ": "i",
     "ᴏ": "o", "ᴇ": "e", "ꜰ": "f", "ꜱ": "s", "ʜ": "h", "ʀ": "r", "ɴ": "n"}
)
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


def _rank_prefix_end(value: str) -> int | None:
    """Match title-shaped text through quotes, compatibility forms and lookalikes."""
    offset = 0
    while offset < len(value) and (
        unicodedata.category(value[offset]).startswith("P") or value[offset].isspace()
    ):
        offset += 1
    folded, ends = [], []
    for index, char in enumerate(value[offset:], start=offset):
        # Keep offsets into original text even if NFKD expands one character.
        for unit in unicodedata.normalize("NFKD", char).casefold():
            if not unicodedata.combining(unit):
                folded.append(unit.translate(_TITLE_LOOKALIKES))
                ends.append(index + 1)
    matched = _RESERVED_PREFIX.match("".join(folded))
    return ends[matched.end() - 1] if matched else None


def _clean_role_prefix(display_name: str, player_name: str) -> str:
    """Render title-like profile prefixes inert; rank must come from appointment."""
    while (end := _rank_prefix_end(display_name)) is not None:
        display_name = display_name[end:].strip().strip("'’“”")
    return display_name or player_name


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

    def label(self, player_name: str, *, trusted_ship_id: str | None = None) -> str | None:
        """Render one visible label only when a trusted ship context is supplied."""
        actor = _actor(player_name)
        display_name = self.profiles.get(actor)
        if display_name is None:
            return None
        base = _clean_role_prefix(display_name, actor)
        if trusted_ship_id is None:
            return base
        role = self.role(actor, trusted_ship_id)
        return f"{_ROLE_TITLE[role]} {base}" if role else base
