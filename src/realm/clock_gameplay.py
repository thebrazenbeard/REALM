"""Host-only REALM actions inside an existing WorldClock.tick SQLite transaction.

Do not invoke Engine.act() or Crafting.perform() from a tick callback: both
open a second connection and BEGIN IMMEDIATE, deadlocking on the clock lock.
This adapter uses the same tested domain transitions and shared receipt table
without granting NPC autonomy, player authentication, or world-release rights.
The host must bind a validated actor and command from trusted policy; the
presence of an event in a clock queue is not proof of permission.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .crafting import Crafting, _MAX_BATCHES, _name
from .engine import Engine, _DIRECTIONS, _LONG_DIRECTIONS


_ENGINE_ACTIONS = frozenset({"go", "take", "drop", "say"})
_CRAFT_ACTIONS = frozenset({"gather", "craft", "build"})


def _economy_rows(db: sqlite3.Connection, table: str, keys: tuple[str, ...]) -> dict:
    """Bound complete main-schema records, including other actors and rooms."""
    return {
        tuple(row[key] for key in keys): tuple(row)
        for row in db.execute(f"SELECT * FROM main.{table}")
    }


def _gather_evidence(db: sqlite3.Connection) -> dict[str, dict]:
    """Only the six persistence families a gather is allowed to touch/read."""
    return {
        "nodes": _economy_rows(db, "gathering_nodes", ("room_id", "material")),
        "pouches": _economy_rows(db, "material_pouches", ("actor", "material")),
        "goods": _economy_rows(db, "crafted_goods", ("actor", "good")),
        "structures": _economy_rows(db, "built_structures", ("id",)),
        "recipes": _economy_rows(db, "craft_recipes", ("id",)),
        "events": _economy_rows(db, "events", ("id",)),
        "receipts": _economy_rows(db, "action_receipts", ("actor", "action_id")),
    }


def _expected_gather(nodes: dict, room: str, arg: str):
    """Use Crafting's validated command grammar and pre-action stock/yield."""
    material, _, maybe_batches = arg.rpartition(" ")
    if maybe_batches.isdecimal():
        batches = int(maybe_batches)
        if not 1 <= batches <= _MAX_BATCHES:
            return None
    else:
        material, batches = arg, 1
    try:
        material = _name(material)
    except ValueError:
        return None
    node = nodes.get((room, material))
    if node is None or node[2] == 0:
        return None
    return material, min(node[2], batches * node[3])


def _verify_gather_effect(db: sqlite3.Connection, before: dict[str, dict],
                          domain: dict[str, dict],
                          *, actor: str, room: str, arg: str, action_id: str,
                          command: str, response: str) -> None:
    """A legacy gather grants only typed stock→pouch→event→receipt deltas.

    The realm clock's older host_action callback may be followed by installed
    SQLite receipt triggers. Verify their full economics before releasing this
    SAVEPOINT, including unrelated actors, sites, historical rows and receipts.
    Ordinary unrelated room `say` events remain an explicit separate lane.
    """
    after = _gather_evidence(db)
    # A late receipt trigger might DELETE the genuine gather event and INSERT
    # a semantically identical replacement with a new event ID. Preserve the
    # exact domain-emitted rows captured BEFORE receipt-trigger callbacks.
    if any(after["events"].get(key) != row
           for key, row in domain["events"].items()):
        raise ValueError("Gather history was replaced after domain execution")
    domain_event_max = max((key[0] for key in domain["events"]), default=0)
    if any(row[4] != "say" or row[0] <= domain_event_max
           for key, row in after["events"].items()
           if key not in domain["events"]):
        raise ValueError("Receipt trigger inserted an ungranted gameplay event")
    expected = {name: dict(rows) for name, rows in before.items()}
    key = (actor, action_id)
    if key in expected["receipts"]:
        raise ValueError("Gather receipt unexpectedly preexists")
    expected["receipts"][key] = (actor, action_id, command, response)
    planned = _expected_gather(before["nodes"], room, arg)
    new_events = {
        event_id: row for event_id, row in after["events"].items()
        if event_id not in before["events"]
    }
    old_event_ids = set(before["events"])
    if any(after["events"].get(key) != row
           for key, row in before["events"].items()):
        raise ValueError("Gather changed prior gameplay history")
    # SAY is an unrelated permitted event family; it must not serve as a
    # surrogate for the one typed gather history row or disguise an extra one.
    new_gathers = [row for row in new_events.values() if row[4] != "say"]
    if (any(row[0] <= max((key[0] for key in old_event_ids), default=0)
            for row in new_events.values())
            or len(new_gathers) != (1 if planned else 0)):
        raise ValueError("Gather produced ungranted gameplay history")
    if planned is not None:
        material, amount = planned
        if response != f"You gather {material} x{amount}.":
            raise ValueError("Gather response conflicts with authorized stock")
        node = before["nodes"][(room, material)]
        expected["nodes"][(room, material)] = (room, material, node[2] - amount, node[3])
        previous = before["pouches"].get((actor, material))
        expected["pouches"][(actor, material)] = (
            actor, material, (previous[2] if previous else 0) + amount)
        event = new_gathers[0]
        if (event[2:5] != (actor, room, "gather")
                or event[5] != json.dumps({"material": material, "amount": amount},
                                         sort_keys=True)):
            raise ValueError("Gather history is not bound to the actual stock grant")
    for name in ("nodes", "pouches", "goods", "structures", "recipes", "receipts"):
        if after[name] != expected[name]:
            raise ValueError("Gather economic/receipt effect diverged")


