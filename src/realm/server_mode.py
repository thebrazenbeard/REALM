"""Host-only immutable Casual/Hardcore mode binding for an imported REALM world.

This seam never joins players or authorizes gameplay. A local SQLite binding
can be rolled back; live Hardcore requires independent off-database custody.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from .hardcore_finality import Admission, HardcoreFinalityGate
from .mortality import HealthState, MortalityPolicy

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CHARACTER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = """CREATE TABLE realm_server_mode (
    id INTEGER PRIMARY KEY CHECK(id=1),
    world_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('casual','hardcore')),
    policy_id TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    policy_version INTEGER NOT NULL CHECK(policy_version BETWEEN 1 AND 1000000),
    configuration_id TEXT NOT NULL,
    created_utc TEXT NOT NULL
)"""


class ModeBindingError(ValueError):
    """Untrusted, conflicting or unqualified per-world server mode."""


@dataclass(frozen=True)
class ServerModeBinding:
    world_id: str
    mode: str
    policy_id: str
    policy_hash: str
    policy_version: int
    configuration_id: str
    created_utc: str


@dataclass(frozen=True)
class ModeAdmission:
    status: str
    reason: str
    authorized_for_play: bool = False


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ModeBindingError(f"invalid {name}")
    return value


def _policy(policy: MortalityPolicy, version: int) -> None:
    if not isinstance(policy, MortalityPolicy):
        raise ModeBindingError("MortalityPolicy required")
    if type(version) is not int or not 1 <= version <= 1_000_000:
        raise ModeBindingError("invalid policy version")
    if policy.mode not in ("casual", "hardcore"):
        raise ModeBindingError("invalid server mode")


def _path(db_path: str | Path) -> Path:
    if not isinstance(db_path, (str, Path)) or str(db_path) in ("", ":memory:"):
        raise ModeBindingError("durable world SQLite file required")
    if str(db_path).startswith("file:"):
        raise ModeBindingError("SQLite URI paths not accepted")
    result = Path(db_path)
    if result.is_symlink() or not result.is_file():
        raise ModeBindingError("world SQLite file missing or symlinked")
    return result


def _world(db: sqlite3.Connection, world_id: str) -> None:
    names = {
        r[0] for r in db.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' "
            "AND name IN ('world_meta','players')"
        )
    }
    if names != {"world_meta", "players"}:
        raise ModeBindingError("world database schema incomplete")
    metadata_cols = tuple(r[1] for r in db.execute("PRAGMA main.table_info(world_meta)"))
    player_cols = tuple(r[1] for r in db.execute("PRAGMA main.table_info(players)"))
    if metadata_cols != ("key", "value") or player_cols != ("id", "room_id", "created_at"):
        raise ModeBindingError("world database schema changed")
    binding = db.execute(
        "SELECT value FROM main.world_meta WHERE key='world_id'"
    ).fetchone()
    if binding is None or binding[0] != world_id:
        raise ModeBindingError("world identity mismatch or unbound world")


def _table_state(db: sqlite3.Connection) -> bool:
    objects = db.execute(
        "SELECT type,name,sql FROM main.sqlite_master "
        "WHERE name='realm_server_mode' OR "
        "(type='trigger' AND tbl_name='realm_server_mode')"
    ).fetchall()
    if not objects:
        return False
    if objects != [("table", "realm_server_mode", _SCHEMA)]:
        raise ModeBindingError("server mode schema foreign or altered")
    return True


def _binding_row(db: sqlite3.Connection) -> ServerModeBinding:
    rows = db.execute(
        "SELECT id,world_id,mode,policy_id,policy_hash,policy_version,"
        "configuration_id,created_utc FROM main.realm_server_mode"
    ).fetchall()
    if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] != 1:
        raise ModeBindingError("server mode ledger missing or ambiguous")
    value = ServerModeBinding(*rows[0][1:])
    try:
        when = datetime.fromisoformat(value.created_utc)
    except (ValueError, TypeError):
        raise ModeBindingError("server mode timestamp invalid") from None
    if (
        not isinstance(value.world_id, str)
        or _IDENTIFIER.fullmatch(value.world_id) is None
        or value.mode not in ("casual", "hardcore")
        or not isinstance(value.policy_id, str) or not value.policy_id.strip()
        or not isinstance(value.policy_hash, str)
        or _DIGEST.fullmatch(value.policy_hash) is None
        or not isinstance(value.configuration_id, str)
        or _IDENTIFIER.fullmatch(value.configuration_id) is None
        or type(value.policy_version) is not int
        or not 1 <= value.policy_version <= 1_000_000
        or when.tzinfo is None or when.utcoffset().total_seconds() != 0
    ):
        raise ModeBindingError("server mode record invalid")
    return value


def bind_server_mode(
    db_path: str | Path, *, world_id: str, policy: MortalityPolicy,
    policy_version: int, configuration_id: str,
) -> ServerModeBinding:
    """Bind once before first player; exact retries cannot change mode."""
    world_id = _identifier(world_id, "world_id")
    configuration_id = _identifier(configuration_id, "configuration_id")
    _policy(policy, policy_version)
    path = _path(db_path)
    try:
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            _world(db, world_id)
            if _table_state(db):
                previous = _binding_row(db)
                if (
                    previous.world_id != world_id
                    or previous.mode != policy.mode
                    or previous.policy_id != policy.policy_id
                    or previous.policy_hash != policy.policy_hash
                    or previous.policy_version != policy_version
                    or previous.configuration_id != configuration_id
                ):
                    raise ModeBindingError("existing server mode cannot change")
                return previous
            if db.execute("SELECT 1 FROM main.players LIMIT 1").fetchone():
                raise ModeBindingError("played world cannot gain unreviewed mode")
            db.execute(_SCHEMA)
            created = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            db.execute(
                "INSERT INTO main.realm_server_mode VALUES(1,?,?,?,?,?,?,?)",
                (world_id, policy.mode, policy.policy_id, policy.policy_hash,
                 policy_version, configuration_id, created),
            )
            return ServerModeBinding(
                world_id, policy.mode, policy.policy_id, policy.policy_hash,
                policy_version, configuration_id, created,
            )
    except sqlite3.Error as exc:
        raise ModeBindingError("server mode SQLite operation rejected") from exc


def read_server_mode(db_path: str | Path, *, world_id: str) -> ServerModeBinding:
    """Read exact bound mode; missing/rolled-back mode is a hard HOLD."""
    world_id = _identifier(world_id, "world_id")
    try:
        with closing(sqlite3.connect(_path(db_path), timeout=10)) as db:
            _world(db, world_id)
            if not _table_state(db):
                raise ModeBindingError("unbound server mode requires host review")
            row = _binding_row(db)
            if row.world_id != world_id:
                raise ModeBindingError("server mode world mismatch")
            return row
    except sqlite3.Error as exc:
        raise ModeBindingError("server mode read failed closed") from exc


def check_mode_admission(
    db_path: str | Path, *, world_id: str, character_id: str,
    policy: MortalityPolicy, policy_version: int,
    witness_gate: HardcoreFinalityGate | None = None,
    local_state: HealthState | None = None,
) -> ModeAdmission:
    """Preflight only: NEVER returns host permission to join or act."""
    _policy(policy, policy_version)
    if not isinstance(character_id, str) or not _CHARACTER.fullmatch(character_id):
        raise ModeBindingError("invalid character identity")
    record = read_server_mode(db_path, world_id=world_id)
    if (
        record.mode != policy.mode or record.policy_id != policy.policy_id
        or record.policy_hash != policy.policy_hash
        or record.policy_version != policy_version
    ):
        raise ModeBindingError("server mode or policy hash mismatch")
    if record.mode == "casual":
        if witness_gate is not None:
            raise ModeBindingError("Casual mode cannot accept Hardcore witness")
        return ModeAdmission("HOST_AUTH_REQUIRED", "mode_bound_not_player_authenticated")
    if witness_gate is None:
        return ModeAdmission("WITNESS_REQUIRED", "death_authority_not_configured")
    if (
        not isinstance(witness_gate, HardcoreFinalityGate)
        or witness_gate.world_id != world_id
        or witness_gate.policy_hash != record.policy_hash
        or not isinstance(local_state, HealthState)
    ):
        return ModeAdmission("HOLD", "witness_binding_incompatible")
    result = witness_gate.check(character_id, local_state)
    if result.decision is Admission.RETIRED:
        return ModeAdmission("RETIRED", result.reason)
    if result.decision is Admission.HOLD:
        return ModeAdmission("HOLD", result.reason)
    return ModeAdmission("HOST_AUTH_REQUIRED", "witness_clear_not_player_auth")
