"""Review-only, source-neutral atmosphere exposure -> injury proposals.

The atmosphere kernel's built-in exposure flags are synthetic defaults and
are deliberately NOT used as universal physiology. A host must provide an
explicit profile per occupant and versioned mortality policy. This module
never writes an injury, consumes a medical resource, signs a tombstone, or
authenticates a character. Clock/event IDs are correlation labels, not receipts.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Mapping

from .atmosphere import _MAX_PRESSURE
from .atmosphere_ledger import AtmosphereLedger, _rehydrate, _read, _MAX_STATE_BYTES
from .mortality import Injury, MortalityPolicy, _require_utc, injure

_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class ExposureProfile:
    """A proposed game-balance profile, not a source/canon or medical fact."""

    profile_id: str
    min_pressure_pa: int
    min_oxygen_pa: int
    pressure_injury: tuple[str, str]
    oxygen_injury: tuple[str, str]
    profile_hash: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.profile_id, str) or not _IDENT.fullmatch(self.profile_id):
            raise ValueError("Invalid exposure profile ID")
        for threshold in (self.min_pressure_pa, self.min_oxygen_pa):
            if type(threshold) is not int or not 0 <= threshold <= _MAX_PRESSURE:
                raise ValueError("Invalid profile pressure threshold")
        for injury in (self.pressure_injury, self.oxygen_injury):
            if (type(injury) is not tuple or len(injury) != 2
                    or any(type(x) is not str or not x.strip() for x in injury)):
                raise ValueError("Invalid exposure injury mapping")
        material = {"schema": "realm.exposure.profile.v1",
                    "profile_id": self.profile_id,
                    "min_pressure_pa": self.min_pressure_pa,
                    "min_oxygen_pa": self.min_oxygen_pa,
                    "pressure_injury": self.pressure_injury,
                    "oxygen_injury": self.oxygen_injury}
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True).encode("utf-8")
        object.__setattr__(self, "profile_hash", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class ExposureDecision:
    status: str
    atmosphere_version: int
    proposals: tuple[Injury, ...]
    event_id: str
    source_state_sha256: str
    mortality_policy_hash: str
    exposure_policy_hash: str
    authorized_for_injury_write: bool = field(default=False, init=False)


def evaluate_exposure_in_transaction(
    ledger: AtmosphereLedger, db: sqlite3.Connection, *, world_id: str,
    event_id: str, at_utc, expected_version: int,
    profiles: Mapping[str, ExposureProfile],
    mortality_policy: MortalityPolicy,
) -> ExposureDecision:
    """Observe one caller-owned world transaction; propose, never persist.

    The host must atomically recheck and separately authorize any later injury
    write. This evaluation is not an append-only witness, a medical prognosis,
    or a promise of replay-sealed injury registration.
    """
    if type(ledger) is not AtmosphereLedger or type(db) is not sqlite3.Connection:
        raise ValueError("Atmosphere ledger and SQLite connection required")
    if type(event_id) is not str or not _IDENT.fullmatch(event_id):
        raise ValueError("Invalid exposure event ID")
    _require_utc(at_utc)
    if type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1:
        raise ValueError("Invalid atmosphere version")
    if type(mortality_policy) is not MortalityPolicy:
        raise ValueError("Versioned mortality policy required")
    if type(profiles) is not dict:
        raise ValueError("Explicit occupant profiles required")
    ledger._bound(db, world_id, require_transaction=True)
    row = db.execute(
        "SELECT world_id,version,state_json FROM main.atm_state WHERE id=1"
    ).fetchone()
    if row is None or row["world_id"] != world_id:
        raise RuntimeError("World atmosphere not enabled")
    if row["version"] != expected_version:
        raise ValueError("Stale atmosphere snapshot")
    model = _rehydrate(_read(row["state_json"], _MAX_STATE_BYTES))
    if set(profiles) != set(model.occupants):
        raise ValueError("Every occupant needs one explicit exposure profile")
    proposals = []
    for actor, occupant in sorted(model.occupants.items()):
        profile = profiles[actor]
        if type(profile) is not ExposureProfile:
            raise ValueError("Unverified occupant exposure profile")
        if occupant["sealed_suit"]:
            continue  # Only the kernel's nominal sealed-suit assumption.
        room = model.compartments[occupant["compartment"]]
        volume = room["volume_l"]
        pressure = room["gas_pa_l"] // volume
        oxygen_partial = room["oxygen_pa_l"] // volume
        candidates = []
        if pressure < profile.min_pressure_pa:
            candidates.append(profile.pressure_injury)
        if oxygen_partial < profile.min_oxygen_pa:
            candidates.append(profile.oxygen_injury)
        for cause, severity in candidates:
            proposals.append(injure(
                world_id=world_id, character_id=actor, cause=cause,
                severity=severity, occurred_at=at_utc, policy=mortality_policy))
    return ExposureDecision(
        status="HOST_REVIEW_REQUIRED", atmosphere_version=expected_version,
        proposals=tuple(proposals), event_id=event_id,
        source_state_sha256=hashlib.sha256(row["state_json"].encode("utf-8")).hexdigest(),
        mortality_policy_hash=mortality_policy.policy_hash,
        exposure_policy_hash=hashlib.sha256(json.dumps({
            "schema": "realm.exposure.occupant_profiles.v1",
            "profiles": [[actor, profiles[actor].profile_hash]
                         for actor in sorted(profiles)]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")).hexdigest(),
    )
