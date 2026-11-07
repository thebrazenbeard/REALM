"""Source-only fixed-binding, two-operation REALM Play host entrypoint.

This is a candidate for a *host-owned game-only capability*, not a permission
boundary by itself. A host administrator must install a protected copy with
an immutable command, protected adjacent binding file, and an enforceable
tool-specific ACL. Never expose an unrestricted shell through this wrapper.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys

from workbridge_mcp52_driver import (
    DriverError,
    MAX_INPUT,
    _reject_nonfinite,
    _unique_json_keys,
    call,
    config_from_file,
    validate_request,
)


ONLY_ALLOWED = frozenset({"realm_status", "realm_look"})
# No CLI argument, environment variable, or game request may replace this path.
# The host owns this file and the directory containing the installed wrapper.
HOST_BINDING = Path(__file__).resolve().with_name("host_binding.json")


def _decode_request(encoded: str | None, stream) -> dict:
    if encoded is None:
        raw = stream.read(MAX_INPUT + 1)
    else:
        if (not encoded or len(encoded) > MAX_INPUT * 4 // 3 + 4
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                       for c in encoded)):
            raise DriverError("REQUEST_ENCODING_INVALID")
        try:
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                                   altchars=b"-_", validate=True)
        except (ValueError, base64.binascii.Error):
            raise DriverError("REQUEST_ENCODING_INVALID") from None
    if len(raw) > MAX_INPUT:
        raise DriverError("REQUEST_TOO_LARGE")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_keys,
                          parse_constant=_reject_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise DriverError("REQUEST_REJECTED") from None


def execute(request: object, *, binding: Path = HOST_BINDING) -> dict:
    # Reject every mutation and unqualified observation operation BEFORE
    # accessing the host-private world config or token.
    name, arguments = validate_request(request)
    if name not in ONLY_ALLOWED or arguments:
        raise DriverError("OPERATION_NOT_ALLOWED")
    config = config_from_file(binding)
    result = call(config, {"tool": name, "arguments": {}})
    data = result["game_data"]
    if name == "realm_status":
        if (data.get("world_id") != config["expected_world_id"]
                or data.get("player") != config["expected_actor"]):
            raise DriverError("BOUND_IDENTITY_MISMATCH")
        # Do not forward opaque or future backend fields (including global
        # event cursors); status is a strictly projected host identity read.
        result["game_data"] = {"ok": True, "world_id": config["expected_world_id"],
                               "player": config["expected_actor"]}
        result["game_text"] = (f"World: {config['expected_world_id']}\n"
                               f"Player: {config['expected_actor']}")
    else:
        text = data.get("text")
        if type(text) is not str:
            raise DriverError("MCP_RESULT_INVALID")
        # The engine's room response includes a global events cursor which
        # exposes activity beyond this actor's visible room. The plugin does
        # not need it. Preserve the room prose, not its global cursor.
        result["game_data"] = {"ok": True, "text": text}
        result["game_text"] = text
    return result


def main(argv: list[str] | None = None, *, stream=None) -> int:
    parser = argparse.ArgumentParser(description="Host-pinned REALM status/look only")
    parser.add_argument("--request-b64", help="base64url-encoded game-only request")
    ns = parser.parse_args(argv)
    try:
        request = _decode_request(ns.request_b64,
                                  sys.stdin.buffer if stream is None else stream)
        print(json.dumps(execute(request), ensure_ascii=False))
        return 0
    except (DriverError, OSError, UnicodeError, ValueError, TypeError):
        exc = sys.exc_info()[1]
        print(json.dumps({"ok": False, "error_code": exc.code
                          if isinstance(exc, DriverError) else "REQUEST_REJECTED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
