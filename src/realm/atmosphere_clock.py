"""Explicit host-only UTC atmosphere effects with final SQLite custody.

No scheduler, service, gateway route, world creation or player permission is
installed here. The host must enable WorldClock and AtmosphereLedger for the
same synthetic/approved world, schedule `atmosphere_tick` events, and supply
this handler to WorldClock.tick. It is not a real decompression solver.
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from .atmosphere_ledger import AtmosphereLedger, _world_authority
from .world_clock import DueEvent


class AtmosphereUTCHandler:
    """Route optional atmosphere ticks and attest their final batch effects."""

    def __init__(self, ledger: AtmosphereLedger, *, world_id: str,
                 other_handler: Callable[[sqlite3.Connection, DueEvent], object] | None = None):
        if type(ledger) is not AtmosphereLedger or type(world_id) is not str or not world_id:
            raise ValueError("Bound atmosphere ledger and world ID required")
        if other_handler is not None and not callable(other_handler):
            raise ValueError("Other host handler must be callable")
        self.ledger = ledger
        self.world_id = world_id
        self.other_handler = other_handler
        self._db = None
        self._expected = None
        self._scheduled = ()
        self._seen = []

    def _evidence(self, db: sqlite3.Connection):
        self.ledger._bound(db, self.world_id, require_transaction=True)
        state = db.execute(
            "SELECT world_id,version,state_json FROM main.atm_state WHERE id=1"
        ).fetchone()
        if state is None or state["world_id"] != self.world_id:
            raise RuntimeError("Atmosphere world state is not enabled")
        receipts = {
            row["action_id"]: (row["world_id"], row["command_json"], row["result_json"])
            for row in db.execute(
                "SELECT action_id,world_id,command_json,result_json FROM main.atm_receipts"
            )
        }
        return (tuple(state), receipts, _world_authority(db))

    def begin_host_tick(self, db: sqlite3.Connection, due_events: tuple[DueEvent, ...]):
        """Capture BEFORE any selected clock handler can mutate atmosphere."""
        self._db = db
        self._scheduled = tuple(e.event_id for e in due_events
                                if e.kind == "atmosphere_tick")
        self._seen = []
        # Clock/state receipt DML and unrelated host callbacks can tamper with
        # earlier atmosphere actions even when this batch has NO new atmosphere
        # event (or no due event at all). An explicitly selected atmosphere
        # handler therefore keeps custody throughout every host UTC batch.
        self._expected = self._evidence(db)

    def __call__(self, db: sqlite3.Connection, event: DueEvent):
        if event.kind != "atmosphere_tick":
            if self.other_handler is None:
                raise ValueError("No host handler for non-atmosphere event")
            return self.other_handler(db, event)
        if db is not self._db or self._expected is None or event.event_id not in self._scheduled:
            raise RuntimeError("Atmosphere event outside prepared host UTC tick")
        if event.event_id in self._seen:
            raise ValueError("Duplicate atmosphere event in one UTC tick")
        if type(event.payload) is not dict or set(event.payload) != {"expected_version"}:
            raise ValueError("Atmosphere UTC event needs exact expected_version")
        if self._evidence(db) != self._expected:
            raise ValueError("Atmosphere effect changed before authorized UTC action")
        result = self.ledger.apply_in_transaction(
            db, world_id=self.world_id, action_id=event.event_id,
            operation="tick", params={}, expected_version=event.payload["expected_version"],
        )
        self._expected = self._evidence(db)
        self._seen.append(event.event_id)
        return result

    def verify_final_precommit(self, db: sqlite3.Connection,
                               processed: tuple[DueEvent, ...]):
        """WorldClock invokes this after all callbacks and receipts, before COMMIT."""
        if db is not self._db:
            raise RuntimeError("Atmosphere finalizer bound to wrong UTC transaction")
        if self._scheduled and tuple(self._seen) != self._scheduled:
            raise ValueError("Missing authorized atmosphere UTC effect")
        if self._evidence(db) != self._expected:
            raise ValueError("Atmosphere postcondition changed before UTC commit")
        self._db = None
        self._expected = None
        self._scheduled = ()
        self._seen = []
