"""Source-only, host-key-gated *staging* of exact Creator worldpack candidates.

An HMAC proves possession of a separately provisioned host staging key; it does
NOT prove Patrick's authorization, source rights, canon, or deployment consent.
This module never imports a world, changes the game database, serves a route, or
provides a release/approval function. Its SQLite ledger is rollbackable; an
independent owner witness and protected custody are required for real release.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3

from experiments.realm_creator_ui.existing_pack import preview_existing_worldpack
from realm.worldpack import WorldpackError

DOMAIN = b"REALM/CREATOR-RELEASE-STAGING/v1\x00"
AUTH_SCHEMA = "realm.creator.release-authorization.v1"
MANIFEST_SCHEMA = "realm.creator.source-manifest.v1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AUTH_FIELDS = {
    "schema", "scope", "release_id", "owner_id", "key_id", "ledger_id",
    "world_id", "revision", "raw_sha256", "semantic_sha256",
    "source_manifest_sha256",
}
_DDL = """CREATE TABLE creator_release_staging (
    release_id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    ledger_id TEXT NOT NULL,
    approval_sha256 TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    semantic_sha256 TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status='STAGED_NOT_IMPORTED'),
    UNIQUE (world_id, revision)
)"""


class ReleaseGateError(ValueError):
    """Untrusted staging material or unsafe local ledger was rejected."""


def _object(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ReleaseGateError(f"{label} has invalid fields")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ReleaseGateError(f"Invalid {label}")
    return value


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseGateError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ReleaseGateError(f"Forbidden JSON number: {value}")


def _json_object(raw: bytes, limit: int, label: str) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ReleaseGateError(f"{label} exceeds byte cap or is empty")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except ReleaseGateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError,
            OverflowError) as exc:
        raise ReleaseGateError(f"Invalid {label} UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseGateError(f"{label} must be an object")
    return value


def _canonical(value: dict) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ReleaseGateError("Invalid canonical approval") from exc


def _validate_manifest(raw: bytes, worldpack_sha: str) -> None:
    manifest = _object(_json_object(raw, 512 * 1024, "source manifest"),
                       {"schema", "sources"}, "source manifest")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ReleaseGateError("Invalid source manifest schema")
    sources = manifest["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 4096:
        raise ReleaseGateError("Invalid source manifest count")
    seen = set()
    worldpack_bound = False
    for index, record in enumerate(sources):
        record = _object(record, {"source_id", "sha256"}, f"source[{index}]")
        source_id = _identifier(record["source_id"], "source_id")
        sha = record["sha256"]
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ReleaseGateError("Invalid source manifest sha256")
        if source_id in seen:
            raise ReleaseGateError("Duplicate source manifest source_id")
        seen.add(source_id)
        if source_id == "worldpack":
            worldpack_bound = hmac.compare_digest(sha, worldpack_sha)
    if not worldpack_bound:
        raise ReleaseGateError("Source manifest must bind exact worldpack raw SHA")


def _validate_approval(approval: dict, preview: dict, manifest_sha: str,
                       expected_owner_id: str, expected_key_id: str,
                       expected_ledger_id: str) -> None:
    _object(approval, _AUTH_FIELDS, "authorization")
    if approval["schema"] != AUTH_SCHEMA or approval["scope"] != "STAGE_ONLY":
        raise ReleaseGateError("Invalid staging authorization schema or scope")
    for field in ("release_id", "owner_id", "key_id", "ledger_id", "world_id"):
        _identifier(approval[field], field)
    if approval["owner_id"] != _identifier(expected_owner_id, "expected owner"):
        raise ReleaseGateError("owner_id does not match configured host owner")
    if approval["key_id"] != _identifier(expected_key_id, "expected key_id"):
        raise ReleaseGateError("key_id does not match configured host key")
    if approval["ledger_id"] != _identifier(expected_ledger_id, "expected ledger_id"):
        raise ReleaseGateError("ledger_id does not match configured host ledger")
    if type(approval["revision"]) is not int or not 1 <= approval["revision"] <= 1_000_000_000:
        raise ReleaseGateError("Invalid revision")
    expected = {
        "world_id": preview["worldpack"]["world_id"],
        "raw_sha256": preview["provenance"]["source_sha256"],
        "semantic_sha256": preview["semantic_sha256"],
        "source_manifest_sha256": manifest_sha,
    }
    for field, actual in expected.items():
        if not isinstance(approval[field], str) or not hmac.compare_digest(approval[field], actual):
            raise ReleaseGateError(f"Authorization {field} mismatches exact staged source")


def _stage_ledger(db_path: Path, approval: dict, approval_sha: str) -> bool:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path, timeout=10.0) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE")
            tables = list(db.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ))
            temp_objects = list(db.execute("SELECT name FROM sqlite_temp_master"))
            if temp_objects or (tables and tables != [("table", "creator_release_staging", _DDL)]):
                raise ReleaseGateError("unexpected ledger schema; refusing staging")
            if not tables:
                db.execute(_DDL)
            row = db.execute(
                "SELECT world_id,revision,owner_id,key_id,ledger_id,approval_sha256,"
                "raw_sha256,semantic_sha256,source_manifest_sha256,status "
                "FROM creator_release_staging WHERE release_id=?",
                (approval["release_id"],),
            ).fetchone()
            expected = (
                approval["world_id"], approval["revision"], approval["owner_id"],
                approval["key_id"], approval["ledger_id"], approval_sha,
                approval["raw_sha256"], approval["semantic_sha256"],
                approval["source_manifest_sha256"], "STAGED_NOT_IMPORTED",
            )
            if row is not None:
                if tuple(row) != expected:
                    raise ReleaseGateError("release_id was already staged for different material")
                return True
            try:
                db.execute(
                    "INSERT INTO creator_release_staging "
                    "(release_id,world_id,revision,owner_id,key_id,ledger_id,approval_sha256,"
                    "raw_sha256,semantic_sha256,source_manifest_sha256,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (approval["release_id"], *expected),
                )
            except sqlite3.IntegrityError as exc:
                raise ReleaseGateError("World and revision already staged in this ledger") from exc
    except sqlite3.Error as exc:
        raise ReleaseGateError("SQLite release staging failed closed") from exc
    return False


def stage_exact_worldpack(
    ledger_path: str | Path,
    worldpack_raw: bytes,
    source_manifest_raw: bytes,
    authorization_raw: bytes,
    staging_mac_hex: str,
    *,
    expected_owner_id: str,
    expected_key_id: str,
    expected_ledger_id: str,
    host_signing_key: bytes,
) -> dict:
    """Stage a *held* exact candidate, without granting release or import authority.

    The host must provision a private, purpose-specific signing key separately;
    the Creator UI/player bearer token must never be used as that key. This
    function does not establish who holds the key, nor verify owner's identity.
    """
    if not isinstance(host_signing_key, bytes) or len(host_signing_key) < 32:
        raise ReleaseGateError("A separately provisioned >=32-byte host signing key is required")
    if not isinstance(staging_mac_hex, str) or not _SHA256.fullmatch(staging_mac_hex):
        raise ReleaseGateError("Invalid staging signature")
    if not isinstance(authorization_raw, bytes) or not 0 < len(authorization_raw) <= 8192:
        raise ReleaseGateError("Invalid authorization byte length")
    actual_mac = hmac.new(host_signing_key, DOMAIN + authorization_raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(staging_mac_hex, actual_mac):
        raise ReleaseGateError("Staging signature failed host-key verification")
    approval = _json_object(authorization_raw, 8192, "authorization")
    if authorization_raw != _canonical(approval):
        raise ReleaseGateError("Authorization must use exact canonical UTF-8 JSON")
    try:
        preview = preview_existing_worldpack(worldpack_raw, {"source_id": "worldpack"})
    except WorldpackError as exc:
        raise ReleaseGateError(str(exc)) from exc
    _validate_manifest(source_manifest_raw, preview["provenance"]["source_sha256"])
    _validate_approval(approval, preview, _digest(source_manifest_raw),
                       expected_owner_id, expected_key_id, expected_ledger_id)
    if not isinstance(ledger_path, (str, Path)) or str(ledger_path) in {"", ":memory:"}:
        raise ReleaseGateError("A durable, dedicated local ledger path is required")
    if str(ledger_path).startswith("file:"):
        raise ReleaseGateError("SQLite URI or shared-memory ledger paths are prohibited")
    unchanged = _stage_ledger(Path(ledger_path), approval, _digest(authorization_raw))
    return {
        "schema": "realm.creator.release-stage-receipt.v1",
        "status": "STAGED_NOT_IMPORTED", "release_id": approval["release_id"],
        "world_id": approval["world_id"], "revision": approval["revision"],
        "raw_sha256": approval["raw_sha256"],
        "semantic_sha256": approval["semantic_sha256"],
        "source_manifest_sha256": approval["source_manifest_sha256"],
        "unchanged": unchanged, "host_key_verified": True,
        "owner_authority": "HELD_UNVERIFIED", "rights_status": "UNVERIFIED",
        "canon_status": "UNREVIEWED", "release_permitted": False,
        "world_imported": False,
    }
