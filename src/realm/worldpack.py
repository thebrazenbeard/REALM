"""Import a validated, setting-neutral worldpack into a fresh REALM database.

Worldpacks describe rooms, exits, and initial items; they are not executable
plugins, game rules, AI prompts, or authority to modify a populated world.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .auth import _CREDENTIAL_SCHEMA_SQL
from .engine import Engine, _SEED_EXITS, _SEED_ITEMS, _SEED_ROOMS, _pristine_schema, _schema_snapshot


SCHEMA = "realm.worldpack.v1"
MAX_SOURCE_BYTES = 512 * 1024
MAX_ROOMS = 512
MAX_ITEMS = 2048
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_EXIT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class WorldpackError(ValueError):
    """Input or destination world rejected without replacing existing data."""


def _object(value: Any, path: str, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise WorldpackError(f"{path} must be an object")
    unexpected = set(value) - required - optional
    absent = required - set(value)
    if unexpected or absent:
        raise WorldpackError(f"{path} has invalid fields: missing={sorted(absent)}, extra={sorted(unexpected)}")
    return value


def _string(
    value: Any, path: str, *, minimum: int = 1,
    maximum: int = 256, multiline: bool = False,
) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise WorldpackError(f"{path} must be text of length {minimum}–{maximum}")
    if not value.strip() or any(
        (ord(char) < 32 and not (multiline and char == "\n"))
        or ord(char) == 127
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise WorldpackError(f"{path} contains empty or control-character text")
    return value


def _identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise WorldpackError(f"{path} must be a lower-case ID: [a-z][a-z0-9_-], up to 64 characters")
    return value


def validate_worldpack(data: Any) -> dict:
    """Return a normalized trusted shape, rejecting unknown schema fields.

    The validator has no file-system, database, network, or model side effects.
    """
    data = _object(data, "worldpack", {"schema", "world_id", "name", "start_room", "rooms"}, {"items"})
    if data["schema"] != SCHEMA:
        raise WorldpackError(f"Unsupported worldpack schema; expected {SCHEMA}")
    world_id = _identifier(data["world_id"], "world_id")
    name = _string(data["name"], "name", maximum=100)
    start_room = _identifier(data["start_room"], "start_room")
    rooms_input = data["rooms"]
    if not isinstance(rooms_input, list) or not 1 <= len(rooms_input) <= MAX_ROOMS:
        raise WorldpackError(f"rooms must contain 1–{MAX_ROOMS} entries")

    rooms = []
    room_ids = set()
    for index, source in enumerate(rooms_input):
        path = f"rooms[{index}]"
        source = _object(source, path, {"id", "name", "description", "exits"})
        room_id = _identifier(source["id"], f"{path}.id")
        if room_id in room_ids:
            raise WorldpackError(f"Duplicate room ID: {room_id}")
        room_ids.add(room_id)
        exits = source["exits"]
        if not isinstance(exits, dict) or len(exits) > 12:
            raise WorldpackError(f"{path}.exits must be an object with 0–12 directions")
        normalized_exits = {}
        for direction, destination in exits.items():
            if not isinstance(direction, str) or _EXIT.fullmatch(direction) is None:
                raise WorldpackError(f"{path}.exits has invalid direction: {direction!r}")
            normalized_exits[direction] = _identifier(destination, f"{path}.exits.{direction}")
        rooms.append({
            "id": room_id,
            "name": _string(source["name"], f"{path}.name", maximum=100),
            "description": _string(source["description"], f"{path}.description", maximum=5000, multiline=True),
            "exits": normalized_exits,
        })
    if start_room not in room_ids:
        raise WorldpackError(f"start_room does not exist: {start_room}")
    for room in rooms:
        for direction, destination in room["exits"].items():
            if destination not in room_ids:
                raise WorldpackError(f"Dangling exit {room['id']}.{direction}: {destination}")

    items_input = data.get("items", [])
    if not isinstance(items_input, list) or len(items_input) > MAX_ITEMS:
        raise WorldpackError(f"items must contain 0–{MAX_ITEMS} entries")
    items = []
    item_ids = set()
    for index, source in enumerate(items_input):
        path = f"items[{index}]"
        source = _object(source, path, {"id", "name", "description", "room"})
        item_id = _identifier(source["id"], f"{path}.id")
        # Item lookup uses COLLATE NOCASE; avoid ambiguous identifiers.
        if item_id.casefold() in item_ids:
            raise WorldpackError(f"Duplicate item ID: {item_id}")
        item_ids.add(item_id.casefold())
        room_id = _identifier(source["room"], f"{path}.room")
        if room_id not in room_ids:
            raise WorldpackError(f"Item {item_id} has unknown room: {room_id}")
        items.append({
            "id": item_id,
            "name": _string(source["name"], f"{path}.name", maximum=100),
            "description": _string(source["description"], f"{path}.description", maximum=2000, multiline=True),
            "room": room_id,
        })
    return {
        "schema": SCHEMA, "world_id": world_id, "name": name,
        "start_room": start_room, "rooms": rooms, "items": items,
    }


def read_worldpack(path: str | Path) -> tuple[dict, str]:
    """Read and validate a bounded JSON source and return its semantic hash."""
    path = Path(path)
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise WorldpackError(f"Worldpack exceeds {MAX_SOURCE_BYTES} bytes")
    source = path.read_bytes()
    if len(source) > MAX_SOURCE_BYTES:
        raise WorldpackError(f"Worldpack exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        data = json.loads(source.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorldpackError(f"Worldpack is not valid UTF-8 JSON: {exc}") from exc
    pack = validate_worldpack(data)
    semantic = json.dumps(pack, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return pack, hashlib.sha256(semantic).hexdigest()


def _verify_installed_worldpack(db, pack: dict, digest: str) -> None:
    """Fail closed on an idempotent retry if imported v1 structure drifted.

    In v1, room and exit definitions and item identities/descriptions are
    authored, static worldpack data. Players can legitimately carry/drop items,
    so their current room/holder is deliberately excluded from this comparison.
    This is *not* an anti-tamper seal or a full SQLite integrity attestation.
    Call only inside the importer's BEGIN IMMEDIATE transaction.
    """
    metadata = dict(db.execute(
        "SELECT key,value FROM world_meta WHERE key IN "
        "('schema','world_id','name','start_room','worldpack_sha256')"
    ))
    if metadata != {
        "schema": SCHEMA,
        "world_id": pack["world_id"],
        "name": pack["name"],
        "start_room": pack["start_room"],
        "worldpack_sha256": digest,
    }:
        raise WorldpackError(
            "Bound world integrity failure: installed identity/metadata differs from this worldpack"
        )

    authored_rooms = sorted(
        (room["id"], room["name"], room["description"]) for room in pack["rooms"]
    )
    actual_rooms = [tuple(row) for row in db.execute(
        "SELECT id,name,description FROM rooms ORDER BY id"
    )]
    if actual_rooms != authored_rooms:
        raise WorldpackError(
            "Bound world integrity failure: authored rooms were removed or changed"
        )

    authored_exits = sorted(
        (room["id"], direction, destination)
        for room in pack["rooms"]
        for direction, destination in room["exits"].items()
    )
    actual_exits = [tuple(row) for row in db.execute(
        "SELECT room_id,direction,destination FROM exits ORDER BY room_id,direction"
    )]
    if actual_exits != authored_exits:
        raise WorldpackError(
            "Bound world integrity failure: authored exits were removed or changed"
        )

    authored_items = sorted(
        (item["id"], item["name"], item["description"])
        for item in pack["items"]
    )
    actual_items = [tuple(row) for row in db.execute(
        "SELECT id,name,description FROM items ORDER BY id"
    )]
    if actual_items != authored_items:
        raise WorldpackError(
            "Bound world integrity failure: authored item definitions were removed or changed"
        )

    # Carried/dropped locations legitimately change, but a damaged local writer
    # can bypass SQLite CHECK constraints via PRAGMA ignore_check_constraints.
    # An item must have exactly one location: room XOR holder. Foreign-key
    # checks alone do not detect a simultaneously held-and-grounded item.
    if db.execute(
        "SELECT EXISTS(SELECT 1 FROM items WHERE "
        "(room_id IS NULL AND held_by IS NULL) "
        "OR (room_id IS NOT NULL AND held_by IS NOT NULL))"
    ).fetchone()[0]:
        raise WorldpackError(
            "Bound world integrity failure: item has invalid room/holder placement"
        )

    # The database file may have been edited by a process that disabled FK
    # checks. SQLite does not retroactively validate those edits on open.
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise WorldpackError(
            "Bound world integrity failure: orphaned SQLite references"
        )


def import_worldpack(db_path: str | Path, worldpack_path: str | Path) -> dict:
    """Import once into a pristine built-in demo database, preserving live data.

    Same-hash retries return unchanged only after a bounded structural check
    of installed worldpack data. A different pack or an already-used demo
    world is rejected. There is no force/reset option.
    """
    pack, digest = read_worldpack(worldpack_path)
    engine = Engine(db_path)
    with closing(engine._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        old_hash = db.execute("SELECT value FROM world_meta WHERE key='worldpack_sha256'").fetchone()
        if old_hash is not None:
            if old_hash[0] != digest:
                raise WorldpackError("A different worldpack is already installed; existing world state is protected")
            _verify_installed_worldpack(db, pack, digest)
            return {"world_id": pack["world_id"], "sha256": digest, "unchanged": True}
        existing_world_id = db.execute(
            "SELECT value FROM world_meta WHERE key='world_id'"
        ).fetchone()
        if existing_world_id is not None and existing_world_id[0] != pack["world_id"]:
            raise WorldpackError(
                "World database is already bound to another world_id; create a fresh database for this pack"
            )
        if any(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in
               ("players", "events", "action_receipts")):
            raise WorldpackError("The demo world has player or event history; import requires a pristine world")
        credential_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='player_credentials'"
        ).fetchone()
        if credential_table is not None and db.execute(
            "SELECT EXISTS(SELECT 1 FROM player_credentials)"
        ).fetchone()[0]:
            raise WorldpackError("The demo world has provisioned credentials; import before provisioning players")
        expected_schema = _pristine_schema(
            _CREDENTIAL_SCHEMA_SQL if credential_table is not None else ""
        )
        if _schema_snapshot(db) != expected_schema:
            raise WorldpackError("The demo world has custom SQL schema objects; refusing import")
        if db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_sequence)").fetchone()[0]:
            raise WorldpackError("The demo world has past event IDs; import requires a pristine world")
        present_rooms = [tuple(row) for row in db.execute("SELECT id,name,description FROM rooms ORDER BY id")]
        present_exits = [tuple(row) for row in db.execute("SELECT room_id,direction,destination FROM exits ORDER BY room_id,direction")]
        present_items = [tuple(row) for row in db.execute(
            "SELECT id,name,description,room_id FROM items ORDER BY id")]
        if (present_rooms != sorted(_SEED_ROOMS)
            or present_exits != sorted(_SEED_EXITS)
            or present_items != sorted(_SEED_ITEMS)):
            raise WorldpackError("World state differs from the pristine demo; refusing destructive replacement")
        db.execute("DELETE FROM items")
        db.execute("DELETE FROM exits")
        db.execute("DELETE FROM rooms")
        db.executemany(
            "INSERT INTO rooms(id,name,description) VALUES(?,?,?)",
            [(r["id"], r["name"], r["description"]) for r in pack["rooms"]],
        )
        db.executemany(
            "INSERT INTO exits(room_id,direction,destination) VALUES(?,?,?)",
            [(r["id"], direction, dest) for r in pack["rooms"] for direction, dest in r["exits"].items()],
        )
        db.executemany(
            "INSERT INTO items(id,name,description,room_id) VALUES(?,?,?,?)",
            [(item["id"], item["name"], item["description"], item["room"]) for item in pack["items"]],
        )
        db.executemany("INSERT INTO world_meta(key,value) VALUES(?,?)", [
            ("schema", SCHEMA), ("name", pack["name"]),
            ("start_room", pack["start_room"]), ("worldpack_sha256", digest),
        ])
        if existing_world_id is None:
            db.execute("INSERT INTO world_meta(key,value) VALUES('world_id',?)", (pack["world_id"],))
    return {"world_id": pack["world_id"], "sha256": digest, "unchanged": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a validated worldpack into a fresh local REALM instance")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True, help="JSON worldpack")
    args = parser.parse_args(argv)
    try:
        result = import_worldpack(args.db, args.file)
    except (WorldpackError, OSError) as exc:
        parser.error(str(exc))
    status = "unchanged" if result["unchanged"] else "imported"
    print(f"Worldpack {status}: {result['world_id']} (sha256={result['sha256']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
