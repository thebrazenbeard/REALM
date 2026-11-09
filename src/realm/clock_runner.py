"""Opt-in foreground REALM UTC event pump; no service or implicit actors.

Only a trusted local host may supply event-kind handlers. An event's payload
never grants actor or command authority; unknown kinds abort the whole tick.
No process starts at import, no OS schedule/service, network or model inference.
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from .world_clock import DueEvent, WorldClock


class UnknownEventKind(RuntimeError):
    """Scheduled event has no trusted host handler; receipt must stay pending."""


class _TrustedRegistryDispatcher:
    """Preserve whole-transaction hooks of explicitly registered host owners.

    WorldClock invokes begin_host_tick before *any* selected callback and
    verify_final_precommit after *all* receipts/effect checks. A plain nested
    dispatch function hides these object attributes, making opted-in custodial
    handlers (such as atmosphere) unusable or unable to attest their effects.
    Nothing is discovered, instantiated, or enabled implicitly here.
    """

    def __init__(self, registry: dict[str, Callable]):
        self._registry = registry
        self._custodians: list[tuple[Callable, Callable]] = []
        seen: set[int] = set()
        for handler in registry.values():
            identity = id(handler)
            if identity in seen:
                continue
            seen.add(identity)
            begin = getattr(handler, "begin_host_tick", None)
            final = getattr(handler, "verify_final_precommit", None)
            if begin is None and final is None:
                continue
            if not callable(begin) or not callable(final):
                raise ValueError("Registered host custody handler requires both callable tick hooks")
            self._custodians.append((begin, final))

    def begin_host_tick(self, db, due_events: tuple[DueEvent, ...]) -> None:
        for begin, _ in self._custodians:
            begin(db, due_events)

    def __call__(self, db, event: DueEvent) -> object:
        handler = self._registry.get(event.kind)
        if handler is None:
            raise UnknownEventKind(f"Unregistered event kind {event.kind!r}")
        return handler(db, event)

    def verify_final_precommit(self, db, processed: tuple[DueEvent, ...]) -> None:
        # A later registered custodian must not mutate SQLite after an earlier
        # custodian's successful final attestation. WorldClock calls this hook
        # after its own receipt/effect checks, so any extra DML here could
        # otherwise yield a durable false UTC ACK. Scope query_only to the
        # entire finalizer phase and restore its prior setting even on failure.
        previous = db.execute("PRAGMA query_only").fetchone()[0]
        changes_before = db.total_changes
        try:
            db.execute("PRAGMA query_only=ON")
            for _, final in self._custodians:
                try:
                    final(db, processed)
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode == sqlite3.SQLITE_READONLY:
                        raise RuntimeError("Host UTC finalizers must remain read-only") from exc
                    raise
                # A trusted callback could itself toggle query_only. Do not
                # silently accept any committed DML during final attestation.
                if (db.total_changes != changes_before
                        or db.execute("PRAGMA query_only").fetchone()[0] != 1):
                    raise RuntimeError("Host UTC finalizers must remain read-only")
        finally:
            db.execute("PRAGMA query_only=ON" if previous else "PRAGMA query_only=OFF")


@dataclass(frozen=True)
class PumpReport:
    pulses: int
    event_ids: tuple[str, ...]
    reason: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
def run_foreground(
    clock: WorldClock,
    handlers: Mapping[str, Callable],
    *,
    pulses: int,
    interval_seconds: float = 1.0,
    batch_limit: int = 100,
    now: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop: Callable[[], bool] | None = None,
    acknowledge_forward_jump: bool = False,
) -> PumpReport:
    """Run bounded trusted UTC ticks in the caller's foreground.

    Monotonic time controls pacing only; persisted state/deadlines remain UTC.
    Any handler/clock exception propagates, so WorldClock.tick rolls back.
    """
    if not isinstance(clock, WorldClock):
        raise TypeError("A bound WorldClock instance is required")
    if type(pulses) is not int or not 1 <= pulses <= 1000:
        raise ValueError("pulses must be an integer in 1..1000")
    if (type(interval_seconds) not in (int, float)
            or not math.isfinite(interval_seconds)
            or not 0.01 <= interval_seconds <= 3600):
        raise ValueError("interval_seconds must be finite in 0.01..3600")
    if type(batch_limit) is not int or not 1 <= batch_limit <= 1000:
        raise ValueError("batch_limit must be an integer in 1..1000")
    if pulses * batch_limit > 100_000:
        raise ValueError("One foreground run may process at most 100000 events")
    if not isinstance(handlers, Mapping) or not handlers:
        raise ValueError("A nonempty trusted event-kind handler registry is required")
    registry = dict(handlers)
    if any(not isinstance(kind, str) or not kind or not callable(handler)
           for kind, handler in registry.items()):
        raise ValueError("Every registered event kind requires a callable handler")
    if (not callable(now) or not callable(monotonic) or not callable(sleep)
            or (stop is not None and not callable(stop))):
        raise ValueError("Clock, pacing and stop providers must be callable")

    dispatch = _TrustedRegistryDispatcher(registry)
    began = monotonic()
    if type(began) not in (int, float) or not math.isfinite(began):
        raise ValueError("Monotonic provider must return finite seconds")
    completed = 0
    event_ids = []
    for index in range(pulses):
        if stop is not None and stop():
            return PumpReport(completed, tuple(event_ids), "stopped")
        if index:
            target = began + index * interval_seconds
            delay = target - monotonic()
            if not math.isfinite(delay):
                raise ValueError("Monotonic clock yielded an invalid interval")
            if delay > 0:
                sleep(delay)
        if stop is not None and stop():
            return PumpReport(completed, tuple(event_ids), "stopped")
        processed = clock.tick(
            now(), dispatch, limit=batch_limit,
            acknowledge_forward_jump=acknowledge_forward_jump,
        )
        event_ids.extend(event.event_id for event in processed)
        completed += 1
    return PumpReport(completed, tuple(event_ids), "complete")
