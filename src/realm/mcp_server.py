"""Local stdio MCP adapter for one explicitly bound REALM world and character.

This process does not expose a network listener.  Its host supplies the world
database, world ID and one player bearer token through the process environment;
tool arguments can never select an actor, a database or an identity.  Gameplay
continues to pass through the same Gateway and CommandRouter as HTTP clients.

An MCP stdio client is a trusted local process with the authority of the bound
player.  It must not expose these tools to an untrusted remote principal. The
selected world database and its parent directory must be host-trusted: read-only
schema/world/token preflight is not atomic with later SQLite path-based commits,
so a local adversary able to replace database files can defeat path pinning.
"""

from __future__ import annotations

from contextlib import closing
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import IO, Any

from .auth import CredentialStore
from .engine import _LONG_DIRECTIONS, _PLAYER_RE, _schema_snapshot
from .gateway import Gateway
from .geography import MapUnavailable


MAX_LINE_BYTES = 32_768
MAX_RESULT_BYTES = 65_536
_WORLD_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SUPPORTED_PROTOCOLS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})


class RealmMCPError(ValueError):
    """The selected world, actor, tool or argument is not admitted."""


@lru_cache(maxsize=1)
def _required_schema() -> dict[str, tuple[str, str, str | None]]:
    """Derive the source-version's baseline from REALM itself, not copied DDL.

    The disposable fixture is separate from the selected world.  Custom
    worldpack objects may be added, but every built-in definition must match.
    """
    with TemporaryDirectory(prefix="realm-mcp-schema-") as temporary:
        fixture = Path(temporary) / "baseline.sqlite3"
        Gateway(fixture, world_id="schema_fixture")
        with closing(sqlite3.connect(fixture)) as db:
            return _schema_snapshot(db)


def _existing_bound_world(path: str, world_id: str, token: str) -> Path:
    """Read-only schema, world and actor gate BEFORE Gateway touches the DB."""
    if type(path) is not str or not path or type(world_id) is not str or not _WORLD_ID.fullmatch(world_id):
        raise RealmMCPError("explicit world database path and valid world ID required")
    if type(token) is not str or not 16 <= len(token) <= 255 or not token.isascii():
        raise RealmMCPError("host player credential is missing or malformed")
    raw = Path(path)
    if not raw.is_absolute() or raw.is_symlink() or not raw.is_file():
        raise RealmMCPError("selected world database must be an existing regular absolute file")
    resolved = raw.resolve(strict=True)
    try:
        with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            snapshot = _schema_snapshot(db)
            expected = _required_schema()
            if any(snapshot.get(name) != definition for name, definition in expected.items()):
                raise RealmMCPError("selected world schema is incomplete or incompatible")
            row = db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()
            if row is None or row[0] != world_id:
                raise RealmMCPError("selected world ID does not match database binding")
            if db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
                raise RealmMCPError("selected world has no authored or seeded rooms")
            actor = db.execute(
                "SELECT player_name FROM player_credentials WHERE token_digest=?",
                (CredentialStore._digest(token),),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise RealmMCPError("selected world database is unavailable or incompatible") from exc
    if actor is None or type(actor[0]) is not str or not _PLAYER_RE.fullmatch(actor[0]):
        raise RealmMCPError("host player credential is invalid or revoked")
    return resolved


def _tool(name: str, description: str, properties: dict[str, object], required: tuple[str, ...] = ()) -> dict[str, object]:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False}}


