"""Governed, additive expansion of an already-bound REALM world.

This is a generic host-side transaction seam, NOT a content/canon/rights release.
The default is HELD. A trusted host must supply a separately verified owner
authorizer for the exact review; a caller-provided callback is not itself proof
of Patrick's approval. Never ingest private lore through this API implicitly.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .worldpack import MAX_ITEMS, MAX_ROOMS

SCHEMA = "realm.world-expansion.v1"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DIRECTION = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_PROTECTED = frozenset({
    "rooms", "exits", "items", "players", "events", "action_receipts",
    "world_meta", "world_expansion_ledger",
})


class ExpansionError(ValueError):
    """Invalid expansion or violated world/revision/history invariant."""


class ExpansionHeld(ExpansionError):
    """Valid proposed change, but exact trusted owner authority is unavailable."""


def _object(value: Any, fields: set[str], where: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ExpansionError(f"{where} requires exact fields {sorted(fields)}")
    return value


def _id(value: Any, where: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ExpansionError(f"{where}: invalid ID")
    return value


def _label(value: Any, where: str, limit: int = 5000) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ExpansionError(f"{where}: missing or untrimmed text")
    if len(value) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ExpansionError(f"{where}: oversized or control text")
    return value


def _hash(value: Any, where: str) -> str:
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ExpansionError(f"{where}: expected lowercase SHA-256")
    return value


def _validate(data: Any) -> tuple[dict[str, Any], str]:
    fields = {
        "schema", "world_id", "base_worldpack_sha256", "expected_revision",
        "expansion_id", "provenance", "rooms", "exits", "items",
    }
    p = _object(copy.deepcopy(data), fields, "expansion")
    if p["schema"] != SCHEMA:
        raise ExpansionError("unsupported expansion schema")
    _id(p["world_id"], "world_id")
    _id(p["expansion_id"], "expansion_id")
    _hash(p["base_worldpack_sha256"], "base_worldpack_sha256")
    rev = p["expected_revision"]
    if type(rev) is not int or not 0 <= rev <= 1_000_000_000:
        raise ExpansionError("invalid expected_revision")
    provenance = _object(p["provenance"], {"source_id", "source_sha256"}, "provenance")
    _label(provenance["source_id"], "source_id", 256)
    _hash(provenance["source_sha256"], "source_sha256")
    if any(type(p[key]) is not list for key in ("rooms", "exits", "items")):
        raise ExpansionError("rooms/exits/items must be lists")
    if not any(p[key] for key in ("rooms", "exits", "items")):
        raise ExpansionError("empty expansion")
    if len(p["rooms"]) > MAX_ROOMS or len(p["items"]) > MAX_ITEMS or len(p["exits"]) > MAX_ROOMS * 12:
        raise ExpansionError("expansion exceeds bounded entity limits")

    seen_rooms: set[str] = set()
    for room in p["rooms"]:
        _object(room, {"id", "name", "description"}, "room")
        rid = _id(room["id"], "room.id")
        if rid.casefold() in seen_rooms:
            raise ExpansionError("duplicate added room ID")
        seen_rooms.add(rid.casefold())
        _label(room["name"], "room.name", 200)
        _label(room["description"], "room.description")
    seen_items: set[str] = set()
    for item in p["items"]:
        _object(item, {"id", "name", "description", "room"}, "item")
        iid = _id(item["id"], "item.id")
        if iid.casefold() in seen_items:
            raise ExpansionError("duplicate added item ID")
        seen_items.add(iid.casefold())
        _label(item["name"], "item.name", 200)
        _label(item["description"], "item.description")
        _id(item["room"], "item.room")
    seen_exits: set[tuple[str, str]] = set()
    for link in p["exits"]:
        _object(link, {"room_id", "direction", "destination"}, "exit")
        rid = _id(link["room_id"], "exit.room_id")
        direction = link["direction"]
        if type(direction) is not str or not _DIRECTION.fullmatch(direction):
            raise ExpansionError("invalid exit.direction")
        _id(link["destination"], "exit.destination")
        key = (rid.casefold(), direction.casefold())
        if key in seen_exits:
            raise ExpansionError("duplicate added exit")
        seen_exits.add(key)
    canonical = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(canonical.encode("utf-8")) > 512 * 1024:
        raise ExpansionError("oversized expansion")
    return p, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _state(conn: sqlite3.Connection, p: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    temp = conn.execute(
        "SELECT name FROM sqlite_temp_master WHERE type IN ('table','view')"
    ).fetchall()
    if any(row[0].casefold() in _PROTECTED for row in temp):
        raise ExpansionError("SQLite TEMP schema shadows protected world tables")
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table'"
        )
    }
    if not {"rooms", "exits", "items", "players", "events", "action_receipts", "world_meta"} <= tables:
        raise ExpansionError("not an initialized REALM world")
    meta = dict(conn.execute("SELECT key,value FROM main.world_meta").fetchall())
    if meta.get("schema") != "realm.worldpack.v1" or not meta.get("world_id") or not meta.get("worldpack_sha256"):
        raise ExpansionError("played world has no validated worldpack binding")
    if meta["world_id"] != p["world_id"]:
        raise ExpansionError("cross-world expansion forbidden")
    if meta["worldpack_sha256"] != p["base_worldpack_sha256"]:
        raise ExpansionError("original canonical worldpack binding changed")
    try:
        rev = int(meta.get("expansion_revision", "0"))
        if str(rev) != meta.get("expansion_revision", "0") or rev < 0:
            raise ValueError()
    except ValueError as exc:
        raise ExpansionError("corrupt expansion revision") from exc
    if "world_expansion_ledger" not in tables:
        if rev != 0:
            raise ExpansionError("revision exists without durable expansion ledger")
        return rev, None
    latest = conn.execute(
        "SELECT MAX(revision),COUNT(*) FROM main.world_expansion_ledger"
    ).fetchone()
    if int(latest[0] or 0) != rev or int(latest[1]) != rev:
        raise ExpansionError("expansion ledger/revision conflict")
    receipt = conn.execute(
        "SELECT proposal_sha256,revision,source_id,source_sha256 "
        "FROM main.world_expansion_ledger WHERE expansion_id=?", (p["expansion_id"],)
    ).fetchone()
    return rev, dict(receipt) if receipt is not None else None


def _review(conn: sqlite3.Connection, p: dict[str, Any], digest: str) -> dict[str, Any]:
    rev, receipt = _state(conn, p)
    if receipt is not None:
        if receipt["proposal_sha256"] != digest:
            raise ExpansionError("expansion_id replay with changed proposal")
        return {
            "world_id": p["world_id"], "expansion_id": p["expansion_id"],
            "base_worldpack_sha256": p["base_worldpack_sha256"],
            "proposal_sha256": digest, "provenance": dict(p["provenance"]),
            "expected_revision": p["expected_revision"], "current_revision": rev,
            "revision": int(receipt["revision"]), "additions": {"rooms": [], "exits": [], "items": []},
            "replayed": True, "held": True,
        }
    if rev != p["expected_revision"]:
        raise ExpansionError("stale expected_revision (CAS)")
    existing_rooms = {row[0].casefold() for row in conn.execute("SELECT id FROM main.rooms")}
    existing_items = {row[0].casefold() for row in conn.execute("SELECT id FROM main.items")}
    existing_exits = {
        (a.casefold(), b.casefold()) for a, b in
        conn.execute("SELECT room_id,direction FROM main.exits")
    }
    new_rooms = {room["id"].casefold() for room in p["rooms"]}
    if existing_rooms & new_rooms:
        raise ExpansionError("cannot overwrite existing room or historical canon")
    if existing_items & {item["id"].casefold() for item in p["items"]}:
        raise ExpansionError("cannot overwrite existing item or historical canon")
    if len(existing_rooms) + len(new_rooms) > MAX_ROOMS:
        raise ExpansionError("world room cap exceeded")
    if len(existing_items) + len(p["items"]) > MAX_ITEMS:
        raise ExpansionError("world item cap exceeded")
    all_rooms = existing_rooms | new_rooms
    if any(item["room"].casefold() not in all_rooms for item in p["items"]):
        raise ExpansionError("item targets dangling room")
    room_exit_counts = dict(conn.execute(
        "SELECT room_id,COUNT(*) FROM main.exits GROUP BY room_id"
    ).fetchall())
    for link in p["exits"]:
        key = (link["room_id"].casefold(), link["direction"].casefold())
        if key in existing_exits:
            raise ExpansionError("cannot overwrite existing exit or historical canon")
        if key[0] not in all_rooms or link["destination"].casefold() not in all_rooms:
            raise ExpansionError("dangling exit")
        room_exit_counts[key[0]] = room_exit_counts.get(key[0], 0) + 1
    if any(count > 12 for count in room_exit_counts.values()):
        raise ExpansionError("room exit cap exceeded")
    return {
        "world_id": p["world_id"], "expansion_id": p["expansion_id"],
        "base_worldpack_sha256": p["base_worldpack_sha256"],
        "proposal_sha256": digest, "provenance": dict(p["provenance"]),
        "expected_revision": p["expected_revision"], "current_revision": rev,
        "revision": rev + 1,
        "additions": {
            "rooms": sorted(room["id"] for room in p["rooms"]),
            "exits": sorted(f"{link['room_id']}:{link['direction']}->{link['destination']}" for link in p["exits"]),
            "items": sorted(item["id"] for item in p["items"]),
        },
        "replayed": False, "held": True,
    }


def inspect_expansion(engine: Any, proposal: dict[str, Any]) -> dict[str, Any]:
    """Read-only deterministic diff; never grants world import or canon approval."""
    p, digest = _validate(proposal)
    with closing(engine._connect()) as conn:
        return _review(conn, p, digest)


def _apply_synthetic_transaction(
    engine: Any,
    proposal: dict[str, Any],
    *,
    owner_authorizer: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """INTERNAL synthetic-only transaction verifier. Not a canon/rights/release gate.

    This primitive tests the proposed host transaction in disposable worlds.
    It MUST NOT be wired to production, a player route, or private-world data.
    A Python callback proves caller control only, never Patrick's exact release.
    """
    p, digest = _validate(proposal)
    # Never let the internal fixture accidentally touch a hosted world.
    test_root = Path(tempfile.gettempdir()).resolve()
    db_path = Path(engine.db_path).resolve()
    if (p["world_id"] != "sample"
            or p["provenance"]["source_id"] != "synthetic-only"
            or not db_path.is_relative_to(test_root)):
        raise ExpansionHeld("synthetic transaction fixture is forbidden on hosted worlds")
    with closing(engine._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            review = _review(conn, p, digest)
            if owner_authorizer is None or owner_authorizer(review) is not True:
                raise ExpansionHeld("owner/canon/source-rights/host approval unverified: HELD")
            if review["replayed"]:
                conn.rollback()
                return {**review, "held": False}
            conn.execute(
                "CREATE TABLE IF NOT EXISTS main.world_expansion_ledger ("
                "expansion_id TEXT PRIMARY KEY, proposal_sha256 TEXT NOT NULL,"
                "world_id TEXT NOT NULL, base_worldpack_sha256 TEXT NOT NULL,"
                "source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,"
                "previous_revision INTEGER NOT NULL UNIQUE, revision INTEGER NOT NULL UNIQUE,"
                "committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.executemany(
                "INSERT INTO main.rooms(id,name,description) VALUES (?,?,?)",
                [(x["id"], x["name"], x["description"]) for x in p["rooms"]],
            )
            conn.executemany(
                "INSERT INTO main.exits(room_id,direction,destination) VALUES (?,?,?)",
                [(x["room_id"], x["direction"], x["destination"]) for x in p["exits"]],
            )
            conn.executemany(
                "INSERT INTO main.items(id,name,description,room_id,held_by) VALUES (?,?,?,?,NULL)",
                [(x["id"], x["name"], x["description"], x["room"]) for x in p["items"]],
            )
            conn.execute(
                "INSERT INTO main.world_expansion_ledger("
                "expansion_id,proposal_sha256,world_id,base_worldpack_sha256,"
                "source_id,source_sha256,previous_revision,revision) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    p["expansion_id"], digest, p["world_id"], p["base_worldpack_sha256"],
                    p["provenance"]["source_id"], p["provenance"]["source_sha256"],
                    review["current_revision"], review["revision"],
                ),
            )
            conn.execute(
                "INSERT INTO main.world_meta(key,value) VALUES ('expansion_revision',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(review["revision"]),),
            )
            conn.commit()
            return {**review, "held": False}
        except BaseException:
            conn.rollback()
            raise


def apply_expansion(
    engine: Any,
    proposal: dict[str, Any],
    *,
    owner_authorizer: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Fail-closed public apply seam until an independent release owner is bound.

    Even a callback returning True cannot establish Patrick's authorization.
    No production release verifier has been installed or qualified here.
    """
    inspect_expansion(engine, proposal)
    raise ExpansionHeld(
        "HELD: an injected callback or host HMAC is not independent "
        "owner/canon/source-rights/revision-bound release evidence"
    )
