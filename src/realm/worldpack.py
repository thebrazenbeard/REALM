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

from .engine import Engine, _SEED_EXITS, _SEED_ITEMS, _SEED_ROOMS


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


def import_worldpack(db_path: str | Path, worldpack_path: str | Path) -> dict:
    """Import once into a pristine built-in demo database, preserving live data.

    Same-hash retries return an unchanged result. A different pack or an
    already-used demo world is rejected. There is no force/reset option.
    """
    pack, digest = read_worldpack(worldpack_path)
    engine = Engine(db_path)
    with closing(engine._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        old_hash = db.execute("SELECT value FROM world_meta WHERE key='worldpack_sha256'").fetchone()
        if old_hash is not None:
            if old_hash[0] != digest:
                raise WorldpackError("A different worldpack is already installed; existing world state is protected")
            return {"world_id": pack["world_id"], "sha256": digest, "unchanged": True}
        if any(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in
               ("players", "events", "action_receipts")):
            raise WorldpackError("The demo world has player or event history; import requires a pristine world")
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
            ("schema", SCHEMA), ("world_id", pack["world_id"]), ("name", pack["name"]),
            ("start_room", pack["start_room"]), ("worldpack_sha256", digest),
        ])
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
