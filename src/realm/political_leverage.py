"""Opt-in, genre-neutral contested political sites backed by real item custody.

Every score reflects an item explicitly approved by the host, actually
carried by a pledged actor and retired from inventory in one action transaction.
An item can contribute to the ledger only once, even if a trusted host later
recreates an item with the same ID. This does not grant chat/NPC/ship privileges.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .political_oaths import (
    _DISPLAY, _FACTION_ID, _WORLD_ID, _world, current_faction_id,
    installed as oaths_installed,
)

DISABLED = "Political sites are not enabled."
_SITE_TABLES = frozenset({
    "political_sites", "political_resources", "political_offerings",
})
_OFFER = re.compile(r"^([A-Za-z][A-Za-z0-9_-]{0,63})\s+to\s+([a-z][a-z0-9_]{0,39})$", re.I)
_ITEM_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def installed(db: sqlite3.Connection) -> bool:
    names = {row[0] for row in db.execute(
        "SELECT name FROM main.sqlite_master WHERE type='table' "
        "AND name IN ('political_sites','political_resources','political_offerings')"
    )}
    if names and names != _SITE_TABLES:
        raise RuntimeError("Partial political site schema; host repair required")
    if names and not oaths_installed(db):
        raise RuntimeError("Political site schema without political factions")
    return bool(names)


def _site(db: sqlite3.Connection, site_id: str, world: str):
    row = db.execute(
        "SELECT id,display_name,room_id,world_id FROM main.political_sites "
        "WHERE id=? COLLATE NOCASE", (site_id,),
    ).fetchone()
    if row is None:
        return None
    if row["world_id"] != world:
        raise PermissionError("Political site world mismatch")
    return row


def _control(db: sqlite3.Connection, site, world: str) -> str:
    scores = db.execute(
        "SELECT f.id,f.display_name,SUM(o.points) AS points "
        "FROM main.political_offerings o "
        "JOIN main.political_factions f ON f.id=o.faction_id "
        "AND f.world_id=o.world_id "
        "WHERE o.site_id=? AND o.world_id=? "
        "GROUP BY f.id,f.display_name ORDER BY points DESC,f.id",
        (site["id"], world),
    ).fetchall()
    if not scores:
        verdict = "Unclaimed"
        standings = "none"
    else:
        max_points = int(scores[0]["points"])
        leaders = [row["display_name"] for row in scores
                   if int(row["points"]) == max_points]
        verdict = ("Controlled by " + leaders[0] if len(leaders) == 1
                   else "Contested between " + ", ".join(leaders))
        standings = ", ".join(
            f"{row['display_name']}={int(row['points'])}" for row in scores
        )
    return f"{site['display_name']}: {verdict}. Support: {standings}."


class WorldLeverage:
    """Trusted host-only setup of site geography and scarce eligible items."""

    def __init__(self, db_path: str | Path, *, world_id: str):
        if not isinstance(db_path, (str, Path)):
            raise TypeError("Expected an existing world database")
        if not isinstance(world_id, str) or not _WORLD_ID.fullmatch(world_id):
            raise ValueError("Invalid world identifier")
        self.db_path = Path(db_path).resolve()
        self.world_id = world_id

    def _connect(self):
        if not self.db_path.is_file():
            raise FileNotFoundError("World must already exist")
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE")
            if _world(db) != self.world_id:
                raise PermissionError("Political leverage world mismatch")
        except BaseException:
            db.rollback()
            db.close()
            raise
        return db

    def install(self) -> None:
        with closing(self._connect()) as db, db:
            if not oaths_installed(db):
                raise RuntimeError("Host must enable political oaths first")
            if installed(db):
                return
            db.execute(
                "CREATE TABLE main.political_sites("
                "id TEXT PRIMARY KEY COLLATE NOCASE,"
                "world_id TEXT NOT NULL,"
                "display_name TEXT NOT NULL,"
                "room_id TEXT NOT NULL REFERENCES rooms(id))"
            )
            db.execute(
                "CREATE TABLE main.political_resources("
                "item_id TEXT PRIMARY KEY COLLATE NOCASE,"
                "world_id TEXT NOT NULL,"
                "points INTEGER NOT NULL CHECK(points BETWEEN 1 AND 5))"
            )
            db.execute(
                "CREATE TABLE main.political_offerings("
                "id INTEGER PRIMARY KEY,"
                "world_id TEXT NOT NULL,"
                "actor TEXT NOT NULL REFERENCES players(id),"
                "faction_id TEXT NOT NULL REFERENCES political_factions(id),"
                "site_id TEXT NOT NULL REFERENCES political_sites(id),"
                "item_id TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                "item_name TEXT NOT NULL,"
                "item_description TEXT NOT NULL,"
                "points INTEGER NOT NULL CHECK(points BETWEEN 1 AND 5))"
            )
            if not installed(db):
                raise RuntimeError("Political site installation incomplete")

    def define_site(self, site_id: str, display_name: str, *,
                    room_id: str) -> None:
        if not isinstance(site_id, str) or not _FACTION_ID.fullmatch(site_id):
            raise ValueError("Invalid political site ID")
        if not isinstance(display_name, str) or not _DISPLAY.fullmatch(display_name):
            raise ValueError("Invalid political site name")
        if not isinstance(room_id, str) or not _ITEM_ID.fullmatch(room_id):
            raise ValueError("Invalid room ID")
        with closing(self._connect()) as db, db:
            if not installed(db):
                raise RuntimeError("Host must install political sites first")
            existing_room = db.execute(
                "SELECT id FROM main.rooms WHERE id=?", (room_id,),
            ).fetchone()
            if existing_room is None:
                raise ValueError("Unknown existing room")
            old = _site(db, site_id, self.world_id)
            if old is not None:
                if old["display_name"] != display_name or old["room_id"] != room_id:
                    raise ValueError("Political site identity cannot be relabeled")
                return
            db.execute(
                "INSERT INTO main.political_sites(id,world_id,display_name,room_id) "
                "VALUES(?,?,?,?)",
                (site_id, self.world_id, display_name, room_id),
            )

    def authorize_resource(self, item_id: str, *, points: int = 1) -> None:
        if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
            raise ValueError("Invalid item ID")
        if type(points) is not int or not 1 <= points <= 5:
            raise ValueError("Political item influence must be an integer 1–5")
        with closing(self._connect()) as db, db:
            if not installed(db):
                raise RuntimeError("Host must install political sites first")
            if db.execute(
                "SELECT 1 FROM main.political_offerings WHERE item_id=? COLLATE NOCASE",
                (item_id,),
            ).fetchone():
                raise ValueError("That item has already been offered")
            if db.execute(
                "SELECT 1 FROM main.items WHERE id=? COLLATE NOCASE",
                (item_id,),
            ).fetchone() is None:
                raise ValueError("Host may only designate existing world items")
            old = db.execute(
                "SELECT points,world_id FROM main.political_resources "
                "WHERE item_id=? COLLATE NOCASE", (item_id,),
            ).fetchone()
            if old is not None:
                if old["world_id"] != self.world_id or old["points"] != points:
                    raise ValueError("Host cannot silently change a resource's value")
                return
            db.execute(
                "INSERT INTO main.political_resources(item_id,world_id,points) "
                "VALUES(?,?,?)", (item_id, self.world_id, points),
            )


    def configure_batch(self, *, sites=(), resources=()) -> None:
        """One host transaction: all valid site/item declarations or none.

        Schema installation remains a separate explicit host opt-in action.
        """
        sites = tuple(sites)
        resources = tuple(resources)
        with closing(self._connect()) as db, db:
            if not installed(db):
                raise RuntimeError("Host must install political sites first")
            seen_sites = set()
            seen_items = set()
            for site_id, display_name, room_id in sites:
                if (not isinstance(site_id, str) or
                        _FACTION_ID.fullmatch(site_id) is None or
                        not isinstance(display_name, str) or
                        _DISPLAY.fullmatch(display_name) is None or
                        not isinstance(room_id, str) or
                        _ITEM_ID.fullmatch(room_id) is None):
                    raise ValueError("Invalid host site declaration")
                if site_id.casefold() in seen_sites:
                    raise ValueError("Duplicate political site declaration")
                seen_sites.add(site_id.casefold())
                if db.execute(
                    "SELECT 1 FROM main.rooms WHERE id=?", (room_id,),
                ).fetchone() is None:
                    raise ValueError("Political site must name an existing room")
                prior = _site(db, site_id, self.world_id)
                if prior is not None:
                    if prior["room_id"] != room_id or prior["display_name"] != display_name:
                        raise ValueError("Existing political site cannot be relabeled")
                else:
                    db.execute(
                        "INSERT INTO main.political_sites(id,world_id,display_name,room_id) "
                        "VALUES(?,?,?,?)",
                        (site_id, self.world_id, display_name, room_id),
                    )
            for item_id, points in resources:
                if (not isinstance(item_id, str) or
                        _ITEM_ID.fullmatch(item_id) is None or
                        type(points) is not int or not 1 <= points <= 5):
                    raise ValueError("Invalid political resource declaration")
                if item_id.casefold() in seen_items:
                    raise ValueError("Duplicate political resource declaration")
                seen_items.add(item_id.casefold())
                if db.execute(
                    "SELECT 1 FROM main.political_offerings WHERE item_id=? COLLATE NOCASE",
                    (item_id,),
                ).fetchone():
                    raise ValueError("Resource was already spent")
                if db.execute(
                    "SELECT 1 FROM main.items WHERE id=? COLLATE NOCASE",
                    (item_id,),
                ).fetchone() is None:
                    raise ValueError("Host resource must exist in the world")
                old = db.execute(
                    "SELECT points,world_id FROM main.political_resources "
                    "WHERE item_id=? COLLATE NOCASE", (item_id,),
                ).fetchone()
                if old is not None:
                    if old["points"] != points or old["world_id"] != self.world_id:
                        raise ValueError("Cannot silently reprice a political resource")
                else:
                    db.execute(
                        "INSERT INTO main.political_resources(item_id,world_id,points) "
                        "VALUES(?,?,?)",
                        (item_id, self.world_id, points),
                    )


def perform(db: sqlite3.Connection, actor: str, room_id: str,
            verb: str, arg: str, event_writer) -> str:
    """Called exclusively inside Engine.act's actor-bound writer transaction."""
    if not installed(db):
        return DISABLED
    world = _world(db)
    if verb == "sites":
        if arg:
            return "Use 'sites' to list public political objectives."
        rows = db.execute(
            "SELECT display_name,id FROM main.political_sites "
            "WHERE world_id=? ORDER BY id", (world,),
        ).fetchall()
        return ("Sites: " + ", ".join(f"{r['display_name']} ({r['id']})" for r in rows)
                if rows else "No political sites have been configured.")
    if verb == "control":
        if not arg or not _FACTION_ID.fullmatch(arg.casefold()):
            return "Specify a political site ID."
        site = _site(db, arg, world)
        return _control(db, site, world) if site else "Unknown political site."
    if verb != "offer":
        raise ValueError("Unsupported political site command")
    matched = _OFFER.fullmatch(arg.strip())
    if matched is None:
        return "Use 'offer <item_id> to <site_id>' at the contested site."
    item_id, site_id = matched.groups()
    site = _site(db, site_id, world)
    if site is None:
        return "Unknown political site."
    if site["room_id"] != room_id:
        return "You must stand at the political site to contribute."
    faction_id = current_faction_id(db, actor)
    if faction_id is None:
        return "You must pledge to a faction before contributing."
    item = db.execute(
        "SELECT id,name,description FROM main.items "
        "WHERE id=? COLLATE NOCASE AND held_by=? COLLATE NOCASE "
        "AND room_id IS NULL",
        (item_id, actor),
    ).fetchone()
    if item is None:
        return "You are not carrying that exact item."
    offered_before = db.execute(
        "SELECT 1 FROM main.political_offerings WHERE item_id=? COLLATE NOCASE",
        (item["id"],),
    ).fetchone()
    if offered_before:
        return "This resource was already contributed."
    approved = db.execute(
        "SELECT points,world_id FROM main.political_resources "
        "WHERE item_id=? COLLATE NOCASE", (item["id"],),
    ).fetchone()
    if approved is None or approved["world_id"] != world:
        return "The host has not designated that item for political influence."
    faction = db.execute(
        "SELECT display_name,world_id FROM main.political_factions "
        "WHERE id=? COLLATE NOCASE", (faction_id,),
    ).fetchone()
    if faction is None or faction["world_id"] != world:
        raise RuntimeError("Political contributor has no valid faction")
    removed = db.execute(
        "DELETE FROM main.items WHERE id=? AND held_by=? COLLATE NOCASE",
        (item["id"], actor),
    )
    if removed.rowcount != 1:
        raise RuntimeError("Political resource custody changed before contribution")
    db.execute(
        "INSERT INTO main.political_offerings("
        "world_id,actor,faction_id,site_id,item_id,item_name,item_description,points"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (world, actor, faction_id, site["id"], item["id"], item["name"],
         item["description"], approved["points"]),
    )
    payload = {
        "site_id": site["id"], "site": site["display_name"],
        "faction_id": faction_id, "faction": faction["display_name"],
        "item_id": item["id"], "item": item["name"],
        "points": approved["points"],
    }
    emissions = []
    event_writer(db, actor, room_id, "political_offer", payload,
                 emissions=emissions)
    if len(emissions) != 1:
        raise RuntimeError("Political contribution has no emitted event receipt")
    event_id, timestamp = emissions[0]
    committed = db.execute(
        "SELECT actor,room_id,kind,payload_json,occurred_at "
        "FROM main.events WHERE id=?", (event_id,),
    ).fetchone()
    if (committed is None or committed["actor"] != actor
            or committed["room_id"] != room_id
            or committed["kind"] != "political_offer"
            or committed["occurred_at"] != timestamp
            or json.loads(committed["payload_json"]) != payload):
        raise RuntimeError("Political contribution event does not survive readback")
    recorded = db.execute(
        "SELECT world_id,actor,faction_id,site_id,points "
        "FROM main.political_offerings WHERE item_id=? COLLATE NOCASE",
        (item["id"],),
    ).fetchone()
    if (recorded is None or recorded["world_id"] != world
            or recorded["actor"] != actor
            or recorded["faction_id"] != faction_id
            or recorded["site_id"] != site["id"]
            or recorded["points"] != approved["points"]
            or db.execute("SELECT 1 FROM main.items WHERE id=?",
                          (item["id"],)).fetchone() is not None):
        raise RuntimeError("Political contribution item custody failed readback")
    pts = int(approved["points"])
    word = "influence"  # Unchanging unit, bounded 1-5 per approved item.
    return (f"{actor} offered {item['name']} to {faction['display_name']} "
            f"at {site['display_name']} ({pts} {word}).")
