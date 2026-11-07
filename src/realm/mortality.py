"""Experimental pure Casual/Hardcore injury rules, not live permadeath authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class HealthState(str, Enum):
    ALIVE = "alive"
    CRITICAL = "critical"
    STABILIZED = "stabilized"
    PENDING_REVIEW = "mortality_pending_review"
    DEATH_CANDIDATE = "fatal_pending_witness"
    DEAD_FINAL = "dead_final"  # Reserved: requires an external durable tombstone.
    RESPAWN_PENDING = "respawn_pending"


@dataclass(frozen=True)
class MortalityPolicy:
    """Versioned game-balance timings; not clinical prognoses or server authority."""

    mode: str
    policy_id: str
    windows_seconds: Mapping[tuple[str, str], int]
    policy_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.mode not in ("casual", "hardcore"):
            raise ValueError("invalid server mode")
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("invalid mortality policy ID")
        windows: dict[tuple[str, str], int] = {}
        for key, seconds in self.windows_seconds.items():
            if (not isinstance(key, tuple) or len(key) != 2
                    or any(not isinstance(part, str) or not part.strip() for part in key)
                    or isinstance(seconds, bool) or not isinstance(seconds, int)
                    or seconds < 0 or seconds > 366 * 24 * 60 * 60):
                raise ValueError("invalid rescue window")
            windows[key] = seconds
        object.__setattr__(self, "windows_seconds", MappingProxyType(windows))
        material = {
            "schema": "realm.mortality.policy.v1",
            "mode": self.mode, "policy_id": self.policy_id,
            "windows": [[cause, severity, seconds]
                        for (cause, severity), seconds in sorted(windows.items())],
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True).encode("utf-8")
        object.__setattr__(self, "policy_hash", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class Injury:
    world_id: str
    character_id: str
    mode: str
    policy_id: str
    policy_hash: str
    cause: str
    severity: str
    state: HealthState
    incident_utc: datetime
    deadline_utc: datetime
    last_observed_utc: datetime
    receipts: tuple[tuple[str, str, str], ...] = ()

    # This immutable value object is NOT a witnessed or rollback-proof death
    # record. Hardcore permadeath requires an external authoritative tombstone.


def _require_utc(value: datetime) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() != timedelta(0)):
        raise ValueError("UTC-aware datetime required")


def _terminal(mode: str) -> HealthState:
    return HealthState.DEATH_CANDIDATE if mode == "hardcore" else HealthState.RESPAWN_PENDING


def injure(*, world_id: str, character_id: str, cause: str, severity: str,
           occurred_at: datetime, policy: MortalityPolicy) -> Injury:
    """Create an immutable source-neutral injury for authoritative host review."""
    _require_utc(occurred_at)
    if not all(isinstance(x, str) and x.strip()
               for x in (world_id, character_id, cause, severity)):
        raise ValueError("invalid injury identity or profile")
    if not isinstance(policy, MortalityPolicy):
        raise ValueError("invalid mortality policy")
    key = (cause, severity)
    if key not in policy.windows_seconds:
        raise ValueError("unsupported injury profile")
    window = policy.windows_seconds[key]
    try:
        deadline = occurred_at + timedelta(seconds=window)
    except OverflowError as exc:
        raise ValueError("rescue window outside supported UTC range") from exc
    state = HealthState.CRITICAL if window else _terminal(policy.mode)
    return Injury(world_id, character_id, policy.mode, policy.policy_id,
                  policy.policy_hash, cause, severity, state, occurred_at,
                  deadline, occurred_at)


def intervene(injury: Injury, *, at: datetime, action_id: str,
              actor_id: str, resource_id: str, authorized: bool, nearby: bool,
              supply_verified: bool) -> Injury:
    """Apply host-verified treatment only within its versioned rescue window.

    The booleans are promises from an *external* host authority, NOT checks
    of credentials, proximity or actual inventory in this pure module.
    """
    _require_utc(at)
    if authorized is not True:
        raise ValueError("unauthorized medical intervention")
    if not all(isinstance(x, str) and x.strip()
               for x in (action_id, actor_id, resource_id)):
        raise ValueError("invalid treatment action identity")
    if at < injury.last_observed_utc and injury.state is not HealthState.PENDING_REVIEW:
        raise ValueError("out-of-order UTC medical intervention")
    receipt = (action_id, actor_id, resource_id)
    for prior in injury.receipts:
        if prior[0] == action_id:
            if prior != receipt:
                raise ValueError("action_id collision")
            return injury
    # Retried committed actions return their original result even if the
    # medic left the room or the resource has since been consumed. New actions
    # still require fresh host-verified proximity and inventory.
    if nearby is not True or supply_verified is not True:
        raise ValueError("unauthorized or unverified medical intervention")
    if injury.state in (HealthState.DEATH_CANDIDATE, HealthState.DEAD_FINAL,
                        HealthState.RESPAWN_PENDING):
        raise ValueError("terminal character state")
    if injury.state is HealthState.PENDING_REVIEW:
        raise ValueError("mortality pending review")
    if injury.state is not HealthState.CRITICAL:
        raise ValueError("character not in treatable critical state")
    if at >= injury.deadline_utc:
        raise ValueError("rescue deadline expired")
    return replace(injury, state=HealthState.STABILIZED,
                   last_observed_utc=at, receipts=injury.receipts + (receipt,))


def tick(injury: Injury, at: datetime, *, uncertain_clock: bool = False) -> Injury:
    """Deterministically resolve deadlines; this is not a running tick service.

    Host callers MUST use externally verified outage/clock status. If time
    provenance is uncertain at a critical deadline, defer death for review.
    """
    _require_utc(at)
    if at < injury.last_observed_utc:
        raise ValueError("out-of-order UTC tick")
    if injury.state is HealthState.CRITICAL:
        if at >= injury.deadline_utc:
            state = (HealthState.PENDING_REVIEW if uncertain_clock is True
                     else _terminal(injury.mode))
            return replace(injury, state=state, last_observed_utc=at)
        return replace(injury, last_observed_utc=at)
    return injury
