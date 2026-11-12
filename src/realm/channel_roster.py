"""Optional, host-managed group chat membership; ship titles grant no access.

The channel roster is a *permission* boundary, independent of display-only
ship officer grants. Nothing is installed or admitted by importing, reading,
or running a player's command. A trusted world host explicitly installs
tables and grants/revokes a joined principal in that world's SQLite file.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .engine import _PLAYER_RE


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DISPLAY = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]{0,63}$")
_TABLES = frozenset(("chat_channels", "chat_memberships"))


def installed(db: sqlite3.Connection) -> bool:
    """Pure schema probe: no ordinary read/command creates permission tables."""
    names = {row[0] for row in db.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' "
        "AND name IN ('chat_channels','chat_memberships')"
    )}
    if not names:
        return False
    if names != _TABLES:
        raise RuntimeError("Partial host chat channel schema")
    return True


def member_for_actor(db: sqlite3.Connection, actor: str) -> sqlite3.Row | None:
    """Current server-issued membership under the caller's one DB snapshot."""
    if not installed(db):
        return None
    bound = db.execute(
        "SELECT value FROM main.world_meta WHERE key='world_id'"
    ).fetchone()
    if bound is None:
        return None
    return db.execute(
        "SELECT m.actor,m.group_id,m.role,m.joined_floor,"
        "g.display_name,g.world_id FROM main.chat_memberships AS m "
        "JOIN main.chat_channels AS g ON g.id=m.group_id "
        "WHERE m.actor=? COLLATE NOCASE AND m.world_id=? AND g.world_id=?",
        (actor, bound[0], bound[0]),
    ).fetchone()


def channel_room(group_id: str) -> str:
    """Reserved event routing key, never an authority source on its own."""
    return "@group:" + group_id


def channel_access_revision(db: sqlite3.Connection, actor: str) -> int:
    """Monotonic trusted-host mutation watermark for response-time privacy.

    Grant, role change, and revoke each append a private marker to the same
    AUTOINCREMENT event ledger, including when a membership row is deleted.
    Client speech cannot write markers; no channel schema is installed by read.
    """
    # The optional chat schema may not be installed on an ordinary world.
    # Such a world cannot have a host-issued channel epoch, and querying the
    # (potentially vast) world event ledger without the optional index on
    # every poll would otherwise force an avoidable full-table scan.
    if not installed(db):
        return 0
    return int(db.execute(
        "SELECT COALESCE(MAX(id),0) FROM main.events "
        "WHERE kind='chat_permission_epoch' AND actor=? COLLATE NOCASE",
        (actor,),
    ).fetchone()[0])


def visible_channel_events(
    db: sqlite3.Connection, actor: str, *, after: int, limit: int,
    latest: bool = False, include_group: bool = True,
) -> list[sqlite3.Row]:
    """Return only currently entitled cross-room events in one read snapshot.

    Global OOC starts at the actor's *first provable join*. A group member
    receives only messages sent after their current, host-granted membership
    epoch. Revocation removes the row, even for reading old channel history.
    """
    first = db.execute(
        "SELECT id FROM main.events WHERE actor=? COLLATE NOCASE "
        "AND kind='join' ORDER BY id LIMIT 1", (actor,)
    ).fetchone()
    if first is None:
        return []
    ordering = "DESC" if latest else "ASC"  # trusted internal switch, never SQL user text
    rows = list(db.execute(
        "SELECT id,occurred_at,actor,room_id,kind,payload_json FROM main.events "
        f"WHERE kind='ooc' AND id>? AND id>=? ORDER BY id {ordering} LIMIT ?",
        (after, first[0], limit),
    ))
    member = member_for_actor(db, actor) if include_group else None
    if member is not None:
        rows.extend(db.execute(
            "SELECT id,occurred_at,actor,room_id,kind,payload_json FROM main.events "
            "WHERE kind='crewchat' AND room_id=? AND id>? AND id>=? "
            f"ORDER BY id {ordering} LIMIT ?",
            (channel_room(member["group_id"]), after, member["joined_floor"], limit),
        ))
    chosen = sorted(rows, key=lambda row: row["id"], reverse=latest)[:limit]
    return sorted(chosen, key=lambda row: row["id"])


def event_text(actor: str, kind: str, payload: dict) -> str:
    """One inert, deterministic display string for HTTP feed and terminal."""
    if kind == "ooc":
        return f"[OOC Channel] {actor}: {payload['text']}"
    if kind == "crewchat":
        return (f"[{payload['group_display']}][{payload['role']}] "
                f"{actor}: \"{payload['text']}\"")
    raise ValueError("Not a chat channel event")


