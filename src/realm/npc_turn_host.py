"""Bounded, explicitly invoked host ticks for optional local model residents.

This is not a background daemon, autonomous scheduler, memory system, identity
provider, or trusted world authority. A host chooses pre-joined residents,
their authorized views and policies, and stable action receipt identifiers.
Untrusted model output is validated by ResidentController and Engine.
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from typing import Sequence

from .engine import Engine
from .npc_local_model import LocalChatProposer
from .residents import ResidentController


_RECEIPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_MAX_VIEW_CHARS = 6000
_HARD_MAX_TURNS = 8


@dataclass(frozen=True)
class ResidentTurn:
    """One host-selected resident, scoped scene, and logical action identity."""

    controller: ResidentController
    host_view: str
    action_id: str


@dataclass(frozen=True)
class ResidentTurnResult:
    """One observed engine response/receipt, not proof of neural inference."""

    actor: str
    action_id: str
    response: str
    replayed: bool


class ResidentTurnHost:
    """Perform at most max_turns host-authorized resident turns on invocation.

    Structural checks precede all inference. A failure stops the batch but
    does not roll back earlier committed actors. Caller retries using the same
    actor and action IDs. Durable SQLite receipts avoid a second model call
    on such a retry after a commit, including across Engine restarts.

    Simultaneous first attempts may infer twice before one receipt commits;
    multi-process inference leases remain a separate host problem.
    """

    def __init__(self, engine: Engine, proposer: LocalChatProposer, *, max_turns: int = 4) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("ResidentTurnHost requires one world Engine")
        if not isinstance(proposer, LocalChatProposer):
            raise TypeError("ResidentTurnHost requires a host-approved local proposer")
        if type(max_turns) is not int or not 1 <= max_turns <= _HARD_MAX_TURNS:
            raise ValueError("Resident tick batch maximum must be 1–8")
        self.engine = engine
        self.proposer = proposer
        self.max_turns = max_turns

    def _preflight(self, turns: Sequence[ResidentTurn]) -> list[tuple[ResidentTurn, str]]:
        if not isinstance(turns, (tuple, list)) or not 1 <= len(turns) <= self.max_turns:
            raise ValueError("Resident tick batch must contain 1–max_turns entries")
        seen: set[str] = set()
        prepared: list[tuple[ResidentTurn, str]] = []
        # This database connection closes before any model HTTP call.
        with closing(self.engine._connect()) as db:
            for turn in turns:
                if not isinstance(turn, ResidentTurn):
                    raise TypeError("Resident tick entries must be ResidentTurn values")
                controller = turn.controller
                if not isinstance(controller, ResidentController) or controller._engine is not self.engine:
                    raise ValueError("Resident turn controller must belong to the same Engine")
                if not isinstance(turn.action_id, str) or _RECEIPT_RE.fullmatch(turn.action_id) is None:
                    raise ValueError("Host action receipt must be 1–128 safe ASCII characters")
                if (
                    not isinstance(turn.host_view, str)
                    or not 1 <= len(turn.host_view) <= _MAX_VIEW_CHARS
                    or not all(c.isprintable() or c in "\n\r\t" for c in turn.host_view)
                ):
                    raise ValueError("Host view must be 1–6000 printable characters")
                name = controller._resident
                if name.casefold() in seen:
                    raise ValueError("Host tick permits one turn per resident")
                seen.add(name.casefold())
                row = db.execute("SELECT id FROM players WHERE id=? COLLATE NOCASE", (name,)).fetchone()
                if row is None:
                    raise ValueError("Host must have joined the resident before inference")
                prepared.append((turn, row[0]))
        return prepared

    def run_tick(self, turns: Sequence[ResidentTurn]) -> tuple[ResidentTurnResult, ...]:
        """Make bounded proposals and commit through Engine in input order."""
        prepared = self._preflight(turns)
        results: list[ResidentTurnResult] = []
        for turn, actor in prepared:
            with closing(self.engine._connect()) as db:
                stored = db.execute(
                    "SELECT command FROM action_receipts WHERE actor=? COLLATE NOCASE AND action_id=?",
                    (actor, turn.action_id),
                ).fetchone()
            if stored is not None:
                # Current host policy is still authoritative. Never replay a
                # previously accepted command merely because receipt exists.
                proposal = {"command": stored[0]}
                turn.controller.validate(proposal)
                response = turn.controller.commit(proposal, action_id=turn.action_id)
                replayed = True
            else:
                proposal = self.proposer.propose(turn.host_view)
                response = turn.controller.commit(proposal, action_id=turn.action_id)
                replayed = False
            results.append(ResidentTurnResult(
                actor=actor, action_id=turn.action_id,
                response=response, replayed=replayed,
            ))
        return tuple(results)
