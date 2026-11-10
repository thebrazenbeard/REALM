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

from .engine import Engine, _DIRECTIONS, _LONG_DIRECTIONS
from .npc_local_model import LocalChatProposer
from .npc_scene import ResidentScene, ResidentSceneBuilder
from .residents import ResidentController


_RECEIPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_MAX_VIEW_CHARS = 6000
_HARD_MAX_TURNS = 8
# Receipt keys are (actor, action_id). Player identities cannot contain '@',
# so a bearer-authenticated player can never write this host-owned actor key
# through Engine.act, even if the player guesses the exact schedule action ID.
_RESIDENT_RECEIPT_PREFIX = "@resident:"
_READ_VERBS = frozenset({
    "look", "l", "examine", "x", "where", "location", "inventory", "inv",
    "i", "who", "history", "listen", "help", "?", "quit", "exit",
})


@dataclass(frozen=True)
class ResidentTurn:
    """One host-selected resident, scoped scene, and logical action identity."""

    controller: ResidentController
    host_view: str
    action_id: str


@dataclass(frozen=True)
class ScopedResidentTurn:
    """Host-selected actor and receipt; view is built by REALM, not supplied."""

    controller: ResidentController
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
        """Legacy host-supplied view path; caller owns scene privacy policy."""
        return self._run_prepared(self._preflight(turns))

    def run_scoped_tick(
        self, turns: Sequence[ScopedResidentTurn], *, speech_events: int = 8,
    ) -> tuple[ResidentTurnResult, ...]:
        """Build every model view from authoritative same-world room state.

        This is the opt-in privacy-scoped path. The host supplies only a
        resident controller and stable receipt ID, not an arbitrary prompt.
        Revalidation occurs within Engine.act's BEGIN IMMEDIATE transaction.
        """
        if not isinstance(turns, (tuple, list)) or not 1 <= len(turns) <= self.max_turns:
            raise ValueError("Resident tick batch must contain 1–max_turns entries")
        candidates: list[ResidentTurn] = []
        for turn in turns:
            if not isinstance(turn, ScopedResidentTurn):
                raise TypeError("Scoped tick entries must be ScopedResidentTurn values")
            candidates.append(ResidentTurn(turn.controller, "host-scoped scene", turn.action_id))
        prepared = self._preflight(candidates)
        builder = ResidentSceneBuilder(self.engine, speech_events=speech_events)
        # Resolve all host scenes before starting any inference or gameplay
        # mutation; a bad later actor cannot partially commit earlier turns.
        scenes = tuple(builder.build(turn.controller) for turn, _ in prepared)
        return self._run_prepared(prepared, scenes=scenes, builder=builder)

    def _run_prepared(
        self, prepared: list[tuple[ResidentTurn, str]], *,
        scenes: tuple[ResidentScene, ...] | None = None,
        builder: ResidentSceneBuilder | None = None,
    ) -> tuple[ResidentTurnResult, ...]:
        """Apply receipts and proposals; optional scene guard is writer-owned."""
        results: list[ResidentTurnResult] = []
        for index, (turn, actor) in enumerate(prepared):
            scene = scenes[index] if scenes is not None else None
            if scene is not None:
                if builder is None:
                    raise RuntimeError("Scoped scene requires its host-owned builder")
                builder.assert_current(scene)
                host_view = scene.view
                writer_guard = lambda db, scene=scene: builder.assert_current_in_connection(scene, db)
            else:
                host_view = turn.host_view
                writer_guard = None
            with closing(self.engine._connect()) as db:
                stored = db.execute(
                    "SELECT command FROM main.action_receipts WHERE actor=? AND action_id=?",
                    (_RESIDENT_RECEIPT_PREFIX + actor, turn.action_id),
                ).fetchone()
                legacy = (db.execute(
                    "SELECT 1 FROM main.action_receipts WHERE actor=? AND action_id=?",
                    (actor, turn.action_id),
                ).fetchone() if stored is None else None)
            if legacy is not None:
                # Old host receipts and player-preclaimed IDs are byte-for-byte
                # indistinguishable. Stop before unnecessary model inference;
                # the writer transaction below repeats this check to close
                # a race with a player POST arriving during model inference.
                raise ValueError(
                    "unverified legacy/player receipt blocks resident turn; "
                    "owner must review provenance and issue a governed new action ID"
                )
            if stored is not None:
                # Current host policy is still authoritative. Never replay a
                # previously accepted command merely because receipt exists.
                # In particular, actor-keyed *player* receipts are never
                # admitted as evidence of a host-authorized turn.
                proposal = {"command": stored[0]}
                turn.controller.validate(proposal)
            else:
                proposal = self.proposer.propose(host_view)
            response, replayed = self._commit_host_turn(
                turn.controller, actor, proposal, turn.action_id,
                writer_guard=writer_guard,
                require_existing=stored is not None,
            )
            results.append(ResidentTurnResult(
                actor=actor, action_id=turn.action_id,
                response=response, replayed=replayed,
            ))
        return tuple(results)

    def _commit_host_turn(self, controller: ResidentController, actor: str,
                          proposal: dict, action_id: str, *, writer_guard=None,
                          require_existing: bool = False) -> tuple[str, bool]:
        """Atomically commit a game action and a player-unclaimable host receipt.

        Engine.act opens its own SQLite transaction and writes a receipt under
        the ordinary player actor. Calling it here would allow a client to
        preclaim the ID and impersonate a host replay. Instead, use Engine's
        existing command interpreter within this one BEGIN IMMEDIATE scope.
        No historical player-keyed receipt is deleted, trusted, or rewritten:
        old host receipts cannot be distinguished from player preclaims without
        independently proven migration evidence.

        This internal Engine._perform seam retains the present Engine.act
        command/room transaction rules, but future outer Engine.act policies
        (notably opt-in combat/death mutation guards) must explicitly be
        composed here before enabling those features for resident turns.
        """
        if not isinstance(controller, ResidentController) or controller._engine is not self.engine:
            raise ValueError("Resident controller must use the same host Engine")
        if not isinstance(actor, str) or actor.casefold() != controller._resident.casefold():
            raise ValueError("Resident controller and host actor must identify the same resident")
        if writer_guard is not None and not callable(writer_guard):
            raise TypeError("Host resident writer guard must be callable")
        accepted = controller.validate(proposal)
        command = accepted.command
        verb, _, arg = command.partition(" ")
        verb, arg = verb.casefold(), arg.strip()
        if verb in _DIRECTIONS:
            arg, verb = _DIRECTIONS[verb], "go"
        elif verb in _LONG_DIRECTIONS:
            arg, verb = verb, "go"
        read_only = verb in _READ_VERBS
        receipt_actor = _RESIDENT_RECEIPT_PREFIX + actor
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if writer_guard is not None:
                writer_guard(db)
            player = db.execute(
                "SELECT id,room_id FROM main.players WHERE id=? COLLATE NOCASE", (actor,)
            ).fetchone()
            if player is None or player["id"] != actor:
                raise ValueError("Host resident identity is no longer joined as expected")
            prior = db.execute(
                "SELECT command,response FROM main.action_receipts WHERE actor=? AND action_id=?",
                (receipt_actor, action_id),
            ).fetchone()
            if prior is None and db.execute(
                "SELECT 1 FROM main.action_receipts WHERE actor=? AND action_id=?",
                (actor, action_id),
            ).fetchone() is not None:
                raise ValueError(
                    "unverified legacy/player receipt blocks resident turn; "
                    "owner must review provenance and issue a governed new action ID"
                )
            if require_existing and prior is None:
                raise ValueError("Host turn receipt changed before its replay")
            if prior is not None:
                if prior["command"] != command:
                    raise ValueError("Host action ID already used for a different command")
                if not read_only:
                    return prior["response"], True
            response = self.engine._perform(db, actor, player["room_id"], verb, arg)
            if prior is None:
                # Observation receipts bind ID and command, never stale private
                # room, inventory, or speech content.
                db.execute(
                    "INSERT INTO main.action_receipts(actor,action_id,command,response) "
                    "VALUES(?,?,?,?)",
                    (receipt_actor, action_id, command, "" if read_only else response),
                )
            return response, prior is not None
