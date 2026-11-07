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
            objects = {tuple(row) for row in db.execute(
                "SELECT type,name FROM main.sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'")}
            expected = {("table", name) for name in (
                "medical_meta", "medical_supply",
                "medical_injuries", "medical_receipts")}
            if objects != expected:
                raise ValueError("medical sidecar schema mismatch")
            rows = db.execute("SELECT key,value FROM main.medical_meta").fetchall()
            expected = {"world_id": world_id, "mode": policy.mode,
                        "policy_hash": policy.policy_hash}
            if len(rows) != len(expected) or dict(rows) != expected:
                raise ValueError("medical sidecar world/policy mismatch")
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