TOOLS: tuple[dict[str, object], ...] = (
    _tool("realm_status", "Read the bound world and authenticated character identity; no arbitrary actor selection.", {}),
    _tool("realm_join", "Join the host-configured character in its existing REALM world. May create the character if it has never joined.", {}),
    _tool("realm_look", "Read the room around the currently bound character, without moving.", {}),
    _tool("realm_inventory", "Inspect carried items as the bound character, without creating an action receipt.", {}),
    _tool("realm_move", "Move the bound character in an explicit direction. Reuse the action_id on uncertain retries to prevent duplicate movement.",
          {"direction": {"type": "string", "enum": sorted(_LONG_DIRECTIONS)},
           "action_id": {"type": "string", "minLength": 1, "maxLength": 128}}, ("direction", "action_id")),
    _tool("realm_command", "Execute an explicit non-speech game command as the bound player. This mutates persistent world state when the command is an action. Use a fresh action_id per intended action; reuse it for retries. Use realm_say for speech.",
          {"command": {"type": "string", "minLength": 1, "maxLength": 512},
           "action_id": {"type": "string", "minLength": 1, "maxLength": 128}}, ("command", "action_id")),
    _tool("realm_say", "Post a message to the shared game room as the bound player. This is public to occupants and durable in the world history; ordinary assistant conversation must not call it.",
          {"message": {"type": "string", "minLength": 1, "maxLength": 280},
           "action_id": {"type": "string", "minLength": 1, "maxLength": 128}}, ("message", "action_id")),
    _tool("realm_feed", "Read recent events visible in the bound character's current room, after an event cursor.",
          {"after": {"type": "integer", "minimum": 0}}, ()),
    _tool("realm_map", "Read the authored map visible at the bound character's current room; no fog of war yet.", {}),
)
_TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


def _arguments(arguments: object, allowed: frozenset[str], required: frozenset[str] = frozenset()) -> dict[str, object]:
    if type(arguments) is not dict or not set(arguments).issubset(allowed) or not required.issubset(arguments):
        raise RealmMCPError("unknown, missing or unauthorized tool arguments")
    return arguments


def _action_id(value: object) -> str:
    if type(value) is not str or _ACTION_ID.fullmatch(value) is None:
        raise RealmMCPError("explicit bounded action_id required for persistent commands")
    return value


class RealmMCPBridge:
    def __init__(self, path: str, world_id: str, token: str):
        self.db_path = _existing_bound_world(path, world_id, token)
        self._world_id = world_id
        self._token = token
        self.gateway = Gateway(self.db_path, world_id=world_id)
        self._bound_actor = self.gateway.credentials.authenticate(token)
        if self._bound_actor is None:
            raise RealmMCPError("host player credential is invalid or revoked")

    def _actor(self) -> str:
        # Revalidate the selected world and credential at every invocation,
        # including after a host modifies or replaces the configured database.
        # This is a best-effort preflight, not an atomic filesystem path lock.
        _existing_bound_world(str(self.db_path), self._world_id, self._token)
        # Reauthenticate each invocation so revocation takes effect without
        # a plugin-process restart.
        actor = self.gateway.credentials.authenticate(self._token)
        if actor is None:
            raise RealmMCPError("host player credential is invalid or revoked")
        if actor != self._bound_actor:
            raise RealmMCPError("host player identity changed; restart with an explicitly bound character")
        return actor

    def call(self, name: str, arguments: object) -> dict[str, object]:
        if name not in _TOOL_NAMES:
            raise RealmMCPError("unknown REALM tool")
        actor = self._actor()
        if name == "realm_status":
            _arguments(arguments, frozenset())
            return {"ok": True, "world_id": self.gateway.world_id, "player": actor}
        if name == "realm_join":
            _arguments(arguments, frozenset())
            return self.gateway.join(actor)
        if name == "realm_look":
            _arguments(arguments, frozenset())
            return self.gateway.room(actor)
        if name == "realm_inventory":
            _arguments(arguments, frozenset())
            return self.gateway.command(actor, "inventory", None)
        if name == "realm_move":
            args = _arguments(arguments, frozenset({"direction", "action_id"}),
                              frozenset({"direction", "action_id"}))
            direction = args["direction"]
            if type(direction) is not str or direction not in _LONG_DIRECTIONS:
                raise RealmMCPError("invalid movement direction")
            return self.gateway.command(actor, "go " + direction, _action_id(args["action_id"]))
        if name == "realm_map":
            _arguments(arguments, frozenset())
            return self.gateway.map(actor)
        if name == "realm_feed":
            args = _arguments(arguments, frozenset({"after"}))
            after = args.get("after", 0)
            if type(after) is not int or not 0 <= after <= 2**63 - 1:
                raise RealmMCPError("invalid bounded feed cursor")
            return self.gateway.feed(actor, after)
        if name == "realm_command":
            args = _arguments(arguments, frozenset({"command", "action_id"}),
                              frozenset({"command", "action_id"}))
            command = args["command"]
            if type(command) is not str or not command.strip() or len(command) > 512:
                raise RealmMCPError("invalid game command")
            if command.strip().split(maxsplit=1)[0].casefold() == "say":
                raise RealmMCPError("use explicit realm_say for durable in-world speech")
            return self.gateway.command(actor, command, _action_id(args["action_id"]))
        args = _arguments(arguments, frozenset({"message", "action_id"}),
                          frozenset({"message", "action_id"}))
        message = args["message"]
        if (type(message) is not str or not 1 <= len(message) <= 280
                or message != message.strip() or not all(ch.isprintable() for ch in message)):
            raise RealmMCPError("invalid room chat message")
        return self.gateway.command(actor, "say " + message, _action_id(args["action_id"]))


