"""Read-only bridge for co-review of REALM Hardcore source-only mechanics.

This is not injury registration, treatment, authority, a medical prediction,
or a release path. Separate SQLite files do not provide cross-file atomicity.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sqlite3

from .atmosphere_ledger import AtmosphereLedger
from .exposure_injury import (
    ExposureDecision, ExposureProfile, evaluate_exposure_in_transaction,
)
from .mortality import MortalityPolicy, _require_utc
from .server_mode import (
    ServerModeBinding, _binding_row, _table_state, _world,
)


# Mirrors MedicalLedger's own four CREATE TABLE statements. Deliberately
# fail closed if its schema evolves without a reviewed bridge update.
_MEDICAL_DDL = {
    "medical_meta": "CREATE TABLE medical_meta ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "medical_supply": "CREATE TABLE medical_supply ("
                      "resource_id TEXT PRIMARY KEY, quantity INTEGER NOT NULL "
                      "CHECK(quantity >= 0))",
    "medical_injuries": "CREATE TABLE medical_injuries ("
                        "injury_id TEXT PRIMARY KEY, world_id TEXT NOT NULL, "
                        "character_id TEXT NOT NULL, mode TEXT NOT NULL, "
                        "policy_id TEXT NOT NULL, policy_hash TEXT NOT NULL, "
                        "cause TEXT NOT NULL, severity TEXT NOT NULL, "
                        "state TEXT NOT NULL, incident_utc TEXT NOT NULL, "
                        "deadline_utc TEXT NOT NULL, last_utc TEXT NOT NULL, "
                        "version INTEGER NOT NULL CHECK(version >= 0))",
    "medical_receipts": "CREATE TABLE medical_receipts ("
                        "action_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                        "injury_id TEXT NOT NULL REFERENCES medical_injuries(injury_id), "
                        "state TEXT NOT NULL, version INTEGER NOT NULL)",
}


@dataclass(frozen=True)
class MedicalSourceReview:
    status: str
    mode_binding: ServerModeBinding
    exposure: ExposureDecision
    authorized_for_injury_write: bool = field(default=False, init=False)
    authorized_for_treatment: bool = field(default=False, init=False)


def _server_binding(db: sqlite3.Connection, world_id: str) -> ServerModeBinding:
    # Existing mode verifier expects tuple rows, while atmosphere uses Row.
    previous = db.row_factory
    try:
        db.row_factory = None
        _world(db, world_id)
        if not _table_state(db):
            raise ValueError("unbound server mode")
        result = _binding_row(db)
    finally:
        db.row_factory = previous
    if result.world_id != world_id:
        raise ValueError("server mode world mismatch")
    return result


def _medical_sidecar_bound(path: str | Path, *, world_id: str,
                          policy: MortalityPolicy) -> None:
    if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
        raise ValueError("durable medical sidecar path required")
    if str(path).startswith("file:"):
        raise ValueError("medical URI path not accepted")
    file = Path(path)
    if not file.is_file() or file.is_symlink():
        raise ValueError("medical sidecar missing or symlinked")
    # mode=ro prevents a missing/moved sidecar from being silently created.
    try:
        with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            objects = db.execute(
                "SELECT type,name,sql FROM main.sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if (len(objects) != len(_MEDICAL_DDL)
                or any(kind != "table" or name not in _MEDICAL_DDL
                       or type(ddl) is not str
                       or " ".join(ddl.split()).replace("( ", "(") !=
                          " ".join(_MEDICAL_DDL[name].split()).replace("( ", "(")
                       for kind, name, ddl in objects)):
                raise ValueError("medical sidecar schema mismatch")
            rows = db.execute("SELECT key,value FROM main.medical_meta").fetchall()
            expected = {"world_id": world_id, "mode": policy.mode,
                        "policy_hash": policy.policy_hash}
            if len(rows) != len(expected) or dict(rows) != expected:
                raise ValueError("medical sidecar world/policy mismatch")
            # Read-only full-table validation can be O(database size).
            # Bound this review-only path to 64 MiB and fail closed if larger.
            page_count = db.execute("PRAGMA main.page_count").fetchone()[0]
            page_size = db.execute("PRAGMA main.page_size").fetchone()[0]
            if page_count * page_size > 64 * 1024 * 1024:
                raise ValueError("medical sidecar exceeds integrity review limit")
            # Exact DDL does not attest old rows: SQLite may previously have
            # accepted invalid CHECK values or orphan FKs with enforcement off.
            if db.execute("PRAGMA main.integrity_check").fetchone() != ("ok",):
                raise ValueError("medical sidecar integrity violation")
            # Do not rely solely on PRAGMA integrity_check's CHECK coverage:
            # a fresh read-only connection can miss prior rows inserted with
            # ignore_check_constraints=ON in some SQLite runtime versions.
            if db.execute(
                "SELECT 1 FROM main.medical_supply "
                "WHERE quantity < 0 OR typeof(quantity) != 'integer' LIMIT 1"
            ).fetchone() is not None:
                raise ValueError("medical sidecar supply integrity violation")
            if db.execute(
                "SELECT 1 FROM main.medical_injuries "
                "WHERE version < 0 OR typeof(version) != 'integer' LIMIT 1"
            ).fetchone() is not None:
                raise ValueError("medical sidecar injury integrity violation")
            if db.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
                raise ValueError("medical sidecar referential integrity violation")
    except sqlite3.Error as exc:
        raise ValueError("medical sidecar cannot be read safely") from exc


def inspect_medical_source_in_transaction(
    atmosphere: AtmosphereLedger, db: sqlite3.Connection, *,
    world_id: str, event_id: str, at_utc: datetime, expected_version: int,
    profiles: dict[str, ExposureProfile], mortality_policy: MortalityPolicy,
    policy_version: int, medical_db_path: str | Path,
) -> MedicalSourceReview:
    """Cross-check live *source* snapshots; always block injury and treatment.

    The caller must hold the authentic world transaction; a separate medical
    sidecar snapshot and server-mode SQLite binding cannot prove independent
    identity, rollback resistance, outage fairness, or treatment authorization.
    """
    if type(db) is not sqlite3.Connection or not db.in_transaction:
        raise ValueError("world SQLite transaction required")
    if type(mortality_policy) is not MortalityPolicy:
        raise ValueError("MortalityPolicy required")
    if type(policy_version) is not int or not 1 <= policy_version <= 1_000_000:
        raise ValueError("invalid server policy version")
    _require_utc(at_utc)
    binding = _server_binding(db, world_id)
    if (binding.mode != mortality_policy.mode
        or binding.policy_id != mortality_policy.policy_id
        or binding.policy_hash != mortality_policy.policy_hash
        or binding.policy_version != policy_version):
        raise ValueError("server mode/policy binding mismatch")
    if at_utc < datetime.fromisoformat(binding.created_utc):
        raise ValueError("medical event predates server mode binding")
    _medical_sidecar_bound(medical_db_path, world_id=world_id,
                           policy=mortality_policy)
    decision = evaluate_exposure_in_transaction(
        atmosphere, db, world_id=world_id, event_id=event_id,
        at_utc=at_utc, expected_version=expected_version,
        profiles=profiles, mortality_policy=mortality_policy)
    return MedicalSourceReview("HELD_HOST_REVIEW_REQUIRED", binding, decision)
