"""Launch REALM's single authenticated, read-only MCP adapter locally.

The engine-side protocol is implemented in ``realm.mcp_server``. This small
launcher only resolves its separately installed source tree; it does not
implement tools, remember credentials, or access game state itself. The
plugin's authority is unconditionally restricted to status and look; the
full-game engine is tested separately, never exposed by this launcher.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


_PILOT_ALLOWED_TOOLS = "realm_status,realm_look"


def _source_root() -> Path:
    configured = os.environ.get("REALM_ROOT")
    if configured:
        source = Path(configured)
        if not source.is_absolute():
            raise ValueError("REALM_ROOT must be an absolute local checkout path")
        candidates = (source,)
    else:
        # Running from a source tree is convenient during development. A
        # plugin installed elsewhere must provide its own host-owned path.
        candidates = Path(__file__).resolve().parents
    for candidate in candidates:
        if (candidate / "src" / "realm" / "mcp_server.py").is_file():
            return candidate.resolve(strict=True)
    raise ValueError("REALM_ROOT must reference a REALM checkout containing the engine MCP adapter")


def main() -> int:
    try:
        source_root = _source_root()
    except (OSError, ValueError) as exc:
        print("REALM plugin setup failed: " + str(exc), file=sys.stderr)
        return 2
    # Pin the runtime scope before importing any engine code.
    os.environ["REALM_MCP_ALLOWED_TOOLS"] = _PILOT_ALLOWED_TOOLS
    sys.path.insert(0, str(source_root / "src"))
    from realm import mcp_server

    # PR51's earlier backend ignored REALM_MCP_ALLOWED_TOOLS altogether.
    # Refuse that source instead of silently advertising all nine tools.
    parse_allowlist = getattr(mcp_server, "_host_tool_allowlist", None)
    try:
        compatible = (callable(parse_allowlist)
                      and parse_allowlist(_PILOT_ALLOWED_TOOLS)
                      == frozenset({"realm_status", "realm_look"}))
    except (TypeError, ValueError):
        compatible = False
    if not compatible:
        print("REALM plugin requires an engine with host-enforced tool allowlist", file=sys.stderr)
        return 2

    # The engine enforces this on both tools/list and direct tools/call. Do not
    # trust an inherited host/user REALM_MCP_ALLOWED_TOOLS value: even a broad
    # parent environment must never convert this pilot into a gameplay writer.
    # Do not add a launcher full-mode override; future gameplay needs its own
    # separately reviewed permission boundary and explicit authorization.
    return mcp_server.main()


if __name__ == "__main__":
    raise SystemExit(main())
