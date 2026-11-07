"""WorkBridge-callable, host-only transport for REALM's existing stdio MCP.

This bridge does not implement REALM commands or start a persistent service.
It launches the exact locally installed MCP server for *one* operation and
returns a JSON envelope. No admin actions, network endpoints or model calls.

Run with ``python -B workbridge_mcp_driver.py --config HOST_PRIVATE_JSON``;
pass exactly one JSON request on stdin. The config is host-owned and contains
paths and the existing actor identity, never any bearer credential.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


ALLOWED = frozenset({"realm_look", "realm_inventory", "realm_move", "realm_say"})
MUTATING = frozenset({"realm_move", "realm_say"})
WORLD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
MAX_CONFIG_BYTES = 8192
MAX_INPUT_BYTES = 8192
MAX_OUTPUT_BYTES = 131072


class BridgeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _object(path: Path) -> dict:
    if not path.is_absolute() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise BridgeError("HOST_CONFIG_INVALID")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        raise BridgeError("HOST_CONFIG_INVALID") from None
    if type(value) is not dict or set(value) != {
        "schema", "python_executable", "realm_root", "server_script",
        "world_db", "expected_world_id", "actor"
    } or value["schema"] != "realm.workbridge-mcp-transport.v1":
        raise BridgeError("HOST_CONFIG_INVALID")
    for key in ("python_executable", "realm_root", "server_script", "world_db"):
        if type(value[key]) is not str or not Path(value[key]).is_absolute():
            raise BridgeError("HOST_CONFIG_INVALID")
    if (type(value["expected_world_id"]) is not str
            or not WORLD_RE.fullmatch(value["expected_world_id"])
            or type(value["actor"]) is not str or not 1 <= len(value["actor"]) <= 24):
        raise BridgeError("HOST_CONFIG_INVALID")
    if not Path(value["server_script"]).is_file():
        raise BridgeError("MCP_SERVER_NOT_INSTALLED")
    if not Path(value["realm_root"]).joinpath("src", "realm", "engine.py").is_file():
        raise BridgeError("REALM_SOURCE_UNAVAILABLE")
    return value


def _world_binding(path: Path, expected: str, actor: str) -> None:
    """Read-only preflight; binding is not atomic with later MCP tool commit."""
    if not path.is_file() or path.is_symlink():
        raise BridgeError("WORLD_DATABASE_NOT_FOUND")
    try:
        with sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=3) as db:
            actual = db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()
            player = db.execute("SELECT id FROM players WHERE id=?", (actor,)).fetchone()
    except (sqlite3.Error, OSError):
        raise BridgeError("WORLD_PREFLIGHT_FAILED") from None
    if actual is None or actual[0] != expected:
        raise BridgeError("WORLD_IDENTITY_MISMATCH")
    if player is None or player[0] != actor:
        raise BridgeError("ACTOR_NOT_JOINED")


def _request(method: str, request_id: int, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method,
            **({"params": params} if params is not None else {})}


def _execute(config: dict, name: str, arguments: dict, *, timeout: int = 10) -> dict:
    env = os.environ.copy()
    env.update({"REALM_ROOT": config["realm_root"], "REALM_DB": config["world_db"],
                "REALM_ACTOR": config["actor"]})
    transcript = [
        _request("initialize", 101, {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "realm-workbridge-driver", "version": "0.1.0"}}),
        _request("tools/list", 102),
        _request("tools/call", 103, {"name": name, "arguments": arguments}),
    ]
    data = ("\n".join(json.dumps(row, separators=(",", ":"), ensure_ascii=True)
                      for row in transcript) + "\n").encode("utf-8")
    try:
        result = subprocess.run(
            [config["python_executable"], "-B", config["server_script"]],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=config["realm_root"], env=env, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        # The game command might already have committed; never auto-retry.
        raise BridgeError("MCP_OUTCOME_INDETERMINATE") from None
    except OSError:
        raise BridgeError("MCP_LAUNCH_FAILED") from None
    if result.returncode != 0:
        # The tool call may already have committed before the subprocess died.
        raise BridgeError("MCP_OUTCOME_INDETERMINATE")
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        raise BridgeError("MCP_OUTPUT_TOO_LARGE")
    try:
        replies = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    except (UnicodeError, ValueError):
        raise BridgeError("MCP_OUTCOME_INDETERMINATE") from None
    if (len(replies) != 3 or not all(type(row) is dict for row in replies)
            or [row.get("id") for row in replies] != [101, 102, 103]):
        raise BridgeError("MCP_OUTCOME_INDETERMINATE")
    if (replies[0].get("result", {}).get("serverInfo", {}).get("name") != "realm-play-local"
            or "error" in replies[0]):
        raise BridgeError("MCP_IDENTITY_MISMATCH")
    tools = replies[1].get("result", {}).get("tools")
    if type(tools) is not list or name not in {item.get("name") for item in tools if type(item) is dict}:
        raise BridgeError("MCP_TOOL_MISSING")
    value = replies[2].get("result")
    if type(value) is not dict or value.get("isError") is not False:
        raise BridgeError("MCP_ACTION_REJECTED")
    blocks = value.get("content")
    if (type(blocks) is not list or len(blocks) != 1 or type(blocks[0]) is not dict
            or blocks[0].get("type") != "text" or type(blocks[0].get("text")) is not str):
        raise BridgeError("MCP_RESULT_INVALID")
    return {"ok": True, "tool": name, "game_text": blocks[0]["text"],
            "game_text_trust": "UNTRUSTED_IN_WORLD_CONTENT",
            "evidence_class": "LOCAL_MCP_STDIO_RESPONSE_UNATTESTED",
            "mutating": name in MUTATING,
            "server_replay_contract": "NOT_STABLE_ACTION_ID_QUALIFIED"}


def invoke(config: dict, request: dict) -> dict:
    if type(request) is not dict or set(request) != {"tool", "arguments"}:
        raise BridgeError("REQUEST_SCHEMA_INVALID")
    name, args = request["tool"], request["arguments"]
    if type(name) is not str or name not in ALLOWED or type(args) is not dict:
        raise BridgeError("REQUEST_SCHEMA_INVALID")
    if name in {"realm_look", "realm_inventory"} and args:
        raise BridgeError("REQUEST_SCHEMA_INVALID")
    if name == "realm_move" and (set(args) != {"direction"} or type(args["direction"]) is not str or
                                 args["direction"] not in {"north", "south", "east", "west", "up", "down"}):
        raise BridgeError("REQUEST_SCHEMA_INVALID")
    if name == "realm_say" and (set(args) != {"text"} or type(args["text"]) is not str or
                                not 1 <= len(args["text"]) <= 280 or
                                not all(c.isprintable() for c in args["text"])):
        raise BridgeError("REQUEST_SCHEMA_INVALID")
    _world_binding(Path(config["world_db"]), config["expected_world_id"], config["actor"])
    return _execute(config, name, args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private REALM MCP transport via WorkBridge")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request-b64", help="Base64url encoded JSON request for one-shot WorkBridge calls")
    args = parser.parse_args(argv)
    try:
        config = _object(args.config)
        if args.request_b64 is None:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        else:
            encoded = args.request_b64
            if (not encoded or len(encoded) > (MAX_INPUT_BYTES * 4 // 3 + 4)
                    or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in encoded)):
                raise BridgeError("REQUEST_ENCODING_INVALID")
            try:
                raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                                       altchars=b"-_", validate=True)
            except (ValueError, base64.binascii.Error):
                raise BridgeError("REQUEST_ENCODING_INVALID") from None
        if len(raw) > MAX_INPUT_BYTES:
            raise BridgeError("REQUEST_TOO_LARGE")
        request = json.loads(raw.decode("utf-8"))
        print(json.dumps(invoke(config, request), ensure_ascii=False))
        return 0
    except (BridgeError, ValueError, UnicodeError, OSError, TypeError):
        code = (sys.exc_info()[1].code if isinstance(sys.exc_info()[1], BridgeError)
                else "REQUEST_REJECTED")
        print(json.dumps({"ok": False, "error_code": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
