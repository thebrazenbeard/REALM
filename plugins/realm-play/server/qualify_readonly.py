"""Read-only, fail-closed host qualification for the REALM Play MCP package.

This is an operator smoke check, not a web bridge or an installed MCP tool.
It never joins a world, sends game commands, or prints subprocess transcripts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


LAUNCHER = Path(__file__).with_name("realm_mcp.py")
REQUIRED = ("REALM_ROOT", "REALM_DB_PATH", "REALM_WORLD_ID",
            "REALM_PLAYER_TOKEN", "REALM_EXPECT_PLAYER")
MAX_OUTPUT = 131_072


class QualificationError(RuntimeError):
    """A deliberately non-sensitive qualification failure code."""


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> object:
    raise ValueError("nonfinite response value")


def _request(request_id: int, method: str, params: dict | None = None) -> dict:
    result = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        result["params"] = params
    return result


def _validated_response(stdout: bytes, expected_world: str, expected_player: str) -> dict:
    if len(stdout) > MAX_OUTPUT:
        raise QualificationError("MCP_RESPONSE_INVALID")
    try:
        replies = [json.loads(line, object_pairs_hook=_unique_keys,
                              parse_constant=_reject_nonfinite)
                   for line in stdout.decode("utf-8").splitlines()]
    except (UnicodeError, ValueError, RecursionError):
        raise QualificationError("MCP_RESPONSE_INVALID") from None
    if (len(replies) != 3 or any(type(r) is not dict for r in replies)
            or [r.get("id") for r in replies] != [1, 2, 3]
            or any(r.get("jsonrpc") != "2.0" or "error" in r for r in replies)):
        raise QualificationError("MCP_RESPONSE_INVALID")

    initialization = replies[0].get("result")
    server_info = initialization.get("serverInfo") if type(initialization) is dict else None
    if type(server_info) is not dict or server_info.get("name") != "realm-local":
        raise QualificationError("MCP_IDENTITY_INVALID")

    status = replies[1].get("result")
    look = replies[2].get("result")
    if (type(status) is not dict or status.get("isError") is not False
            or type(status.get("structuredContent")) is not dict):
        raise QualificationError("STATUS_UNAVAILABLE")
    bound = status["structuredContent"]
    if (bound.get("ok") is not True or bound.get("world_id") != expected_world
            or bound.get("player") != expected_player):
        raise QualificationError("BOUND_IDENTITY_MISMATCH")
    if (type(look) is not dict or look.get("isError") is not False
            or type(look.get("structuredContent")) is not dict):
        raise QualificationError("LOOK_UNAVAILABLE")
    room = look["structuredContent"]
    if (room.get("ok") is not True or type(room.get("text")) is not str
            or not room["text"].strip()):
        raise QualificationError("LOOK_UNAVAILABLE")

    # Room descriptions and cursors may be sensitive or untrusted; only the
    # identity verified by the backend leaves this operator-only process.
    return {"ok": True, "world_id": expected_world, "player": expected_player}


def qualify(env: dict[str, str]) -> dict:
    if any(type(env.get(key)) is not str or not env[key].strip() for key in REQUIRED):
        raise QualificationError("HOST_BINDING_MISSING")
    if not LAUNCHER.is_file():
        raise QualificationError("MCP_LAUNCHER_MISSING")

    # Exactly three read-only protocol operations, all executed in one fresh
    # stdio child. Never send realm_join, realm_feed, realm_map, or mutations.
    messages = (
        _request(1, "initialize", {"protocolVersion": "2025-06-18",
                                   "capabilities": {},
                                   "clientInfo": {"name": "realm-readonly-qualifier", "version": "1.0.0"}}),
        _request(2, "tools/call", {"name": "realm_status", "arguments": {}}),
        _request(3, "tools/call", {"name": "realm_look", "arguments": {}}),
    )
    payload = "".join(json.dumps(m, separators=(",", ":")) + "\n" for m in messages)
    child_env = os.environ.copy()
    for key in REQUIRED:
        child_env.pop(key, None)
    child_env.update({key: env[key] for key in REQUIRED if key != "REALM_EXPECT_PLAYER"})
    # Eliminate a parent PYTHONPATH override to keep the engine import rooted
    # in the host-selected REALM_ROOT resolved by the plugin launcher.
    child_env.pop("PYTHONPATH", None)
    # Avoid propagating a parent-requested broad engine scope. The plugin
    # launcher independently pins its two-tool scope on every invocation.
    child_env.pop("REALM_MCP_ALLOWED_TOOLS", None)
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(LAUNCHER)],
            input=payload.encode("utf-8"), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=child_env, check=False, timeout=20,
            cwd=str(LAUNCHER.parent.parent),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise QualificationError("MCP_PROCESS_UNAVAILABLE") from None
    finally:
        child_env.pop("REALM_PLAYER_TOKEN", None)
    if result.returncode != 0:
        raise QualificationError("MCP_PROCESS_UNAVAILABLE")
    return _validated_response(result.stdout, env["REALM_WORLD_ID"],
                               env["REALM_EXPECT_PLAYER"])


def main() -> int:
    try:
        response = qualify(os.environ)
    except QualificationError as exc:
        print(json.dumps({"ok": False, "error_code": str(exc)}))
        return 2
    print(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
