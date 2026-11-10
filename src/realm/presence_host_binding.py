"""Explicit host-owned Engine witness attachment for bound local listeners.

Multiple authenticated listeners may share an Engine and one witness. A
failed socket bind must never attach any witness, and the final listener
closure must release its host-owned attachment. The lock is never acquired
from a combat SQLite writer and performs no database I/O.
"""

from __future__ import annotations

import threading

from .engine import Engine
from .local_presence import LocalPresenceWitness


_BINDING_LOCK = threading.Lock()


def bind_host_engine(engine: Engine, witness: LocalPresenceWitness) -> None:
    with _BINDING_LOCK:
        current = getattr(engine, "_local_presence_witness", None)
        if current is not None and current is not witness:
            raise ValueError("Cannot replace another live-world presence witness")
        count = getattr(engine, "_local_presence_owner_count", 0)
        engine._local_presence_witness = witness
        engine._local_presence_owner_count = count + 1


def unbind_host_engine(engine: Engine, witness: LocalPresenceWitness) -> None:
    with _BINDING_LOCK:
        if getattr(engine, "_local_presence_witness", None) is not witness:
            return
        count = getattr(engine, "_local_presence_owner_count", 0)
        if count <= 1:
            delattr(engine, "_local_presence_witness")
            if hasattr(engine, "_local_presence_owner_count"):
                delattr(engine, "_local_presence_owner_count")
        else:
            engine._local_presence_owner_count = count - 1
