"""Host-only ownership boundary for separately authored REALM world instances.

This is an in-process, local filesystem capability API, NOT remote account
authorization or a public multi-tenant hosting service. Each world receives a
separate worldpack, SQLite path and high-entropy owner capability. Untrusted
network clients must never be allowed to invoke ``create`` or supply a path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets

from .builder import add_item, add_room, describe_world, link_rooms, new_world
from .worldpack import WorldpackError, import_worldpack, read_worldpack, validate_worldpack


_WORLD_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_OWNER_SCHEMA = "realm.hosted-owner.v1"
_OWNER_FILENAME = "owner.json"
_PACK_FILENAME = "worldpack.json"
_DB_FILENAME = "world.sqlite3"
_HASH_PREFIX = b"REALM local world owner v1\0"


class WorldAccessError(PermissionError):
    """The supplied host-held capability does not authorize this world."""


def _token_digest(token: str) -> str:
    return hashlib.sha256(_HASH_PREFIX + token.encode("utf-8")).hexdigest()


def _write_new_private_file(path: Path, content: bytes) -> None:
    """Write a private file with exclusive creation; never replace another."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd != -1:
            os.close(fd)


class HostedWorlds:
    """Local operator API for separately owned *offline* authored worlds.

    Construct only with a private directory controlled by the host OS account.
    A local process that can rewrite this directory or import this Python class
    with arbitrary filesystem access is outside this API's security boundary.
    The capability is an in-process authorization input, not a network login.
    """

    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root = path.resolve(strict=True)
        if not self._root.is_dir():
            raise NotADirectoryError(self._root)

    def _world_dir(self, world_id: str) -> Path:
        # Disallow path traversal and avoid world name/path authority confusion.
        if not isinstance(world_id, str) or not _WORLD_ID.fullmatch(world_id):
            raise WorldAccessError("Host-owned world capability required")
        path = self._root / world_id
        if path.is_symlink() or path.resolve(strict=False).parent != self._root:
            raise WorldAccessError("Host-owned world capability required")
        return path

    @staticmethod
    def _check_regular(path: Path, parent: Path) -> None:
        if path.is_symlink() or path.resolve(strict=False).parent != parent:
            raise WorldAccessError("Host-owned world capability required")
        if path.exists() and not path.is_file():
            raise WorldAccessError("Host-owned world capability required")

    def create(
        self, *, world_id: str, name: str, start_room: str,
        room_name: str, description: str,
    ) -> str:
        """Host creates a new private authored world; returns secret **once**.

        A failed/interrupted creation may leave an incomplete reserved folder.
        No operation treats a folder as owned until owner metadata is complete.
        """
        folder = self._world_dir(world_id)
        # Validate all user-authored fields before any path changes.
        proposal = {
            "schema": "realm.worldpack.v1", "world_id": world_id, "name": name,
            "start_room": start_room,
            "rooms": [{"id": start_room, "name": room_name,
                       "description": description, "exits": {}}],
            "items": [],
        }
        validate_worldpack(proposal)
        folder.mkdir(mode=0o700, exist_ok=False)
        token = secrets.token_urlsafe(32)
        pack = folder / _PACK_FILENAME
        new_world(pack, world_id, name, start_room, room_name, description)
        metadata = {"schema": _OWNER_SCHEMA, "world_id": world_id,
                    "owner_sha256": _token_digest(token)}
        # Metadata is the last file created; an incomplete folder is unusable.
        _write_new_private_file(
            folder / _OWNER_FILENAME,
            (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        return token

    def _authorized_paths(self, world_id: str, capability: str) -> tuple[Path, Path]:
        folder = self._world_dir(world_id)
        if not isinstance(capability, str) or not 1 <= len(capability) <= 256:
            raise WorldAccessError("Host-owned world capability required")
        metadata_path = folder / _OWNER_FILENAME
        pack = folder / _PACK_FILENAME
        db = folder / _DB_FILENAME
        for item in (metadata_path, pack, db):
            self._check_regular(item, folder)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (not isinstance(metadata, dict)
                    or set(metadata) != {"schema", "world_id", "owner_sha256"}
                    or metadata["schema"] != _OWNER_SCHEMA
                    or metadata["world_id"] != world_id
                    or not isinstance(metadata["owner_sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", metadata["owner_sha256"])
                    or not hmac.compare_digest(_token_digest(capability), metadata["owner_sha256"])):
                raise WorldAccessError("Host-owned world capability required")
            worldpack, _ = read_worldpack(pack)
            if worldpack["world_id"] != world_id:
                raise WorldAccessError("Host-owned world capability required")
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise WorldAccessError("Host-owned world capability required") from exc
        return pack, db

    def describe(self, world_id: str, *, capability: str) -> str:
        pack, _ = self._authorized_paths(world_id, capability)
        return describe_world(pack)

    def add_room(self, world_id: str, *, capability: str,
                 room_id: str, name: str, description: str) -> dict:
        pack = self._editable_worldpack(world_id, capability)
        return add_room(pack, room_id, name, description)

    def link_rooms(self, world_id: str, *, capability: str,
                   source: str, direction: str, destination: str,
                   back: str | None = None) -> dict:
        pack = self._editable_worldpack(world_id, capability)
        return link_rooms(pack, source, direction, destination, back)

    def add_item(self, world_id: str, *, capability: str,
                 item_id: str, name: str, description: str,
                 room_id: str) -> dict:
        pack = self._editable_worldpack(world_id, capability)
        return add_item(pack, item_id, name, description, room_id)

    def _editable_worldpack(self, world_id: str, capability: str) -> Path:
        pack, db = self._authorized_paths(world_id, capability)
        # Only offline staging is supported; live OLC/versioned migration is
        # separate work. Even an empty imported world may have durable binding.
        if db.exists():
            raise WorldpackError("World is already provisioned; live edits require an explicit migration")
        return pack

    def provision_instance(self, world_id: str, *, capability: str) -> dict:
        """Import this host's authorized worldpack into its *own* SQLite DB."""
        pack, db = self._authorized_paths(world_id, capability)
        return import_worldpack(db, pack)

    def instance_path(self, world_id: str, *, capability: str) -> Path:
        """Return path to an existing DB only to authorized local host code.

        This does not start a gateway or open a port. Future remote-player
        authentication cannot be derived from this owner capability alone.
        """
        _, db = self._authorized_paths(world_id, capability)
        if not db.is_file():
            raise WorldpackError("World has not been provisioned")
        return db