def _guard_connection(db: sqlite3.Connection, engine: Engine, crafting: Crafting | None) -> None:
    """Reject a wrong-world, unbound, or nontransactional connection."""
    if not isinstance(db, sqlite3.Connection):
        raise TypeError("Expected a SQLite connection")
    if not isinstance(engine, Engine) or (crafting is not None and
                                          not isinstance(crafting, Crafting)):
        raise TypeError("Expected initialized Engine and optional Crafting")
    if not db.in_transaction:
        raise RuntimeError("Existing SQLite transaction required")
    main_file = next(
        (row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"),
        None,
    )
    if not main_file or Path(main_file).resolve() != engine.db_path.resolve():
        raise ValueError("Connection points to a different world database")
    if crafting is not None and crafting.db_path.resolve() != engine.db_path.resolve():
        raise ValueError("Crafting points to a different world database")
    if db.row_factory is not sqlite3.Row:
        raise ValueError("Connection must use sqlite3.Row row_factory")
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("Shared gameplay requires enabled foreign keys")
    # SQLite resolves unqualified object names from TEMP before main.
    # The legacy Engine/Crafting transition helpers intentionally use bare
    # table names, so do not run them with ANY user-created TEMP objects.
    # This also refuses TEMP triggers/views that could intercept reads/writes.
    if db.execute(
        "SELECT name FROM sqlite_temp_master "
        "WHERE type IN ('table','view','trigger','index') "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None:
        raise RuntimeError("TEMP schema objects cannot shadow authoritative gameplay")
    if db.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='world_meta'"
    ).fetchone() is None or db.execute(
        "SELECT 1 FROM main.world_meta WHERE key='world_id' AND length(value)>0"
    ).fetchone() is None:
        raise RuntimeError("Gameplay requires a bound world")


def perform_in_transaction(
    db: sqlite3.Connection, *, engine: Engine, crafting: Crafting | None,
    actor: str, command: str, action_id: str,
) -> str:
    """Apply a trusted scheduled action without opening or closing a transaction.

    WorldClock.tick owns BEGIN IMMEDIATE, eventual commit, rollback and clock
    receipt. Caller supplies the canonical, externally authenticated actor,
    policy-admitted command and unique action ID (normally clock event ID).
    This is a local SQLite deduplication seam, not a remote authorization gate.
    Unsupported commands fail closed; read-only queries use other APIs.
    """
    _guard_connection(db, engine, crafting)
    if not isinstance(actor, str) or not actor:
        raise ValueError("Actor must be an authenticated player name")
    if not isinstance(command, str):
        raise ValueError("Command must be text")
    if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
        raise ValueError("action_id must be a nonempty string up to 128 characters")
    command = command.strip()
    verb, _, arg = command.partition(" ")
    verb, arg = verb.casefold(), arg.strip()
    if verb in _DIRECTIONS:
        arg, verb = _DIRECTIONS[verb], "go"
    elif verb in _LONG_DIRECTIONS:
        arg, verb = verb, "go"
    if verb not in _ENGINE_ACTIONS | _CRAFT_ACTIONS:
        raise ValueError("Unsupported scheduled gameplay action")
    if verb in _CRAFT_ACTIONS and crafting is None:
        raise ValueError("Crafting is not registered for this world")

    # Even a trigger with RAISE(FAIL) can leave earlier side effects of its
    # failed statement. Protect this entire action when the trusted host catches
    # a callback error and commits a different event in the same outer tick.
    db.execute("SAVEPOINT realm_clock_gameplay")
    try:
        player = db.execute(
            "SELECT id,room_id FROM main.players WHERE id=? COLLATE NOCASE",
            (actor,),
        ).fetchone()
        if player is None:
            raise ValueError("Join this REALM before issuing commands")
        actual_actor, room = player["id"], player["room_id"]
        previous = db.execute(
            "SELECT command,response FROM main.action_receipts WHERE actor=? AND action_id=?",
            (actual_actor, action_id),
        ).fetchone()
        if previous is not None:
            if previous["command"] != command:
                raise ValueError("Action ID already used for a different command")
            response = previous["response"]
        else:
            gather_before = _gather_evidence(db) if verb == "gather" else None
            if verb in _ENGINE_ACTIONS:
                response = engine._perform(db, actual_actor, room, verb, arg)
            elif verb == "gather":
                response = crafting._gather(db, actual_actor, room, arg)
            else:
                response = crafting._make(db, actual_actor, room, verb, arg)
            gather_domain = _gather_evidence(db) if gather_before is not None else None
            db.execute(
                "INSERT INTO main.action_receipts(actor,action_id,command,response) VALUES(?,?,?,?)",
                (actual_actor, action_id, command, response),
            )
            if gather_before is not None:
                _verify_gather_effect(db, gather_before, gather_domain,
                                      actor=actual_actor,
                                      room=room, arg=arg, action_id=action_id,
                                      command=command, response=response)
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT realm_clock_gameplay")
        db.execute("RELEASE SAVEPOINT realm_clock_gameplay")
        raise
    db.execute("RELEASE SAVEPOINT realm_clock_gameplay")
    return response
