"""Read-only SQLite WAL-reset fix preflight for release qualification.

SQLite's 2026 WAL-reset race is fixed in 3.51.3, and backported to 3.50.7
and 3.44.6. A version pass is necessary but not sufficient for a production
world: application concurrency, restore, and integrity tests remain required.

Source: https://www.sqlite.org/wal.html#walresetbug
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing


def wal_reset_fix_present(version: tuple[int, ...]) -> bool:
    """Conservatively recognize only documented fixed release lines."""
    if len(version) < 3:
        return False
    major, minor, patch = version[:3]
    if (major, minor, patch) >= (3, 51, 3):
        return True
    if (major, minor) == (3, 50):
        return patch >= 7
    if (major, minor) == (3, 44):
        return patch >= 6
    return False


def main() -> int:
    with closing(sqlite3.connect(":memory:")) as db:
        source_id = db.execute("SELECT sqlite_source_id()").fetchone()[0]
    fixed = wal_reset_fix_present(sqlite3.sqlite_version_info)
    print(f"Loaded SQLite: {sqlite3.sqlite_version}")
    print(f"SQLite source ID: {source_id}")
    if fixed:
        print("WAL-reset version gate: PASS (application qualification still required)")
        return 0
    print("WAL-reset version gate: BLOCK for durable multi-connection WAL production")
    return 2


if __name__ == "__main__":
    sys.exit(main())
