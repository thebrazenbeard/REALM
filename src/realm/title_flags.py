"""Read-only ship title presentation policy, not ship/crew authorization.

Captain presentation derives from the current, persisted ship owner. First
Officer requires a short-lived, host-signed display grant; neither label gives
permission to pilot, transfer a ship, or alter a world.

This module deliberately does not install a crew schema, set titles through
client commands, or import any setting-specific character and ship names.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import sqlite3

from .engine import _PLAYER_RE


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_EPOCH = (1 << 63) - 1
_MAX_GRANT_SECONDS = 3600
_ROLE_FIRST_OFFICER = "first_officer"


@dataclass(frozen=True, slots=True)
class OfficerGrant:
    """Opaque-to-players display proof, issued with the server's secret key.

    Valid only until the declared UTC expiry and while the persisted ship
    remains assigned to the same owner in the same world. No gameplay
    capability or permanent crew membership follows from this object.
    """

    world_id: str
    ship_id: str
    owner: str
    actor: str
    role: str
    expires_utc: int
    signature: str


def _check_identifiers(actor: str, ship_id: str, world_id: str) -> None:
    if not isinstance(actor, str) or not _PLAYER_RE.fullmatch(actor):
        raise ValueError("Invalid player identifier")
    if not isinstance(ship_id, str) or not _IDENTIFIER.fullmatch(ship_id):
        raise ValueError("Invalid ship identifier")
    if not isinstance(world_id, str) or not _IDENTIFIER.fullmatch(world_id):
        raise ValueError("Invalid world identifier")


def _key_valid(signing_key: bytes | None) -> bool:
    return type(signing_key) is bytes and len(signing_key) >= 32


def _now_valid(now_utc: int) -> bool:
    return type(now_utc) is int and 0 <= now_utc <= _MAX_EPOCH


def _canonical_player(db: sqlite3.Connection, actor: str) -> str | None:
    row = db.execute(
        "SELECT id FROM main.players WHERE id=? COLLATE NOCASE", (actor,)
    ).fetchone()
    return row[0] if row is not None else None


def _bound_world(db: sqlite3.Connection, world_id: str) -> bool:
    row = db.execute(
        "SELECT value FROM main.world_meta WHERE key='world_id'"
    ).fetchone()
    return row is not None and row[0] == world_id


def _ship_owner(db: sqlite3.Connection, ship_id: str) -> str | None:
    # Core-only worlds deliberately do not install the optional Geography
    # schema. Looking up a label must not create/alter that schema.
    installed = db.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='geo_ships'"
    ).fetchone()
    if installed is None:
        return None
    row = db.execute(
        "SELECT owner FROM main.geo_ships WHERE id=?", (ship_id,)
    ).fetchone()
    return row[0] if row is not None else None


def _roster_match(
    db: sqlite3.Connection, ship_id: str, owner: str, officer: str,
) -> bool | None:
    """Tri-state role check: None means opt-in roster schema is not installed.

    Once a world installs the authoritative crew ledger, a signed legacy
    display grant must no longer bypass its revocation state.
    """
    installed = db.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='crew_role_state'"
    ).fetchone()
    if installed is None:
        return None
    return db.execute(
        "SELECT 1 FROM main.crew_role_state "
        "WHERE ship_id=? AND owner=? AND officer=?",
        (ship_id, owner, officer),
    ).fetchone() is not None


def _signature(signing_key: bytes, grant: OfficerGrant) -> str:
    payload = json.dumps(
        (grant.world_id, grant.ship_id, grant.owner, grant.actor,
         grant.role, grant.expires_utc),
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return hmac.new(signing_key, payload, hashlib.sha256).hexdigest()


def issue_first_officer_grant(
    db: sqlite3.Connection, *, actor: str, ship_id: str, world_id: str,
    signing_key: bytes, expires_utc: int, now_utc: int,
) -> OfficerGrant:
    """Mint a *display-only* proof through an authorized host call.

    This function cannot authenticate the calling host. Keep the signing key
    exclusively on the server; never expose this function as a player route.
    Actor identity and role-appointment approval must already have occurred
    in the caller's trusted domain before it invokes this function.
    """
    _check_identifiers(actor, ship_id, world_id)
    if not _key_valid(signing_key):
        raise ValueError("Host signing key must be at least 32 bytes")
    if not _now_valid(now_utc) or type(expires_utc) is not int or not (
        now_utc < expires_utc <= min(_MAX_EPOCH, now_utc + _MAX_GRANT_SECONDS)
    ):
        raise ValueError("Grant requires a host UTC expiry within one hour")
    if not _bound_world(db, world_id):
        raise PermissionError("Ship title grant world mismatch")
    player = _canonical_player(db, actor)
    owner = _ship_owner(db, ship_id)
    if player is None or owner is None or player == owner:
        raise PermissionError("Host cannot grant this ship display role")
    # Only existing, joined owner identities can anchor a crew label.
    if _canonical_player(db, owner) != owner:
        raise PermissionError("Ship owner is not a joined canonical principal")
    if _roster_match(db, ship_id, owner, player) is False:
        raise PermissionError("Officer is not currently appointed in the crew ledger")
    candidate = OfficerGrant(
        world_id=world_id, ship_id=ship_id, owner=owner,
        actor=player, role=_ROLE_FIRST_OFFICER,
        expires_utc=expires_utc, signature="",
    )
    return OfficerGrant(
        world_id=candidate.world_id, ship_id=candidate.ship_id,
        owner=candidate.owner, actor=candidate.actor, role=candidate.role,
        expires_utc=candidate.expires_utc,
        signature=_signature(signing_key, candidate),
    )


def ship_display_name(
    db: sqlite3.Connection, *, actor: str, ship_id: str, world_id: str,
    officer_grant: OfficerGrant | None = None, signing_key: bytes | None = None,
    now_utc: int | None = None,
) -> str:
    """Render a canonical joined name plus a verified ship-derived label.

    Unknown/other-owned ships and missing/expired/altered role evidence
    return the plain player name. The result has no authority outside UI
    presentation and never changes persistent world state.
    """
    _check_identifiers(actor, ship_id, world_id)
    player = _canonical_player(db, actor)
    if player is None:
        return actor
    if not _bound_world(db, world_id):
        return player
    owner = _ship_owner(db, ship_id)
    if owner is None:
        return player
    if owner == player:
        return f"Capt. {player}"
    if _roster_match(db, ship_id, owner, player) is False:
        # An installed crew ledger supersedes prior host-signed display
        # grants, including after a revoke or owner-change invalidation.
        return player
    if (
        not isinstance(officer_grant, OfficerGrant)
        or not _key_valid(signing_key)
        or not _now_valid(now_utc)
    ):
        return player
    if (
        officer_grant.world_id != world_id
        or officer_grant.ship_id != ship_id
        or officer_grant.owner != owner
        or officer_grant.actor != player
        or officer_grant.role != _ROLE_FIRST_OFFICER
        or type(officer_grant.expires_utc) is not int
        or not (now_utc < officer_grant.expires_utc <=
                min(_MAX_EPOCH, now_utc + _MAX_GRANT_SECONDS))
        or type(officer_grant.signature) is not str
        or not _HEX_SHA256.fullmatch(officer_grant.signature)
    ):
        return player
    expected = _signature(signing_key, officer_grant)
    if not hmac.compare_digest(expected, officer_grant.signature):
        return player
    return f"First Officer {player}"


def ship_roster_display_name(
    db: sqlite3.Connection, *, actor: str, ship_id: str, world_id: str,
) -> str:
    """Render captain/first-officer labels from the current, installed ledger.

    This is a read-only *presentation* seam. It cannot grant gameplay rights,
    and clients must not choose the actor passed by an authenticated host.
    Legacy signed-grant rendering remains in ship_display_name until a host
    opts into the crew ledger; it cannot bypass the ledger once installed.
    """
    _check_identifiers(actor, ship_id, world_id)
    player = _canonical_player(db, actor)
    if player is None:
        return actor
    if not _bound_world(db, world_id):
        return player
    owner = _ship_owner(db, ship_id)
    if owner is None:
        return player
    if owner == player:
        return f"Capt. {player}"
    if _roster_match(db, ship_id, owner, player) is True:
        return f"First Officer {player}"
    return player
