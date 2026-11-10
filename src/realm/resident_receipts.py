"""Atomic host-only receipts for direct, trusted resident actions.

The host selects an actor and action ID; an untrusted model selects neither.
Use the same host-owned actor principal as ResidentTurnHost so distinct host
adapters cannot accidentally perform one resident action ID twice.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from typing import Callable

from .engine import Engine, _DIRECTIONS, _LONG_DIRECTIONS


_READ_VERBS = frozenset({
    "look", "l", "examine", "x", "where", "location", "inventory", "inv",
    "i", "who", "history", "listen", "help", "?", "quit", "exit",
})
# Same grammar as ResidentTurnHost's stable action IDs. No Unicode or spaces:
# both host adapters must agree on which receipt identities are usable.
HOST_ACTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)


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
