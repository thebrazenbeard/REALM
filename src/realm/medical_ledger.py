"""Opt-in SQLite medical treatment ledger, not live mortality or death authority.

This module expects *trusted host* authentication, proximity, source records,
and time. It never marks a character dead, provisions user credentials, or
changes REALM's normal Engine/Gateway routes. The ledger is rollbackable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .mortality import HealthState, Injury, MortalityPolicy, injure, intervene


@dataclass(frozen=True)
class TreatmentReceipt:
    action_id: str
    injury_id: str
    state: HealthState
    version: int


class MedicalLedger:
    """Per-world opt-in sidecar; one transactional kit spend per intervention."""

    def __init__(self, db_path: str | Path, *, world_id: str,
                 policy: MortalityPolicy):
        if not isinstance(world_id, str) or not world_id.strip():
            raise ValueError("invalid medical world_id")
        if not isinstance(policy, MortalityPolicy):
            raise ValueError("invalid mortality policy")
        if str(db_path) == ":memory:":
            raise ValueError("medical ledger requires a durable SQLite sidecar file")
        self.path = Path(db_path)
        self.world_id = world_id
        self.policy = policy
        identity = {"world_id": world_id, "mode": policy.mode,
                    "policy_hash": policy.policy_hash}
        expected_tables = {
            "medical_meta", "medical_supply", "medical_injuries",
            "medical_receipts",
        }
        # Lock before checking identity: foreign or partly initialized files
        # must not acquire a medical schema through this constructor.
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','view','trigger','index') "
                "AND name NOT LIKE 'sqlite_%'")}
            if tables and tables != expected_tables:
                raise ValueError("medical ledger requires a pristine or bound sidecar")
            if tables:
                existing = dict(db.execute("SELECT key,value FROM medical_meta"))
                if existing != identity:
                    raise ValueError("medical world/policy identity mismatch")
                return
            for statement in (
                """CREATE TABLE medical_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
                """CREATE TABLE medical_supply (
                    resource_id TEXT PRIMARY KEY, quantity INTEGER NOT NULL
                    CHECK(quantity >= 0))""",
                """CREATE TABLE medical_injuries (
                    injury_id TEXT PRIMARY KEY, world_id TEXT NOT NULL,
                    character_id TEXT NOT NULL, mode TEXT NOT NULL,
                    policy_id TEXT NOT NULL, policy_hash TEXT NOT NULL,
                    cause TEXT NOT NULL, severity TEXT NOT NULL,
                    state TEXT NOT NULL, incident_utc TEXT NOT NULL,
                    deadline_utc TEXT NOT NULL, last_utc TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version >= 0))""",
                """CREATE TABLE medical_receipts (
                    action_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    injury_id TEXT NOT NULL REFERENCES medical_injuries(injury_id),
                    state TEXT NOT NULL, version INTEGER NOT NULL)""",
            ):
                db.execute(statement)
            db.executemany("INSERT INTO medical_meta VALUES (?,?)",
                           identity.items())

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            return db
        except BaseException:
            db.close()
            raise

    def register_injury(self, injury_id: str, injury: Injury) -> None:
        """Host stages one pure injury; never replaces an existing incident."""
        if not isinstance(injury_id, str) or not injury_id.strip():
            raise ValueError("invalid injury_id")
        if (not isinstance(injury, Injury)
                or injury.world_id != self.world_id
                or injury.mode != self.policy.mode
                or injury.policy_id != self.policy.policy_id
                or injury.policy_hash != self.policy.policy_hash
                or injury.state is not HealthState.CRITICAL):
            raise ValueError("injury does not match this medical world/policy")
        canonical = injure(
            world_id=injury.world_id, character_id=injury.character_id,
            cause=injury.cause, severity=injury.severity,
            occurred_at=injury.incident_utc, policy=self.policy)
        if injury != canonical:
            raise ValueError("injury does not match policy-derived rescue window")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO medical_injuries VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (injury_id, injury.world_id, injury.character_id, injury.mode,
                 injury.policy_id, injury.policy_hash, injury.cause,
                 injury.severity, injury.state.value,
                 injury.incident_utc.isoformat(), injury.deadline_utc.isoformat(),
                 injury.last_observed_utc.isoformat()))

    def stock(self, resource_id: str, quantity: int) -> None:
        """Host-only initial fixture/provisioning; not a player refill API."""
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("invalid resource_id")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("invalid initial quantity")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO medical_supply VALUES (?,?)",
                       (resource_id, quantity))

    def treat(self, *, world_id: str, injury_id: str, actor_id: str,
              resource_id: str, action_id: str, expected_version: int,
              at: datetime, authorized: bool, nearby: bool) -> TreatmentReceipt:
        """Consume one stock unit and stabilize one injury or do neither.

        The booleans and UTC time are assertions by a trusted host, NOT
        independently authenticated facts. This is not an exposed player API.
        """
        if world_id != self.world_id:
            raise ValueError("cross-world medical intervention rejected")
        if not all(isinstance(v, str) and v.strip()
                   for v in (injury_id, actor_id, resource_id, action_id)):
            raise ValueError("invalid medical action identity")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("invalid expected injury version")
        if (not isinstance(at, datetime) or at.tzinfo is None
                or at.utcoffset() != timedelta(0)):
            raise ValueError("UTC-aware treatment time required")
        if authorized is not True:
            raise ValueError("unauthorized medical action")
        fingerprint_material = (world_id, injury_id, actor_id, resource_id,
                                action_id, expected_version, at.isoformat(),
                                self.policy.policy_hash)
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_material, separators=(",", ":")).encode("utf-8")).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT fingerprint,injury_id,state,version FROM medical_receipts "
                "WHERE action_id=?", (action_id,)).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("medical action_id collision")
                return TreatmentReceipt(action_id, injury_id,
                                        HealthState(previous["state"]),
                                        previous["version"])
            if nearby is not True:
                raise ValueError("unverified medical proximity")
            row = db.execute("SELECT * FROM medical_injuries WHERE injury_id=?",
                             (injury_id,)).fetchone()
            if row is None or row["world_id"] != world_id:
                raise ValueError("unknown or wrong-world injury")
            if row["version"] != expected_version:
                raise ValueError("stale injury version")
            if (row["policy_hash"] != self.policy.policy_hash
                    or row["mode"] != self.policy.mode):
                raise ValueError("injury policy mismatch")
            incident = Injury(
                row["world_id"], row["character_id"], row["mode"],
                row["policy_id"], row["policy_hash"], row["cause"],
                row["severity"], HealthState(row["state"]),
                datetime.fromisoformat(row["incident_utc"]),
                datetime.fromisoformat(row["deadline_utc"]),
                datetime.fromisoformat(row["last_utc"]))
            resolved = intervene(incident, at=at, action_id=action_id,
                                 actor_id=actor_id, resource_id=resource_id,
                                 authorized=True, nearby=True,
                                 supply_verified=True)
            if resolved.state is not HealthState.STABILIZED:
                raise ValueError("medical treatment did not stabilize injury")
            consumed = db.execute(
                "UPDATE medical_supply SET quantity=quantity-1 "
                "WHERE resource_id=? AND quantity>0", (resource_id,))
            if consumed.rowcount != 1:
                raise ValueError("insufficient medical inventory")
            updated = db.execute(
                "UPDATE medical_injuries SET state=?,last_utc=?,version=version+1 "
                "WHERE injury_id=? AND version=? AND state=?",
                (resolved.state.value, resolved.last_observed_utc.isoformat(),
                 injury_id, expected_version, HealthState.CRITICAL.value))
            if updated.rowcount != 1:
                raise ValueError("medical injury CAS conflict")
            result_version = expected_version + 1
            db.execute(
                "INSERT INTO medical_receipts VALUES (?,?,?,?,?)",
                (action_id, fingerprint, injury_id,
                 resolved.state.value, result_version))
            return TreatmentReceipt(action_id, injury_id,
                                    resolved.state, result_version)
