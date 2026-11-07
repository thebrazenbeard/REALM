"""Pinned, read-only REALM launcher candidate for a host-operated WorkBridge call.

No user-supplied config path, RPC name, arguments, interpreter or command.
Requires a separately installed, privately permissioned host config at the
fixed path below. Does NOT restrict WorkBridge's other process-exec methods.

This file is source/test material, not an installed Lappy launcher.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "private" / "realm-host-config.json"
DRIVER = ROOT / "workbridge_mcp52_driver.py"
# Current PR #53's driver bytes, so source drift requires explicit re-review.
DRIVER_SHA256 = "2b07ad05990e5592ade33d44af0dd3a9bbc477bdc4359f09f57b61e5d5cb9eac"
ALLOWED = frozenset({"realm_status", "realm_look"})
MAX_OUTPUT = 135_000
TIMEOUT_SECONDS = 18


def _minimal_environment() -> dict[str, str]:
    """Give the driver a minimal conventional runtime, no ambient API tokens."""
    names = ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR",
             "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL", "PYTHONUTF8")
    return {name: os.environ[name] for name in names if name in os.environ}


def run_readonly(tool: object, *, config: Path = CONFIG, driver: Path = DRIVER) -> dict:
    """Return one sanitized driver result; never execute model-authored commands."""
    if type(tool) is not str or tool not in ALLOWED:
        return {"ok": False, "error_code": "READONLY_TOOL_DENIED"}

    # Injection overrides here exist only for offline tests. The CLI calls
    # with baked-in constants and has no input mechanism for these paths.
    if (not config.is_absolute() or config.is_symlink() or config.parent.is_symlink()
            or not config.is_file() or not driver.is_absolute()
            or driver.is_symlink() or not driver.is_file()):
        return {"ok": False, "error_code": "HOST_WRAPPER_NOT_QUALIFIED"}
    try:
        if (config.stat().st_size > 8192
                or hashlib.sha256(driver.read_bytes()).hexdigest() != DRIVER_SHA256):
            return {"ok": False, "error_code": "HOST_WRAPPER_NOT_QUALIFIED"}
        host_data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {"ok": False, "error_code": "HOST_WRAPPER_NOT_QUALIFIED"}
    if type(host_data) is not dict:
        return {"ok": False, "error_code": "HOST_WRAPPER_NOT_QUALIFIED"}

    request = json.dumps({"tool": tool, "arguments": {}}, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(request).decode("ascii").rstrip("=")
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(driver), "--config", str(config),
             "--request-b64=" + encoded],
            input=b"", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_minimal_environment(), cwd=str(driver.parent), timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "error_code": "MCP_READ_OUTCOME_UNKNOWN"}
    if proc.returncode != 0 or len(proc.stdout) > MAX_OUTPUT:
        return {"ok": False, "error_code": "MCP_READ_REJECTED"}
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeError):
        return {"ok": False, "error_code": "MCP_RESPONSE_INVALID"}
    if (type(data) is not dict or data.get("ok") is not True
            or data.get("tool") != tool or data.get("mutating") is not False
            or type(data.get("game_data")) is not dict
            or data["game_data"].get("ok") is not True):
        return {"ok": False, "error_code": "MCP_RESPONSE_INVALID"}
    if tool == "realm_status" and (data["game_data"].get("world_id") != host_data.get("expected_world_id")
                                    or data["game_data"].get("player") != host_data.get("expected_actor")):
        return {"ok": False, "error_code": "MCP_RESPONSE_INVALID"}
    return data


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        response = {"ok": False, "error_code": "READONLY_TOOL_DENIED"}
    else:
        response = run_readonly(args[0])
    print(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 0 if response.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
