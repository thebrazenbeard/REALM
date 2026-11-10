"""No-write static prerequisite checks for a deliberately local REALM host.

This is not a launcher, installer, network reachability check, database repair,
or evidence that remote gameplay has been qualified.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .engine import _pristine_schema, _schema_snapshot


@dataclass(frozen=True)
class Finding:
    severity: str  # fatal | warning
    code: str
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    findings: tuple[Finding, ...]

    @property
    def ready(self) -> bool:
        """Only static prerequisites without a known fatal finding."""
        return all(item.severity != "fatal" for item in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "mode": "local-only-read-only",
            "findings": [
                {"severity": f.severity, "code": f.code, "detail": f.detail}
                for f in self.findings
            ],
        }


def preflight_local_host(
    db_path: str | Path, *, http_host: str = "127.0.0.1",
    telnet_host: str = "127.0.0.1", expected_world_id: str | None = None,
) -> PreflightReport:
    """Inspect a *static* one-world host configuration with zero DB writes.

    An existing SQLite file is read with ``immutable=1`` to avoid SQLite
    sidecars and exclusive writer transitions. WAL, SHM and rollback-journal
    sidecars make that inspection potentially stale, so it is unverified.
    """
    findings: list[Finding] = []

    def add(severity: str, code: str, detail: str) -> None:
        findings.append(Finding(severity, code, detail))

    for label, host in (("http", http_host), ("telnet", telnet_host)):
        if host != "127.0.0.1":
            add("fatal", f"{label}_nonloopback", "Local listeners must use exactly 127.0.0.1")
    if sys.version_info < (3, 11):
        add("fatal", "python_unsupported", "REALM requires Python 3.11 or newer")
    if sqlite3.sqlite_version_info < (3, 35):
        add("fatal", "sqlite_unsupported", "REALM requires SQLite 3.35 or newer")
    if not isinstance(db_path, (str, Path)) or not str(db_path):
        add("fatal", "db_path_invalid", "A persistent SQLite file path is required")
        return PreflightReport(tuple(findings))
    if expected_world_id is not None and (
        not isinstance(expected_world_id, str) or not expected_world_id
    ):
        add("fatal", "world_id_invalid", "Expected world identity must be nonempty")
        return PreflightReport(tuple(findings))

    path = Path(db_path)
    if str(path) == ":memory:":
        add("fatal", "db_path_invalid", "An in-memory database is not persistent")
        return PreflightReport(tuple(findings))
    try:
        if path.is_symlink() and not path.exists():
            add("fatal", "db_symlink_broken", "Database path is a broken symbolic link")
            return PreflightReport(tuple(findings))
        resolved = path.resolve(strict=False)
        if not resolved.parent.is_dir():
            add("fatal", "db_parent_missing", "Database parent directory does not exist")
            return PreflightReport(tuple(findings))
        if not os.access(resolved.parent, os.W_OK | os.X_OK):
            add("fatal", "db_parent_not_writable", "Database parent does not advertise write access")
        if path.is_symlink():
            add("warning", "db_symlink", "Database path follows a symbolic link")
        # Evaluate recovery evidence *before* classifying an absent DB as a
        # brand-new world: an orphaned journal is not a clean initial state.
        def sidecar_present(suffix: str) -> bool:
            # SQLite may create journal files next to the original filename
            # supplied by a host, not only its symlink-resolved target.
            for base in (path, resolved):
                candidate = base.with_name(base.name + suffix)
                if candidate.exists() or candidate.is_symlink():
                    return True
            return False

        if any(sidecar_present(suffix) for suffix in ("-wal", "-shm")):
            add("fatal", "db_wal_unverified", "WAL/SHM sidecar exists; immutable read cannot certify live state")
            return PreflightReport(tuple(findings))
        if sidecar_present("-journal"):
            add("fatal", "db_journal_unverified",
                "Rollback journal sidecar exists; immutable read cannot certify recovered state")
            return PreflightReport(tuple(findings))
        if not path.exists():
            add("warning", "db_missing", "World database is not initialized; authorized startup must create it")
            return PreflightReport(tuple(findings))
        if not path.is_file():
            add("fatal", "db_not_file", "Database target is not an ordinary file")
            return PreflightReport(tuple(findings))
        if not os.access(path, os.R_OK | os.W_OK):
            add("fatal", "db_permissions", "Database does not advertise read/write access")
        with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            observed = _schema_snapshot(db)
            expected = _pristine_schema()
            if any(observed.get(name) != descriptor for name, descriptor in expected.items()):
                add("fatal", "db_schema", "Required REALM core schema is missing or altered")
            else:
                row = db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()
                world_id = row[0] if row is not None else None
                if not world_id:
                    add("warning", "world_unbound", "Database has not been bound to a named world")
                elif expected_world_id is not None and world_id != expected_world_id:
                    add("fatal", "world_mismatch", "Stored world identity differs from requested host world")
    except (OSError, ValueError, sqlite3.Error):
        # Arbitrary path exceptions may contain credentials or private names.
        add("fatal", "db_invalid", "Unable to inspect database safely as SQLite")
    return PreflightReport(tuple(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only REALM local host preflight")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--telnet-host", default="127.0.0.1")
    parser.add_argument("--expected-world-id")
    parser.add_argument("--json", action="store_true", help="Output path-redacted machine-readable findings")
    args = parser.parse_args(argv)
    report = preflight_local_host(
        args.db, http_host=args.http_host, telnet_host=args.telnet_host,
        expected_world_id=args.expected_world_id,
    )
    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        print("REALM local preflight:", "READY" if report.ready else "NOT READY")
        for finding in report.findings:
            print(f"{finding.severity.upper()} [{finding.code}]: {finding.detail}")
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
