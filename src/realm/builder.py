"""Offline, setting-neutral authoring for REALM worldpack v1 files.

The builder never opens a game database, changes a running world, or grants
players permission to edit another host's world. A host must separately import
the resulting validated declaration into a new, pristine REALM instance.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from .worldpack import WorldpackError, read_worldpack, validate_worldpack


def _save(path: Path, pack: dict, *, new: bool = False) -> None:
    """Validate before changing a file and replace edits atomically."""
    normalized = validate_worldpack(pack)
    data = (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(data) > 512 * 1024:
        raise WorldpackError("Authored worldpack exceeds the importer file-size limit")
    if not new and not path.is_file():
        raise WorldpackError("Worldpack does not exist; create it with 'new' first")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".realm-pack-", suffix=".tmp",
                                         delete=False) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if new:
            # A hard-link installs the *complete* temporary file only if the
            # destination does not exist. Unlike open('xb') followed by write,
            # an interruption cannot strand a half-authored target file.
            # Source and destination share a directory/filesystem. If this
            # filesystem does not support hard links, fail closed.
            os.link(temp_name, path)
        else:
            os.replace(temp_name, path)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)


def new_world(path: str | Path, world_id: str, name: str, start_room: str,
              room_name: str, description: str) -> dict:
    pack = {"schema": "realm.worldpack.v1", "world_id": world_id, "name": name,
            "start_room": start_room,
            "rooms": [{"id": start_room, "name": room_name,
                       "description": description, "exits": {}}], "items": []}
    _save(Path(path), pack, new=True)
    return pack


def _edit(path: str | Path, operation) -> dict:
    path = Path(path)
    pack, _ = read_worldpack(path)
    operation(pack)
    _save(path, pack)
    return pack


def add_room(path: str | Path, room_id: str, name: str, description: str) -> dict:
    def add(pack):
        if any(room["id"] == room_id for room in pack["rooms"]):
            raise WorldpackError(f"Room already exists: {room_id}")
        pack["rooms"].append({"id": room_id, "name": name,
                              "description": description, "exits": {}})
    return _edit(path, add)


def link_rooms(path: str | Path, source: str, direction: str,
               destination: str, back: str | None = None) -> dict:
    def link(pack):
        rooms = {room["id"]: room for room in pack["rooms"]}
        if source not in rooms or destination not in rooms:
            raise WorldpackError("Both rooms must exist before linking them")
        if direction in rooms[source]["exits"]:
            raise WorldpackError(f"Exit already exists: {source}.{direction}")
        if back is not None and back in rooms[destination]["exits"]:
            raise WorldpackError(f"Return exit already exists: {destination}.{back}")
        if source == destination and back == direction:
            raise WorldpackError("Return exit would conflict with the forward exit")
        rooms[source]["exits"][direction] = destination
        if back is not None:
            rooms[destination]["exits"][back] = source
    return _edit(path, link)


def add_item(path: str | Path, item_id: str, name: str,
             description: str, room_id: str) -> dict:
    def add(pack):
        if any(item["id"].casefold() == item_id.casefold() for item in pack["items"]):
            raise WorldpackError(f"Item already exists: {item_id}")
        pack["items"].append({"id": item_id, "name": name,
                              "description": description, "room": room_id})
    return _edit(path, add)


def describe_world(path: str | Path) -> str:
    pack, digest = read_worldpack(path)
    lines = [f"{pack['name']} ({pack['world_id']})", f"Start: {pack['start_room']}",
             f"Rooms: {len(pack['rooms'])} | Items: {len(pack['items'])}",
             f"SHA-256 declaration: {digest}"]
    for room in pack["rooms"]:
        exits = ", ".join(f"{direction} -> {destination}"
                          for direction, destination in sorted(room["exits"].items()))
        lines.append(f"  {room['id']}: {room['name']} [{exits or 'no exits'}]")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Author offline REALM worldpacks; no live world is modified")
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="Create a worldpack with one starting room")
    new.add_argument("--world-id", required=True)
    new.add_argument("--name", required=True)
    new.add_argument("--start-room", required=True)
    new.add_argument("--room-name", required=True)
    new.add_argument("--description", required=True)

    room = commands.add_parser("room", help="Add a room (unlinked until 'exit')")
    room.add_argument("--id", required=True)
    room.add_argument("--name", required=True)
    room.add_argument("--description", required=True)

    exit_command = commands.add_parser("exit", help="Add a directed or two-way exit")
    exit_command.add_argument("--from-room", required=True)
    exit_command.add_argument("--direction", required=True)
    exit_command.add_argument("--to-room", required=True)
    exit_command.add_argument("--back", help="Optional return direction")

    item = commands.add_parser("item", help="Place an initial portable item")
    item.add_argument("--id", required=True)
    item.add_argument("--name", required=True)
    item.add_argument("--description", required=True)
    item.add_argument("--room", required=True)

    commands.add_parser("show", help="Validate and list rooms and exits")
    for command in commands.choices.values():
        command.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "new":
            new_world(args.file, args.world_id, args.name, args.start_room,
                      args.room_name, args.description)
        elif args.command == "room":
            add_room(args.file, args.id, args.name, args.description)
        elif args.command == "exit":
            link_rooms(args.file, args.from_room, args.direction, args.to_room, args.back)
        elif args.command == "item":
            add_item(args.file, args.id, args.name, args.description, args.room)
        print(describe_world(args.file))
    except (WorldpackError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
