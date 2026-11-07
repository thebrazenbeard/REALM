"""Opt-in source-only Hardcore admission fence, not a live tombstone authority.

A fresh, independently VERIFIED attestation must come from a release-bound
external append-only witness that survives restoring the world SQLite backup.
None is installed by this module. Merely injecting a fake verifier can never
qualify real permadeath; production hosts must qualify their own witness.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .mortality import HealthState, MortalityPolicy


class Admission(str, Enum):
    ALLOW = "allow"
    HOLD = "hold_for_independent_review"
    RETIRED = "retired_by_witness"


@dataclass(frozen=True)
class WitnessStatement:
    """Opaque witness claim; proof requires external cryptographic verification."""
    world_id: str
    character_id: str
    mode: str
    policy_hash: str
    nonce: str
    verdict: str  # exactly "clear" or "retired"
    sequence: int
    proof: str


class IndependentWitness(Protocol):
    """Production implementation MUST be outside rollbackable world authority."""

    def attest(self, world_id: str, character_id: str,
               nonce: str) -> WitnessStatement:
        """Fresh independent observation bound to this unpredictable nonce."""

    def verify(self, statement: WitnessStatement) -> bool:
        """Verify authority authenticity and currentness; never host trust boolean."""


@dataclass(frozen=True)
class AdmissionCheck:
    decision: Admission
    reason: str
    witness_sequence: int | None = None


class HardcoreFinalityGate:
    """Read-only player rejoin/action gate; no local or external death writes.

    No assertion about irreversible death is made without a separate witness.
    Call this BEFORE every protected action/join; a one-off check is not a
    sticky session grant. Caller must bind original server mode/policy at host.
    """

    def __init__(self, *, world_id: str, policy: MortalityPolicy,
                 witness: IndependentWitness):
        if not isinstance(world_id, str) or not world_id.strip():
            raise ValueError("invalid Hardcore world_id")
        if not isinstance(policy, MortalityPolicy) or policy.mode != "hardcore":
            raise ValueError("Hardcore witness gate requires Hardcore policy")
        if (witness is None
                or not callable(getattr(witness, "attest", None))
                or not callable(getattr(witness, "verify", None))):
            raise ValueError("independent witness is required")
        self.world_id = world_id
        self.policy_hash = policy.policy_hash
        self.witness = witness

    def check(self, character_id: str, local_state: HealthState) -> AdmissionCheck:
        """Return ALLOW only with fresh external CLEAR and eligible local state.

        A world DB rollback restores ALIVE locally but still yields RETIRED if
        the independently surviving authority carries its terminal tombstone.
        Missing witness, stale signature, invalid schema, or transport outage
        all produce HOLD; never confuse absence with witnessed CLEAR.
        """
        if not isinstance(character_id, str) or not character_id.strip():
            raise ValueError("invalid character_id")
        if not isinstance(local_state, HealthState):
            raise ValueError("invalid local Hardcore state")
        if local_state is HealthState.DEAD_FINAL:
            return AdmissionCheck(Admission.RETIRED, "local_final_state")

        nonce = secrets.token_hex(32)
        try:
            claim = self.witness.attest(self.world_id, character_id, nonce)
            valid = (
                isinstance(claim, WitnessStatement)
                and claim.world_id == self.world_id
                and claim.character_id == character_id
                and claim.mode == "hardcore"
                and claim.policy_hash == self.policy_hash
                and claim.nonce == nonce
                and claim.verdict in ("clear", "retired")
                and type(claim.sequence) is int and claim.sequence > 0
                and isinstance(claim.proof, str) and bool(claim.proof)
            )
            if not valid or self.witness.verify(claim) is not True:
                return AdmissionCheck(Admission.HOLD, "unverified_witness")
        except Exception:
            return AdmissionCheck(Admission.HOLD, "unavailable_witness")

        if claim.verdict == "retired":
            return AdmissionCheck(Admission.RETIRED, "external_tombstone",
                                  claim.sequence)
        if local_state in (HealthState.ALIVE, HealthState.STABILIZED):
            return AdmissionCheck(Admission.ALLOW, "fresh_witness_clear",
                                  claim.sequence)
        return AdmissionCheck(Admission.HOLD, "unresolved_local_mortality",
                              claim.sequence)
