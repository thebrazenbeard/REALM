"""Opt-in, host-invoked cadence selection for local REALM resident proposals.

This module never opens a listener, launches a model, starts a background
worker, or grants an actor game authority by itself. A trusted host selects
pre-joined residents and calls ``run_pulse`` with its own durable pulse index.
The existing scoped scene, host command policy, and SQLite receipt machinery
remain the only route from an untrusted model proposal to a world action.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

from .npc_turn_host import ResidentTurnHost, ResidentTurnResult, ScopedResidentTurn
from .residents import ResidentController


_SCHEDULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z", re.ASCII)
_MAX_RESIDENTS = 32
_MAX_PERIOD = 10000
_MAX_PULSE_INDEX = 2**63 - 1


@dataclass(frozen=True, slots=True)
class ResidentCadence:
    """Host-selected actor with deterministic periodic eligibility.

    Pulse ``n`` selects this actor exactly when ``n % every_pulses == phase``.
    ``phase=0`` permits an immediate first turn at pulse zero. All timing,
    consent, provisioned actors, and repetition policy belong to the host.
    """

    controller: ResidentController
    every_pulses: int
    phase: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.controller, ResidentController):
            raise TypeError("Cadence entry requires one host resident controller")
        if type(self.every_pulses) is not int or not 1 <= self.every_pulses <= _MAX_PERIOD:
            raise ValueError("Resident period must be an integer in 1..10000")
        if type(self.phase) is not int or not 0 <= self.phase < self.every_pulses:
            raise ValueError("Resident phase must be within its pulse period")


class ResidentForegroundSchedule:
    """Select due actors for *one* explicit host pulse, then invoke scoped turns.

    The schedule is an immutable host policy. A repeated pulse reuses the same
    actor/action IDs, allowing existing SQLite receipts to prevent duplicate
    world effects and suppress model inference on committed retries.

    A changed schedule policy must use a new ``schedule_id``. Exact pulse
    monotonicity and clock persistence are host responsibilities; this is not
    an autonomous daemon or crash-resilient inference lease.
    """

    def __init__(
        self,
        host: ResidentTurnHost,
        cadences: Sequence[ResidentCadence],
        *,
        schedule_id: str,
        speech_events: int = 8,
    ) -> None:
        if not isinstance(host, ResidentTurnHost):
            raise TypeError("Schedule requires the host's resident-turn executor")
        if not isinstance(cadences, (tuple, list)) or not 1 <= len(cadences) <= _MAX_RESIDENTS:
            raise ValueError("Schedule must contain 1..32 host-approved residents")
        if not isinstance(schedule_id, str) or _SCHEDULE_ID.fullmatch(schedule_id) is None:
            raise ValueError("Schedule ID must be 1..64 safe ASCII characters")
        if type(speech_events) is not int or not 1 <= speech_events <= 20:
            raise ValueError("Scoped scene speech limit must be 1..20")
        copied = tuple(cadences)
        seen: set[str] = set()
        for cadence in copied:
            if not isinstance(cadence, ResidentCadence):
                raise TypeError("Schedule entries must be ResidentCadence values")
            if cadence.controller._engine is not host.engine:
                raise ValueError("Resident schedule must use controllers from the same Engine")
            name = cadence.controller._resident.casefold()
            if name in seen:
                raise ValueError("Resident schedule contains duplicate case-insensitive actor")
            seen.add(name)
        self.host = host
        self.cadences = copied
        self.schedule_id = schedule_id
        self.speech_events = speech_events

    def run_pulse(self, pulse_index: int) -> tuple[ResidentTurnResult, ...]:
        """Perform only due turns; no due actor means no inference or effect.

        All eligibility and capacity checks happen before any model request.
        Within a nonempty batch, ResidentTurnHost preflights every actor and
        validates each transaction. A later model failure may leave earlier
        committed actors, but exact IDs make retry non-duplicating.
        """
        if type(pulse_index) is not int or not 0 <= pulse_index <= _MAX_PULSE_INDEX:
            raise ValueError("Host pulse index must be a bounded nonnegative integer")
        due = tuple(
            cadence for cadence in self.cadences
            if pulse_index % cadence.every_pulses == cadence.phase
        )
        if len(due) > self.host.max_turns:
            raise ValueError("Due resident count exceeds host pulse capacity")
        if not due:
            return ()
        turns: list[ScopedResidentTurn] = []
        for cadence in due:
            actor_key = cadence.controller._resident.casefold().encode("utf-8")
            suffix = hashlib.sha256(actor_key).hexdigest()[:16]
            receipt = f"npc:{self.schedule_id}:{pulse_index}:{suffix}"
            turns.append(ScopedResidentTurn(cadence.controller, receipt))
        return self.host.run_scoped_tick(turns, speech_events=self.speech_events)
