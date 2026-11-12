"""Host-only, world-bound reservation of AI resident principal names.

This is an opt-in local host operation, not a model permission, an HTTP API,
or a public account registration system. Already-issued player credentials
must be denied by the credential stores after a name is reserved.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from .engine import Engine, _PLAYER_RE

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS main.reserved_npc_principals ("
    "player_name TEXT PRIMARY KEY COLLATE NOCASE,"
    "world_id TEXT NOT NULL,"
    "reserved_utc TEXT NOT NULL)"
)


def is_reserved_npc(db: sqlite3.Connection, actor: str) -> bool:
    """Read only trusted-main role state in the caller's SQLite transaction."""
    if not isinstance(actor, str):
        return False
    if db.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='reserved_npc_principals'"
    ).fetchone() is None:
        return False
    return db.execute(
        "SELECT 1 FROM main.reserved_npc_principals "
        "WHERE player_name=? COLLATE NOCASE", (actor,),
    ).fetchone() is not None


def reserve_npc(engine: Engine, actor: str) -> bool:
    """Explicit host action; cannot be called from a client/gameplay route.

    Return True on first reservation, False if already reserved. Require a
    bound world and an existing canonical actor. Never create a game player.
    """
    if not isinstance(engine, Engine):
        raise TypeError("NPC reservation requires a REALM Engine")
    if not isinstance(actor, str) or _PLAYER_RE.fullmatch(actor) is None:
        raise ValueError("Invalid NPC principal name")
    with closing(engine._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        world = db.execute(
            "SELECT value FROM main.world_meta WHERE key='world_id'"
        ).fetchone()
        if world is None or not world[0]:
            raise ValueError("NPC reservation requires a bound world")
        player = db.execute(
            "SELECT id FROM main.players WHERE id=? COLLATE NOCASE",
            (actor,),
        ).fetchone()
        if player is None:
            raise ValueError("NPC reservation requires a previously joined actor")
        db.execute(_SCHEMA)
        if is_reserved_npc(db, player["id"]):
            return False
        db.execute(
            "INSERT INTO main.reserved_npc_principals "
            "(player_name,world_id,reserved_utc) VALUES (?,?,?)",
            (player["id"], world[0], datetime.now(timezone.utc).isoformat()),
        )
        if not is_reserved_npc(db, player["id"]):
            raise RuntimeError("NPC principal reservation was not recorded")
    return True
