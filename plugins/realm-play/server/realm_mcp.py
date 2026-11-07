"""Local-only, dependency-free REALM MCP stdio game tools.

This process is launched by a trusted *local* MCP host. It binds to one
already-joined character and one existing SQLite world selected by its
environment, not by model-supplied tool arguments. There is no remote server,
credential exchange, model inference, or background gameplay scheduler.

Wire format: one JSON-RPC object per UTF-8 line on stdin/stdout. Stdout is
reserved for protocol messages; diagnostics go to stderr only.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import sys
from pathlib import Path


_DIRECTIONS = frozenset({"north", "south", "east", "west", "up", "down"})


def _realm_root() -> Path:
    # An installed plugin may be cached outside the REALM source checkout.
    # An explicit local path is then required; no network discovery occurs.
    configured = os.environ.get("REALM_ROOT")
    if configured:
        candidate = Path(configured)
        if (not candidate.is_absolute()
                or not (candidate / "src" / "realm" / "engine.py").is_file()
                or not (candidate / "src" / "realm" / "residents.py").is_file()):
            raise RuntimeError("REALM_ROOT must name a local REALM source checkout")
        return candidate
    # In-tree source works without additional configuration.
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "realm" / "engine.py").is_file():
            return parent
    raise RuntimeError("Set REALM_ROOT to your local REALM source checkout")


try:
    sys.path.insert(0, str(_realm_root() / "src"))
except RuntimeError as exc:
    print("REALM plugin setup failed: " + str(exc), file=sys.stderr)
    raise SystemExit(2) from None
from realm.engine import Engine  # noqa: E402
from realm.residents import ResidentController  # noqa: E402


class RealmGameTools:
    """A fixed actor/world adapter; tools never accept these as arguments."""

    def __init__(self, database: str, actor: str):
        if not isinstance(database, str) or not database:
            raise ValueError("REALM_DB must name an existing local world")
        path = Path(database)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError("REALM_DB must be an existing regular absolute path")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("REALM_DB must be a regular file")
        if not isinstance(actor, str) or not actor or len(actor) > 24:
            raise ValueError("REALM_ACTOR must be one existing character")
        # Read-only qualification BEFORE Engine construction, since the Engine
        # creates demo schema on a fresh path. Never create/join an actor here.
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            try:
                row = db.execute("SELECT id FROM players WHERE id=?", (actor,)).fetchone()
            except sqlite3.DatabaseError as exc:
                raise ValueError("REALM_DB is not an initialized world") from exc
        if row is None or row[0] != actor:
            raise ValueError("REALM_ACTOR must already be joined to this world")
        self._engine = Engine(path)
        self._actor = actor
        self._resident = ResidentController(self._engine, actor)

    def call(self, name: str, arguments: object) -> str:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        if name in {"realm_look", "realm_inventory"}:
            if arguments:
                raise ValueError("This tool takes no arguments")
            return self._engine.act(self._actor, "look" if name == "realm_look" else "inventory")
        if name == "realm_move":
            if set(arguments) != {"direction"}:
                raise ValueError("Only direction is accepted")
            direction = arguments["direction"]
            if not isinstance(direction, str) or direction not in _DIRECTIONS:
                raise ValueError("Direction must be north, south, east, west, up or down")
            return self._resident.commit({"command": "go " + direction},
                                         action_id="mcp-" + secrets.token_hex(16))
        if name == "realm_say":
            if set(arguments) != {"text"}:
                raise ValueError("Only text is accepted")
            text = arguments["text"]
            if (not isinstance(text, str) or not 1 <= len(text) <= 280
                    or text != text.strip() or not all(ch.isprintable() for ch in text)):
                raise ValueError("Speech must be 1-280 printable characters")
            return self._resident.commit({"command": "say " + text},
                                         action_id="mcp-" + secrets.token_hex(16))
        raise ValueError("Unknown REALM tool")


_TOOLS = [
    {"name": "realm_look", "description": "Describe your current REALM room and visible exits/items.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "realm_inventory", "description": "Inspect the items carried by the bound character.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "realm_move", "description": "Move the bound character through a REALM exit.",
     "inputSchema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": sorted(_DIRECTIONS)}},
         "required": ["direction"], "additionalProperties": False}},
    {"name": "realm_say", "description": "Speak as the bound character to people in the room.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "minLength": 1, "maxLength": 280}},
         "required": ["text"], "additionalProperties": False}},
]


def _result(request_id: object, value: object) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def serve(tools: RealmGameTools) -> None:
    for line in sys.stdin:
        response = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("Expected JSON-RPC 2.0 object")
            request_id = request.get("id")
            method = request.get("method")
            # Notifications have no JSON-RPC response.
            if "id" not in request:
                continue
            if method == "initialize":
                response = _result(request_id, {
                    "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "realm-play-local", "version": "0.1.0"}})
            elif method == "ping":
                response = _result(request_id, {})
            elif method == "tools/list":
                response = _result(request_id, {"tools": _TOOLS})
            elif method == "tools/call":
                params = request.get("params", {})
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    response = _error(request_id, -32602, "Invalid tool call")
                else:
                    try:
                        message = tools.call(params["name"], params.get("arguments", {}))
                        response = _result(request_id, {"content": [{"type": "text", "text": message}],
                                                        "isError": False})
                    except (ValueError, sqlite3.Error) as exc:
                        # Don't disclose paths/SQL details through tool errors.
                        message = str(exc) if isinstance(exc, ValueError) else "REALM action failed"
                        response = _result(request_id, {"content": [{"type": "text", "text": message}],
                                                        "isError": True})
            else:
                response = _error(request_id, -32601, "Method not found")
        except (json.JSONDecodeError, ValueError):
            response = _error(None, -32700, "Invalid JSON-RPC request")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def main() -> int:
    try:
        tools = RealmGameTools(os.environ.get("REALM_DB", ""), os.environ.get("REALM_ACTOR", ""))
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print("REALM plugin setup failed: " + str(exc), file=sys.stderr)
        return 2
    serve(tools)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
