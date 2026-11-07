"""Launch REALM's single authenticated MCP adapter from a local plugin host.

The engine-side protocol is implemented in ``realm.mcp_server``. This small
launcher only resolves its separately installed source tree; it does not
implement tools, remember credentials, or access game state itself.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


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
    sys.path.insert(0, str(source_root / "src"))
    from realm.mcp_server import main as run_engine_mcp

    return run_engine_mcp()


if __name__ == "__main__":
    raise SystemExit(main())
