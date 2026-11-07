"""One-shot WorkBridge transport for REALM PR #52's token-bound local MCP.

Experimental source only. This is not a plugin, install script, or service.
The host pins the private config path and must protect the separate token file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


WORLD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
ACTOR_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,23}\Z")
ACTION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ALLOWED = frozenset({"realm_status", "realm_join", "realm_look", "realm_inventory",
                     "realm_move", "realm_command", "realm_say", "realm_feed", "realm_map"})
MUTATING = frozenset({"realm_join", "realm_move", "realm_command", "realm_say"})
DIRECTIONS = frozenset({"north", "south", "east", "west", "up", "down"})
MAX_INPUT = 8192
MAX_OUTPUT = 131072


class DriverError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> object:
    raise ValueError("nonfinite JSON value")


def config_from_file(path: Path) -> dict:
    if not path.is_absolute() or path.is_symlink():
        raise DriverError("HOST_CONFIG_INVALID")
    try:
        if path.stat().st_size > 8192:
            raise DriverError("HOST_CONFIG_INVALID")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise DriverError("HOST_CONFIG_INVALID") from None
    if type(data) is not dict or set(data) != {
        "schema", "python_executable", "realm_root", "world_db",
        "expected_world_id", "expected_actor", "token_file",
    } or data["schema"] != "realm.workbridge-mcp52.v1":
        raise DriverError("HOST_CONFIG_INVALID")
    for key in ("python_executable", "realm_root", "world_db", "token_file"):
        if type(data[key]) is not str or not Path(data[key]).is_absolute():
            raise DriverError("HOST_CONFIG_INVALID")
    if (type(data["expected_world_id"]) is not str
            or not WORLD_RE.fullmatch(data["expected_world_id"])
            or type(data["expected_actor"]) is not str
            or not ACTOR_RE.fullmatch(data["expected_actor"])):
        raise DriverError("HOST_CONFIG_INVALID")
    if not (Path(data["realm_root"]) / "src" / "realm" / "mcp_server.py").is_file():
        raise DriverError("MCP_SERVER_NOT_INSTALLED")
    return data


def token_from_file(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise DriverError("TOKEN_FILE_INVALID")
    try:
        if not path.is_file() or path.stat().st_size > 512:
            raise DriverError("TOKEN_FILE_INVALID")
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise DriverError("TOKEN_FILE_INVALID") from None
    if not 16 <= len(token) <= 255 or not token.isascii() or not token.isprintable():
        raise DriverError("TOKEN_FILE_INVALID")
    return token


def preflight(config: dict, token: str) -> None:
    """Read-only early check. NOT ATOMIC with backend command transactions."""
    path = Path(config["world_db"])
    if not path.is_file() or path.is_symlink():
        raise DriverError("WORLD_DATABASE_NOT_FOUND")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    try:
        with sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            row = db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()
            actor = db.execute("SELECT player_name FROM player_credentials WHERE token_digest=?",
                               (digest,)).fetchone()
    except (sqlite3.Error, OSError):
        raise DriverError("WORLD_PREFLIGHT_FAILED") from None
    if row is None or row[0] != config["expected_world_id"]:
        raise DriverError("WORLD_IDENTITY_MISMATCH")
    if actor is None or actor[0] != config["expected_actor"]:
        raise DriverError("ACTOR_TOKEN_MISMATCH")


def validate_request(request: object) -> tuple[str, dict]:
    if type(request) is not dict or set(request) != {"tool", "arguments"}:
        raise DriverError("REQUEST_SCHEMA_INVALID")
    name, args = request["tool"], request["arguments"]
    if type(name) is not str or name not in ALLOWED or type(args) is not dict:
        raise DriverError("REQUEST_SCHEMA_INVALID")
    if name in {"realm_status", "realm_join", "realm_look", "realm_inventory", "realm_map"}:
        if args:
            raise DriverError("REQUEST_SCHEMA_INVALID")
    elif name == "realm_feed":
        if set(args) not in (set(), {"after"}) or ("after" in args and
                (type(args["after"]) is not int or not 0 <= args["after"] <= 2**63-1)):
            raise DriverError("REQUEST_SCHEMA_INVALID")
    elif name == "realm_move":
        if (set(args) != {"direction", "action_id"} or
                type(args["direction"]) is not str or args["direction"] not in DIRECTIONS or
                type(args["action_id"]) is not str or ACTION_RE.fullmatch(args["action_id"]) is None):
            raise DriverError("REQUEST_SCHEMA_INVALID")
    elif name in {"realm_command", "realm_say"}:
        text_key = "command" if name == "realm_command" else "message"
        limit = 512 if name == "realm_command" else 280
        if (set(args) != {text_key, "action_id"}
                or type(args[text_key]) is not str
                or not args[text_key].strip() or len(args[text_key]) > limit
                or (name == "realm_say" and (args[text_key] != args[text_key].strip()
                    or not all(c.isprintable() for c in args[text_key])))
                or (name == "realm_command" and
                    args[text_key].strip().split(maxsplit=1)[0].casefold() == "say")
                or type(args["action_id"]) is not str
                or ACTION_RE.fullmatch(args["action_id"]) is None):
            raise DriverError("REQUEST_SCHEMA_INVALID")
    return name, args


def _rpc(method: str, request_id: int, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method,
            **({"params": params} if params is not None else {})}


def call(config: dict, request: object, *, timeout: int = 12) -> dict:
    name, args = validate_request(request)
    token = token_from_file(Path(config["token_file"]))
    preflight(config, token)
    env = os.environ.copy()
    env.update({"REALM_DB_PATH": config["world_db"],
                "REALM_WORLD_ID": config["expected_world_id"],
                "REALM_PLAYER_TOKEN": token,
                "PYTHONPATH": str(Path(config["realm_root"]) / "src")})
    lines = [
        _rpc("initialize", 101, {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "realm-workbridge", "version": "0.1.0"}}),
        _rpc("tools/list", 102),
        _rpc("tools/call", 103, {"name": name, "arguments": args}),
    ]
    payload = ("\n".join(json.dumps(item, ensure_ascii=True, separators=(",", ":"))
                         for item in lines) + "\n").encode("utf-8")
    try:
        completed = subprocess.run(
            [config["python_executable"], "-B", "-m", "realm.mcp_server"],
            cwd=config["realm_root"], env=env, input=payload,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DriverError("MCP_OUTCOME_INDETERMINATE") from None
    finally:
        # Token is intentionally never logged or returned in CLI output.
        env.pop("REALM_PLAYER_TOKEN", None)
    if completed.returncode != 0 or len(completed.stdout) > MAX_OUTPUT:
        raise DriverError("MCP_OUTCOME_INDETERMINATE")
    try:
        replies = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines()]
    except (UnicodeError, ValueError):
        raise DriverError("MCP_OUTCOME_INDETERMINATE") from None
    if (len(replies) != 3 or not all(type(row) is dict for row in replies)
            or [row.get("id") for row in replies] != [101, 102, 103]):
        raise DriverError("MCP_OUTCOME_INDETERMINATE")
    if any("error" in row for row in replies):
        # A JSON-RPC message containing both result and error is ambiguous.
        # The game action might already have committed; do not retry it.
        raise DriverError("MCP_OUTCOME_INDETERMINATE")
    initialize_result = replies[0].get("result")
    info = initialize_result.get("serverInfo") if type(initialize_result) is dict else None
    if (type(info) is not dict or info.get("name") != "realm-local"
            or "error" in replies[0]):
        raise DriverError("MCP_IDENTITY_MISMATCH")
    discovered = replies[1].get("result")
    tools = discovered.get("tools") if type(discovered) is dict else None
    if (type(tools) is not list or name not in
            {t.get("name") for t in tools if type(t) is dict}):
        raise DriverError("MCP_TOOL_MISSING")
    result = replies[2].get("result")
    if type(result) is not dict or result.get("isError") is not False:
        raise DriverError("MCP_ACTION_REJECTED")
    blocks = result.get("content")
    if (type(blocks) is not list or len(blocks) != 1
            or type(blocks[0]) is not dict or blocks[0].get("type") != "text"
            or type(blocks[0].get("text")) is not str):
        raise DriverError("MCP_RESULT_INVALID")
    return {"ok": True, "tool": name, "game_text": blocks[0]["text"],
            "game_text_trust": "UNTRUSTED_IN_WORLD_CONTENT",
            "evidence_class": "LOCAL_MCP_STDIO_RESPONSE_UNATTESTED",
            "mutating": name in MUTATING,
            "replay_policy": "CALLER_REUSES_STABLE_ACTION_ID_NO_AUTOMATIC_RETRY"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Source-only REALM PR52 WorkBridge transport")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request-b64", help="Base64url JSON; pass as --request-b64=<encoded>")
    ns = parser.parse_args(argv)
    try:
        config = config_from_file(ns.config)
        if ns.request_b64 is None:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        else:
            value = ns.request_b64
            if (not value or len(value) > (MAX_INPUT * 4 // 3 + 4) or
                    any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                        for c in value)):
                raise DriverError("REQUEST_ENCODING_INVALID")
            try:
                raw = base64.b64decode(value + "=" * (-len(value) % 4),
                                       altchars=b"-_", validate=True)
            except (ValueError, base64.binascii.Error):
                raise DriverError("REQUEST_ENCODING_INVALID") from None
        if len(raw) > MAX_INPUT:
            raise DriverError("REQUEST_TOO_LARGE")
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_keys,
                             parse_constant=_reject_nonfinite)
        print(json.dumps(call(config, request), ensure_ascii=False))
        return 0
    except (DriverError, OSError, UnicodeError, ValueError, TypeError):
        e = sys.exc_info()[1]
        print(json.dumps({"ok": False, "error_code": e.code if isinstance(e, DriverError)
                          else "REQUEST_REJECTED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
