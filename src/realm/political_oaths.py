"""Optional, genre-neutral political allegiances in one authenticated REALM world.

The host alone installs and defines factions. Player commands may make or
break a *public* oath; this never creates crewchat privileges, rank, treasury,
land, or canon. One world file holds all state; no cross-world authority.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

DISABLED = "Political allegiances are not enabled."
_TABLES = frozenset({"political_factions", "political_oaths"})
_FACTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_WORLD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DISPLAY = re.compile(r"^[A-Za-z][A-Za-z0-9 .'-]{0,63}$")


def installed(db: sqlite3.Connection) -> bool:
    names = {row[0] for row in db.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' "
        "AND name IN ('political_factions','political_oaths')"
    )}
    if names and names != _TABLES:
        raise RuntimeError("Partial optional political schema; host repair required")
    return bool(names)


def _world(db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT value FROM main.world_meta WHERE key='world_id'"
    ).fetchone()
    if row is None:
        raise PermissionError("Political world has no trusted binding")
    return str(row[0])


def current_faction_id(db: sqlite3.Connection, actor: str) -> str | None:
    if not installed(db):
        return None
    world = _world(db)
    row = db.execute(
        "SELECT faction_id,world_id FROM main.political_oaths "
        "WHERE actor=? COLLATE NOCASE", (actor,)
    ).fetchone()
    if row is None:
        return None
    if row[1] != world:
        raise PermissionError("Political oath belongs to another world")
    return str(row[0])


def _display_status(db: sqlite3.Connection, subject: str, *, self_view: bool) -> str:
    world = _world(db)
    row = db.execute(
        "SELECT id FROM main.players WHERE id=? COLLATE NOCASE",
        (subject,),
    ).fetchone()
    if row is None:
        return "Unknown player."
    name = str(row[0])
    faction_id = current_faction_id(db, name)
    if faction_id is None:
        display = "unaffiliated"
    else:
        faction = db.execute(
            "SELECT display_name,world_id FROM main.political_factions "
            "WHERE id=? COLLATE NOCASE", (faction_id,),
        ).fetchone()
        if faction is None or faction[1] != world:
            raise RuntimeError("Political faction binding is incomplete")
        display = str(faction[0])
    breaks = db.execute(
        "SELECT COUNT(*) FROM main.events "
        "WHERE actor=? COLLATE NOCASE AND kind='political_break'",
        (name,),
    ).fetchone()[0]
    label = "Allegiance" if self_view else name
    return f"{label}: {display} (broken oaths: {breaks})."


class WorldPolitics:
    """Trusted host-only configuration: no player-command or HTTP endpoint."""

    def __init__(self, db_path: str | Path, *, world_id: str):
        if not isinstance(db_path, (str, Path)):
            raise TypeError("Expected a world database path")
        if not isinstance(world_id, str) or not _WORLD_ID.fullmatch(world_id):
            raise ValueError("Invalid world identifier for political host")
        self.db_path = Path(db_path).resolve()
        self.world_id = world_id

    def _connect(self):
        if not self.db_path.is_file():
            raise FileNotFoundError("World must exist before installing politics")
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE")
            if _world(db) != self.world_id:
                raise PermissionError("Political host world mismatch")
        except BaseException:
            db.rollback()
            db.close()
            raise
        return db

    def install(self) -> None:
        with closing(self._connect()) as db, db:
            if installed(db):
                return
            db.execute(
                "CREATE TABLE main.political_factions("
                "id TEXT PRIMARY KEY COLLATE NOCASE,"
                "world_id TEXT NOT NULL,display_name TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE main.political_oaths("
                "actor TEXT PRIMARY KEY COLLATE NOCASE REFERENCES players(id),"
                "faction_id TEXT NOT NULL REFERENCES political_factions(id),"
                "world_id TEXT NOT NULL)"
            )
            if not installed(db):
                raise RuntimeError("Political schema install incomplete")

    def create_faction(self, faction_id: str, display_name: str) -> None:
        if (not isinstance(faction_id, str)
                or not _FACTION_ID.fullmatch(faction_id)):
            raise ValueError("Invalid faction identifier")
        if (not isinstance(display_name, str)
                or not _DISPLAY.fullmatch(display_name)):
            raise ValueError("Invalid faction display name")
        with closing(self._connect()) as db, db:
            if not installed(db):
                raise RuntimeError("Trusted host must install politics first")
            previous = db.execute(
                "SELECT world_id,display_name FROM main.political_factions "
                "WHERE id=? COLLATE NOCASE", (faction_id,),
            ).fetchone()
            if previous is not None:
                if previous[0] != self.world_id:
                    raise PermissionError("Faction exists in a foreign world")
                if previous[1] != display_name:
                    raise ValueError("Faction identity is already defined")
                return
            db.execute(
                "INSERT INTO main.political_factions(id,world_id,display_name) "
                "VALUES(?,?,?)",
                (faction_id, self.world_id, display_name),
            )


def perform(db: sqlite3.Connection, actor: str, room_id: str,
            verb: str, arg: str, event_writer) -> str:
    if not installed(db):
        return DISABLED
    world = _world(db)
    if verb == "factions":
        if arg:
            return "Use 'factions' to inspect public political factions."
        factions = db.execute(
            "SELECT f.display_name,COUNT(o.actor) "
            "FROM main.political_factions AS f "
            "LEFT JOIN main.political_oaths AS o "
            "ON o.faction_id=f.id AND o.world_id=f.world_id "
            "WHERE f.world_id=? GROUP BY f.id ORDER BY f.id", (world,),
        ).fetchall()
        if not factions:
            return "No political factions have been established."
        return "Factions: " + ", ".join(
            f"{name} ({count} pledged)" for name, count in factions
        )
    if verb == "allegiance":
        return _display_status(db, arg or actor, self_view=not bool(arg))
    if verb == "pledge":
        if not arg or not _FACTION_ID.fullmatch(arg.casefold()):
            return "Unknown faction."
        faction = db.execute(
            "SELECT id,display_name,world_id FROM main.political_factions "
            "WHERE id=? COLLATE NOCASE", (arg,),
        ).fetchone()
        if faction is None or faction[2] != world:
            return "Unknown faction."
        current = current_faction_id(db, actor)
        if current is not None:
            if current.casefold() == str(faction[0]).casefold():
                return f"Already pledged to {faction[1]}."
            return "Renounce your current oath before pledging another faction."
        db.execute(
            "INSERT INTO main.political_oaths(actor,faction_id,world_id) "
            "VALUES(?,?,?)", (actor, faction[0], world),
        )
        event_writer(db, actor, room_id, "political_pledge",
                     {"faction_id": faction[0], "faction": faction[1]})
        return f"{actor} pledged allegiance to {faction[1]}."
    if verb == "renounce":
        if arg:
            return "Use 'renounce' to break your current oath."
        current = current_faction_id(db, actor)
        if current is None:
            return "Already unaffiliated."
        faction = db.execute(
            "SELECT display_name,world_id FROM main.political_factions "
            "WHERE id=? COLLATE NOCASE", (current,),
        ).fetchone()
        if faction is None or faction[1] != world:
            raise RuntimeError("Cannot renounce an inconsistent political oath")
        db.execute(
            "DELETE FROM main.political_oaths WHERE actor=? COLLATE NOCASE "
            "AND faction_id=? AND world_id=?", (actor, current, world),
        )
        event_writer(db, actor, room_id, "political_break",
                     {"faction_id": current, "faction": faction[0]})
        return f"{actor} broke allegiance to {faction[0]}."
    raise ValueError("Unsupported political command")
