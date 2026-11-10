"""Host-gated, model-neutral action proposals for prospective REALM residents.

This module performs no inference or autonomous scheduling. A trusted host
creates one ResidentController per pre-joined character, provides a unique
action receipt ID, and decides when to submit an untrusted proposal. The
proposal cannot choose an actor or bypass the Engine's transactions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Mapping

from .engine import Engine


_ENGINE_VERBS = frozenset({
    "look", "l", "where", "location", "go", "n", "s", "e", "w", "u", "d",
    "north", "south", "east", "west", "up", "down", "say", "who",
    "inventory", "inv", "i", "take", "drop", "history", "listen",
})
_SAFE_DEFAULT = frozenset({
    "look", "l", "go", "n", "s", "e", "w", "u", "d",
    "north", "south", "east", "west", "up", "down", "say",
})


@dataclass(frozen=True)
class ResidentProposal:
    """One untrusted requested command, without player or receipt authority."""

    command: str

    @classmethod
    def from_untrusted(cls, payload: Mapping[str, object]) -> "ResidentProposal":
        if not isinstance(payload, Mapping) or set(payload) != {"command"}:
            raise ValueError("Resident proposal must contain only a command")
        command = payload["command"]
        if not isinstance(command, str) or not command or len(command) > 512:
            raise ValueError("Resident command must be 1–512 characters")
        if command != command.strip() or not all(c.isprintable() for c in command):
            raise ValueError("Resident commands must be single-line printable text")
        return cls(command)


class ResidentController:
    """A trusted, actor-bound host adapter; not a model or authorization service.

    The host must explicitly provision the resident's identity with
    ``Engine.join`` before enabling this controller, and must authenticate
    requests to create/use the controller. The adapter is not exposed over
    REALM's player HTTP gateway and does not mint player bearer credentials.
    """

    def __init__(
        self, engine: Engine, resident: str, *,
        allowed_verbs: frozenset[str] = _SAFE_DEFAULT,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("ResidentController needs a REALM Engine")
        if not isinstance(resident, str) or not resident:
            raise ValueError("Host must provide a resident identity")
        if not isinstance(allowed_verbs, frozenset) or not allowed_verbs:
            raise ValueError("Host must provide a nonempty frozen verb policy")
        if any(not isinstance(v, str) or v not in _ENGINE_VERBS for v in allowed_verbs):
            raise ValueError("Resident policy contains an unsupported engine verb")
        self._engine = engine
        self._resident = resident
        self._allowed_verbs = allowed_verbs

    def validate(self, proposal: Mapping[str, object]) -> ResidentProposal:
        """Host-side parse and command policy check; no world mutation."""
        parsed = ResidentProposal.from_untrusted(proposal)
        verb, _, arg = parsed.command.partition(" ")
        if verb.casefold() not in self._allowed_verbs:
            raise ValueError("Resident command is not authorized by host policy")
        # Reject malformed speech before calling Engine.act. Its own movement,
        # room, item, world rules and transaction logic remain authoritative.
        if verb.casefold() == "say" and (not arg.strip() or len(arg) > 280):
            raise ValueError("Resident speech must contain 1–280 characters")
        return parsed

    def commit(
        self, proposal: Mapping[str, object], *, action_id: str,
        writer_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> str:
        """PROPOSE → host VALIDATE → Engine COMMIT with host-owned receipt ID."""
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
            raise ValueError("Host action ID must contain 1–128 characters")
        if writer_guard is not None and not callable(writer_guard):
            raise TypeError("Resident writer_guard must be callable")
        accepted = self.validate(proposal)
        return self._engine.act(
            self._resident, accepted.command, action_id=action_id,
            writer_guard=writer_guard,
        )
