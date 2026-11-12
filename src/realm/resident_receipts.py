"""Atomic host-only receipts for direct, trusted resident actions.

The host selects an actor and action ID; an untrusted model selects neither.
Use the same host-owned actor principal as ResidentTurnHost so distinct host
adapters cannot accidentally perform one resident action ID twice.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from typing import Callable, Mapping

from .engine import Engine, _DIRECTIONS, _LONG_DIRECTIONS


_READ_VERBS = frozenset({
    "look", "l", "examine", "x", "where", "location", "inventory", "inv",
    "i", "who", "history", "listen", "help", "?", "quit", "exit",
})
# Same grammar as ResidentTurnHost's stable action IDs. No Unicode or spaces:
# both host adapters must agree on which receipt identities are usable.
HOST_ACTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)


def replay_resident_if_committed(
    engine: Engine, resident: str, action_id: str,
    validate: Callable[[Mapping[str, object]], object],
    writer_guard: Callable[[sqlite3.Connection], None] | None,
) -> str | None:
    """Safely answer a prior host action without asking a model again.

    Check actor membership, current policy, and the optional writer authority
    in one BEGIN IMMEDIATE transaction. An absent receipt returns None only
    after denying ambiguous legacy/player claims. A subsequent first commit
    still makes its own definitive receipt check after model inference.
    """
    # This is a pre-inference/replay *probe*, never a write commit. A host
    # writer_guard may contain DML even though its intended role is checking
    # authorization. Always roll back its transaction, including on cache
    # hits, misses, or exceptions, so a failed model request cannot leave
    # unreceipted effects in the world ledger.
    with closing(engine._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            # BEGIN IMMEDIATE first ensures a stable admission snapshot. Turn
            # this connection read-only *after* acquiring its writer lock so
            # a mistaken guard UPDATE cannot influence a transient look or
            # escape on an otherwise successful retry.
            db.execute("PRAGMA query_only=ON")
            if writer_guard is not None:
                writer_guard(db)
            player = db.execute(
                "SELECT id,room_id FROM main.players WHERE id=? COLLATE NOCASE", (resident,)
            ).fetchone()
            if player is None:
                raise ValueError("Join this REALM before issuing resident commands")
            actor = player["id"]
            prior = db.execute(
                "SELECT command,response FROM main.action_receipts WHERE actor=? AND action_id=?",
                ("@resident:" + actor, action_id),
            ).fetchone()
            if prior is None:
                if db.execute(
                    "SELECT 1 FROM main.action_receipts WHERE actor=? AND action_id=?",
                    (actor, action_id),
                ).fetchone() is not None:
                    raise ValueError(
                        "unverified legacy/player receipt blocks resident action; "
                        "owner must review provenance and issue a governed new action ID"
                    )
                return None
            # No model may change a committed receipt on retry. The *current*
            # host policy must nevertheless authorize that exact stored command.
            validate({"command": prior["command"]})
            verb, _, arg = prior["command"].partition(" ")
            verb, arg = verb.casefold(), arg.strip()
            if verb in _DIRECTIONS:
                arg, verb = _DIRECTIONS[verb], "go"
            elif verb in _LONG_DIRECTIONS:
                arg, verb = verb, "go"
            if verb not in _READ_VERBS and verb != "say":
                from .combat import Combat
                # A cached host receipt cannot grant post-death world effects.
                Combat.require_living_for_mutation(db, actor)
            if verb in _READ_VERBS:
                return engine._perform(db, actor, player["room_id"], verb, arg)
            return prior["response"]
        finally:
            db.rollback()


def commit_resident_command(
    engine: Engine, resident: str, command: str, action_id: str,
    writer_guard: Callable[[sqlite3.Connection], None] | None,
) -> str:
    """Commit under a principal that no joined player can impersonate.

    The old actor-keyed resident receipts and ordinary player receipts are
    indistinguishable. A collision without a host receipt therefore fails
    closed rather than trusting, deleting, or silently migrating legacy data.
    """
    verb, _, arg = command.partition(" ")
    verb, arg = verb.casefold(), arg.strip()
    if verb in _DIRECTIONS:
        arg, verb = _DIRECTIONS[verb], "go"
    elif verb in _LONG_DIRECTIONS:
        arg, verb = verb, "go"
    read_only = verb in _READ_VERBS

    with closing(engine._connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if writer_guard is not None:
            writer_guard(db)
        player = db.execute(
            "SELECT id,room_id FROM main.players WHERE id=? COLLATE NOCASE", (resident,)
        ).fetchone()
        if player is None:
            raise ValueError("Join this REALM before issuing resident commands")
        actor, room_id = player["id"], player["room_id"]
        if not read_only and verb != "say":
            from .combat import Combat
            Combat.require_living_for_mutation(db, actor)
        principal = "@resident:" + actor
        prior = db.execute(
            "SELECT command,response FROM main.action_receipts WHERE actor=? AND action_id=?",
            (principal, action_id),
        ).fetchone()
        if prior is None and db.execute(
            "SELECT 1 FROM main.action_receipts WHERE actor=? AND action_id=?",
            (actor, action_id),
        ).fetchone() is not None:
            raise ValueError(
                "unverified legacy/player receipt blocks resident action; "
                "owner must review provenance and issue a governed new action ID"
            )
        if prior is not None:
            if prior["command"] != command:
                raise ValueError("Action ID already used for a different command")
            if not read_only:
                return prior["response"]
        response = engine._perform(db, actor, room_id, verb, arg)
        if prior is None:
            db.execute(
                "INSERT INTO main.action_receipts(actor,action_id,command,response) "
                "VALUES(?,?,?,?)",
                (principal, action_id, command, "" if read_only else response),
            )
        return response