def _jsonrpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _jsonrpc_result(request_id: object, data: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": data}


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _nonfinite_json(value: str) -> object:
    raise ValueError("nonfinite JSON value")


def dispatch(message: object, bridge: RealmMCPBridge) -> dict[str, object] | None:
    if type(message) is not dict or message.get("jsonrpc") != "2.0" or type(message.get("method")) is not str:
        return _jsonrpc_error(None, -32600, "invalid JSON-RPC request")
    request_id = message.get("id")
    if "id" in message and (type(request_id) not in (int, str) and request_id is not None):
        return _jsonrpc_error(None, -32600, "invalid request identifier")
    if "id" not in message:
        # MCP notifications are one-way. Unknown notifications cannot cause
        # gameplay effects because no tool invocation is processed here.
        return None
    method = message["method"]
    params = message.get("params", {})
    if method == "initialize":
        if type(params) is not dict:
            return _jsonrpc_error(request_id, -32602, "invalid initialize parameters")
        version = params.get("protocolVersion", "2025-06-18")
        if type(version) is not str or version not in _SUPPORTED_PROTOCOLS:
            version = "2025-06-18"
        return _jsonrpc_result(request_id, {"protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "realm-local", "version": "0.1.0"}})
    if method == "ping":
        return _jsonrpc_result(request_id, {})
    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": TOOLS})
    if method != "tools/call":
        return _jsonrpc_error(request_id, -32601, "unknown MCP method")
    if (type(params) is not dict or type(params.get("name")) is not str
            or not set(params).issubset({"name", "arguments", "_meta"})):
        return _jsonrpc_error(request_id, -32602, "invalid tool call")
    try:
        response = bridge.call(params["name"], params.get("arguments", {}))
    except (RealmMCPError, MapUnavailable, ValueError, sqlite3.Error) as exc:
        # Never serialize exception causes or the environment/configuration:
        # authentication material must remain outside the tool transcript.
        safe = str(exc) if isinstance(exc, RealmMCPError) else "game action unavailable or rejected"
        return _jsonrpc_result(request_id, {"content": [{"type": "text", "text": safe}], "isError": True})
    if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
        return _jsonrpc_result(request_id, {"content": [{"type": "text", "text": "game result exceeds plugin limit"}],
                                            "isError": True})
    return _jsonrpc_result(request_id, {"content": [{"type": "text", "text": response.get("text", json.dumps(response, ensure_ascii=False))}],
                                         "structuredContent": response, "isError": False})


def serve_stdio(bridge: RealmMCPBridge, source: IO[bytes], destination: IO[str]) -> None:
    while True:
        raw = source.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        if len(raw) > MAX_LINE_BYTES:
            destination.write(json.dumps(_jsonrpc_error(None, -32700, "oversized JSON-RPC line")) + "\n")
            destination.flush()
            break  # Do not interpret an overflow tail as another request.
        try:
            obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_keys,
                             parse_constant=_nonfinite_json)
        except (UnicodeError, ValueError, RecursionError):
            result = _jsonrpc_error(None, -32700, "invalid JSON-RPC JSON")
        else:
            result = dispatch(obj, bridge)
        if result is not None:
            # Escape unpaired surrogate code points accepted by json.loads;
            # writing those directly to a strict UTF-8 stdout kills the MCP process.
            destination.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
            destination.flush()


def main() -> int:
    try:
        bridge = RealmMCPBridge(os.environ.get("REALM_DB_PATH", ""),
                                os.environ.get("REALM_WORLD_ID", ""),
                                os.environ.get("REALM_PLAYER_TOKEN", ""))
    except RealmMCPError:
        print("REALM MCP requires an existing bound world and valid host-configured player credential", file=sys.stderr)
        return 2
    serve_stdio(bridge, sys.stdin.buffer, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