class ChannelRoster:
    """Explicit privileged host API, never exposed to player HTTP/Telnet."""

    def __init__(self, db_path: str | Path, *, world_id: str):
        if not isinstance(world_id, str) or _IDENTIFIER.fullmatch(world_id) is None:
            raise ValueError("Channel roster requires a valid world identifier")
        if not isinstance(db_path, (str, Path)):
            raise TypeError("Channel roster requires an existing world path")
        self.db_path = Path(db_path).resolve()
        self.world_id = world_id

    @contextmanager
    def _connect(self):
        if not self.db_path.is_file():
            raise FileNotFoundError("Host channel world file is absent")
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                self._check_world(db)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _check_world(self, db: sqlite3.Connection) -> None:
        bound = db.execute(
            "SELECT value FROM main.world_meta WHERE key='world_id'"
        ).fetchone()
        if bound is None or bound[0] != self.world_id:
            raise PermissionError("Channel roster world identity mismatch")

    @staticmethod
    def _require_installed(db: sqlite3.Connection) -> None:
        if not installed(db):
            raise RuntimeError("Trusted host must explicitly install group channels")

    @staticmethod
    def _advance_epoch(db: sqlite3.Connection, actor: str) -> int:
        """Host-only inert event; never delivered through any player channel."""
        row = db.execute(
            "INSERT INTO main.events(occurred_at,actor,room_id,kind,payload_json) "
            "VALUES(?,?,?,?,?) RETURNING id",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), actor,
             "@channel-permission", "chat_permission_epoch", "{}"),
        ).fetchone()
        if row is None:
            raise RuntimeError("Channel permission revision insert failed")
        return int(row[0])

    def install(self) -> None:
        """Transactional opt-in; never run from a read or player command."""
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS main.chat_channels("
                "id TEXT PRIMARY KEY COLLATE NOCASE,"
                "world_id TEXT NOT NULL,display_name TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS main.chat_memberships("
                "actor TEXT PRIMARY KEY COLLATE NOCASE REFERENCES players(id),"
                "group_id TEXT NOT NULL REFERENCES chat_channels(id),"
                "world_id TEXT NOT NULL,role TEXT NOT NULL,"
                "joined_floor INTEGER NOT NULL CHECK(joined_floor>=1))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS main.idx_chat_permission_epoch "
                "ON events(actor COLLATE NOCASE,id) WHERE kind='chat_permission_epoch'"
            )
            self._require_installed(db)

    def create_group(self, group_id: str, display_name: str) -> None:
        if not isinstance(group_id, str) or _IDENTIFIER.fullmatch(group_id) is None:
            raise ValueError("Invalid channel group identifier")
        if not isinstance(display_name, str) or _DISPLAY.fullmatch(display_name) is None:
            raise ValueError("Invalid channel display name")
        with self._connect() as db:
            self._require_installed(db)
            present = db.execute(
                "SELECT world_id FROM main.chat_channels WHERE id=?", (group_id,)
            ).fetchone()
            if present is not None:
                if present[0] != self.world_id:
                    raise PermissionError("Channel group world mismatch")
                db.execute(
                    "UPDATE main.chat_channels SET display_name=? WHERE id=?",
                    (display_name, group_id),
                )
            else:
                db.execute(
                    "INSERT INTO main.chat_channels(id,world_id,display_name) VALUES(?,?,?)",
                    (group_id, self.world_id, display_name),
                )

    def grant(self, actor: str, group_id: str, *, role: str) -> None:
        """One current group per player; changes of group require prior revoke."""
        if not isinstance(actor, str) or _PLAYER_RE.fullmatch(actor) is None:
            raise ValueError("Invalid channel member")
        if not isinstance(group_id, str) or _IDENTIFIER.fullmatch(group_id) is None:
            raise ValueError("Invalid channel group")
        if not isinstance(role, str) or _DISPLAY.fullmatch(role) is None:
            raise ValueError("Invalid host-issued group role")
        with self._connect() as db:
            self._require_installed(db)
            principal = db.execute(
                "SELECT id FROM main.players WHERE id=? COLLATE NOCASE", (actor,)
            ).fetchone()
            group = db.execute(
                "SELECT id,world_id FROM main.chat_channels WHERE id=?", (group_id,)
            ).fetchone()
            if principal is None or group is None or group["world_id"] != self.world_id:
                raise PermissionError("Joined member and active local group required")
            current = db.execute(
                "SELECT group_id,world_id FROM main.chat_memberships WHERE actor=? COLLATE NOCASE",
                (principal[0],),
            ).fetchone()
            if current is not None:
                if (current["group_id"].casefold() != group["id"].casefold()
                        or current["world_id"] != self.world_id):
                    raise PermissionError("Revoke old channel membership before changing group")
                db.execute(
                    "UPDATE main.chat_memberships SET role=? WHERE actor=?",
                    (role, principal[0]),
                )
                self._advance_epoch(db, principal[0])
            else:
                next_event = self._advance_epoch(db, principal[0]) + 1
                db.execute(
                    "INSERT INTO main.chat_memberships(actor,group_id,world_id,role,joined_floor) "
                    "VALUES(?,?,?,?,?)",
                    (principal[0], group["id"], self.world_id, role, next_event),
                )

    def revoke(self, actor: str) -> bool:
        if not isinstance(actor, str) or _PLAYER_RE.fullmatch(actor) is None:
            raise ValueError("Invalid channel member")
        with self._connect() as db:
            self._require_installed(db)
            removed = db.execute(
                "DELETE FROM main.chat_memberships WHERE actor=? COLLATE NOCASE "
                "AND world_id=?",
                (actor, self.world_id),
            )
            if removed.rowcount == 1:
                self._advance_epoch(db, actor)
            return removed.rowcount == 1
