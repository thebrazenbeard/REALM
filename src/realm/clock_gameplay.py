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
from collections import Counter
from pathlib import Path

from .crafting import Crafting, _MAX_BATCHES, _name
from .engine import Engine, _DIRECTIONS, _LONG_DIRECTIONS


_ENGINE_ACTIONS = frozenset({"go", "take", "drop", "say"})
_CRAFT_ACTIONS = frozenset({"gather", "craft", "build"})
_GATHER_EFFECT_TABLES = frozenset({
    "gathering_nodes", "material_pouches", "crafted_goods",
    "built_structures", "craft_recipes", "events", "action_receipts",
})


def _economy_rows(db: sqlite3.Connection, table: str, keys: tuple[str, ...]) -> dict:
    """Bound complete main-schema records, including other actors and rooms."""
    return {
        tuple(row[key] for key in keys): tuple(row)
        for row in db.execute(f"SELECT * FROM main.{table}")
    }


def _other_world_state(db: sqlite3.Connection) -> tuple[tuple, dict[str, Counter]]:
    """Require every other main-world table/schema to remain unchanged.

    A signed gather has no grant to move players, transfer items, edit maps,
    credentials, ship state, future clock entries or host extension tables.
    Capture duplicates too, not only rows addressable by a known primary key.
    SQLite's internal sequence bookkeeping is deliberately excluded: a valid
    newly emitted gather/event can advance it. Attached databases and external
    host effects are outside this in-world transaction contract.
    """
    schema = tuple(tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ))
    tables = {}
    for kind, name, _owner, _sql in schema:
        if kind != "table" or name in _GATHER_EFFECT_TABLES:
            continue
        quoted_name = '"' + name.replace('"', '""') + '"'
        tables[name] = Counter(tuple(row) for row in db.execute(
            "SELECT * FROM main." + quoted_name))
    return schema, tables


def _allocator_rows(db: sqlite3.Connection) -> dict[str, int]:
    """The SQLite event allocator is state, even though it is internal DDL."""
    if not db.execute("SELECT 1 FROM main.sqlite_schema "
                      "WHERE type='table' AND name='sqlite_sequence'").fetchone():
        return {}
    return dict(db.execute("SELECT name,seq FROM main.sqlite_sequence"))


def _gather_evidence(db: sqlite3.Connection) -> dict[str, dict]:
    """Signed gather deltas plus a complete main-world non-gather guard."""
    return {
        "nodes": _economy_rows(db, "gathering_nodes", ("room_id", "material")),
        "pouches": _economy_rows(db, "material_pouches", ("actor", "material")),
        "goods": _economy_rows(db, "crafted_goods", ("actor", "good")),
        "structures": _economy_rows(db, "built_structures", ("id",)),
        "recipes": _economy_rows(db, "craft_recipes", ("id",)),
        "events": _economy_rows(db, "events", ("id",)),
        "receipts": _economy_rows(db, "action_receipts", ("actor", "action_id")),
        "other_world": _other_world_state(db),
        "allocators": _allocator_rows(db),
    }


def _say_evidence(db: sqlite3.Connection) -> dict[str, object]:
    """Complete source-world snapshot without requiring optional Crafting tables."""
    schema = tuple(tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ))
    tables = {}
    for kind, name, _owner, _sql in schema:
        if kind != "table" or name in ("events", "action_receipts"):
            continue
        quoted_name = '"' + name.replace('"', '""') + '"'
        tables[name] = Counter(tuple(row) for row in db.execute(
            "SELECT * FROM main." + quoted_name))
    return {
        "schema": schema,
        "tables": tables,
        "events": _economy_rows(db, "events", ("id",)),
        "receipts": _economy_rows(db, "action_receipts", ("actor", "action_id")),
        "allocators": _allocator_rows(db),
    }


def _transfer_evidence(db: sqlite3.Connection) -> dict[str, object]:
    """Complete main-world snapshot for a typed go/take/drop effect."""
    schema = tuple(tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ))
    tables = {}
    for kind, name, _owner, _sql in schema:
        if kind != "table" or name in (
            "events", "action_receipts", "players", "items"
        ):
            continue
        quoted_name = '"' + name.replace('"', '""') + '"'
        tables[name] = Counter(tuple(row) for row in db.execute(
            "SELECT * FROM main." + quoted_name))
    return {
        "schema": schema,
        "tables": tables,
        "players": _economy_rows(db, "players", ("id",)),
        "items": _economy_rows(db, "items", ("id",)),
        "events": _economy_rows(db, "events", ("id",)),
        "receipts": _economy_rows(db, "action_receipts", ("actor", "action_id")),
        "allocators": _allocator_rows(db),
    }


def _planned_transfer(db: sqlite3.Connection, before: dict, engine: Engine,
                      actor: str, room: str, verb: str, arg: str) -> tuple:
    """Plan one bound grant from pre-action state, not trigger-tainted rows.

    Item lookup mirrors Engine's SQLite query, including ambiguous names.
    """
    players, items = dict(before["players"]), dict(before["items"])
    events = []
    if verb == "go":
        direction = _DIRECTIONS.get(arg.casefold(), arg.casefold())
        dest = db.execute(
            "SELECT destination FROM main.exits WHERE room_id=? AND direction=?",
            (room, direction),
        ).fetchone()
        if dest is None:
            return f"No exit leads {direction or 'that way'}.", players, items, events
        destination = dest[0]
        player = players[(actor,)]
        players[(actor,)] = (player[0], destination, player[2])
        events = [
            (actor, room, "leave", {"to": destination, "direction": direction}),
            (actor, destination, "move", {"from": room, "direction": direction}),
        ]
        return engine._look(db, destination, actor), players, items, events
    if not arg:
        return ("Take what?" if verb == "take" else "Drop what?"), players, items, events
    if verb == "take":
        item = db.execute(
            "SELECT id,name FROM main.items WHERE room_id=? AND "
            "(id=? COLLATE NOCASE OR name=? COLLATE NOCASE)",
            (room, arg, arg),
        ).fetchone()
        if item is None:
            return "That item is not here.", players, items, events
        original = items[(item["id"],)]
        items[(item["id"],)] = (original[0], original[1], original[2], None, actor)
        events = [(actor, room, "take", {"item": item["id"]})]
        return f"You take {item['name']}.", players, items, events
    item = db.execute(
        "SELECT id,name FROM main.items WHERE held_by=? AND "
        "(id=? COLLATE NOCASE OR name=? COLLATE NOCASE)",
        (actor, arg, arg),
    ).fetchone()
    if item is None:
        return "You are not carrying that item.", players, items, events
    original = items[(item["id"],)]
    items[(item["id"],)] = (original[0], original[1], original[2], room, None)
    events = [(actor, room, "drop", {"item": item["id"]})]
    return f"You drop {item['name']}.", players, items, events


def _verify_transfer_effect(db: sqlite3.Connection, before: dict, domain: dict,
                            *, plan: tuple, actor: str, action_id: str,
                            command: str, response: str,
                            emissions: list[tuple[int, str]]) -> None:
    """Attest typed row/event/receipt/allocator effects before UTC ACK.

    Other selected trusted handlers may emit unrelated events between calls;
    installed triggers cannot add events within this individual grant.
    """
    after = _transfer_evidence(db)
    expected_response, expected_players, expected_items, expected_events = plan
    if response != expected_response:
        raise ValueError("Host action response diverged from bound gameplay effect")
    for stage in (domain, after):
        if stage["schema"] != before["schema"] or stage["tables"] != before["tables"]:
            raise ValueError("Host action changed ungranted world state")
        if stage["players"] != expected_players or stage["items"] != expected_items:
            raise ValueError("Host action changed ungranted player/item state")
    if (domain["receipts"] != before["receipts"]
            or after["events"] != domain["events"]
            or any(domain["events"].get(key) != row
                   for key, row in before["events"].items())):
        raise ValueError("Host action gameplay event history diverged")
    expected_receipts = dict(before["receipts"])
    receipt_key = (actor, action_id)
    if receipt_key in expected_receipts:
        raise ValueError("Host action receipt unexpectedly preexists")
    expected_receipts[receipt_key] = (actor, action_id, command, response)
    if after["receipts"] != expected_receipts:
        raise ValueError("Host action receipt effects diverged")
    added = [row for key, row in domain["events"].items()
             if key not in before["events"]]
    added.sort(key=lambda row: row[0])
    if len(added) != len(expected_events) or len(emissions) != len(expected_events):
        raise ValueError("Host action emitted ungranted gameplay events")
    allocator = before["allocators"].get("events", 0)
    for offset, (row, (source_actor, room, kind, payload)) in enumerate(
        zip(added, expected_events), 1
    ):
        if (row[0:2] != emissions[offset - 1]
                or row[0] != allocator + offset
                or row[2:6] != (source_actor, room, kind,
                                   json.dumps(payload, sort_keys=True))):
            raise ValueError("Host action event timestamp/history is not bound to its typed grant")
    expected_allocators = dict(before["allocators"])
    if added:
        expected_allocators["events"] = allocator + len(added)
    if (domain["allocators"] != expected_allocators
            or after["allocators"] != expected_allocators):
        raise ValueError("Host action changed ungranted event allocation")


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


def _planned_make(before: dict, actor: str, room: str, kind: str, arg: str) -> tuple:
    """Plan a recipe's exact economic grant from pre-domain rows only.

    Source recipes are world-owner configured and already validated by Crafting;
    no trigger-tainted result may authorize an extra output or material debit.
    The construction timestamp is separately captured at its real INSERT.
    """
    pouches = dict(before["pouches"])
    goods = dict(before["goods"])
    structures = dict(before["structures"])
    if not arg:
        return f"{kind.capitalize()} what? Type 'recipes' for options.", pouches, goods, structures, None
    recipe = before["recipes"].get((arg.lower(),))
    if recipe is None or recipe[1] != kind:
        return f"No {kind} recipe named {arg!r}.", pouches, goods, structures, None
    if kind == "build" and any(
        row[1:4] == (room, recipe[0], actor) for row in structures.values()
    ):
        return f"You already built {recipe[3]} here.", pouches, goods, structures, None
    ingredients = json.loads(recipe[2])
    holdings = {key[1]: row[2] for key, row in pouches.items() if key[0] == actor}
    missing = [f"{material} x{quantity - holdings.get(material, 0)}"
               for material, quantity in ingredients.items()
               if holdings.get(material, 0) < quantity]
    if missing:
        return "Missing materials: " + ", ".join(missing), pouches, goods, structures, None
    for material, quantity in ingredients.items():
        old = pouches[(actor, material)]
        pouches[(actor, material)] = (actor, material, old[2] - quantity)
    if kind == "craft":
        key = (actor, recipe[3])
        old = goods.get(key)
        goods[key] = (actor, recipe[3], (old[2] if old else 0) + recipe[4])
        response = f"You craft {recipe[3]} x{recipe[4]}."
    else:
        response = f"You build {recipe[3]} here."
    return response, pouches, goods, structures, (recipe[0], recipe[3])


def _verify_make_effect(db: sqlite3.Connection, before: dict, domain: dict,
                        *, actor: str, room: str, verb: str, arg: str,
                        action_id: str, command: str, response: str,
                        emissions: list[tuple[int, str]],
                        constructions: list[tuple[int, str]]) -> None:
    """Attest the complete typed craft/build economy and event grant.

    A signed recipe can consume precisely its configured ingredients and add
    precisely one output batch/structure, event and receipt. Installed triggers
    cannot append, delete or change any other world row, including previous
    history, other actors' inventory or AUTOINCREMENT sequence state.
    """
    after = _gather_evidence(db)
    planned_response, pouches, goods, structures, output = _planned_make(
        before, actor, room, verb, arg)
    if response != planned_response:
        raise ValueError("Craft/build response conflicts with pre-action recipe")
    expected = {key: dict(value) for key, value in before.items()
                if key not in ("other_world", "allocators")}
    expected["pouches"], expected["goods"] = pouches, goods
    valid = output is not None
    if len(constructions) != (1 if valid and verb == "build" else 0):
        raise ValueError("Craft/build construction provenance diverged")
    expected_allocators = dict(before["allocators"])
    if valid and verb == "build":
        structure_id = expected_allocators.get("built_structures", 0) + 1
        if constructions[0][0] != structure_id:
            raise ValueError("Construction allocator/identity was forged")
        structures[(structure_id,)] = (
            structure_id, room, output[0], actor, constructions[0][1])
        expected_allocators["built_structures"] = structure_id
    expected["structures"] = structures
    receipt_key = (actor, action_id)
    if receipt_key in expected["receipts"]:
        raise ValueError("Craft/build receipt unexpectedly preexists")
    expected["receipts"][receipt_key] = (actor, action_id, command, response)
    if len(emissions) != (1 if valid else 0):
        raise ValueError("Craft/build gameplay event provenance diverged")
    if valid:
        new_id = expected_allocators.get("events", 0) + 1
        if emissions[0][0] != new_id:
            raise ValueError("Craft/build event allocator/identity was forged")
        expected["events"][(new_id,)] = (
            new_id, emissions[0][1], actor, room, verb,
            json.dumps({"recipe": output[0], "output": output[1]}, sort_keys=True))
        expected_allocators["events"] = new_id
    # Receipt insertion happens after the domain transition; its only grant is
    # exactly one receipt, never another event or economic row mutation.
    for stage in (domain, after):
        if stage["other_world"] != before["other_world"]:
            raise ValueError("Craft/build changed ungranted world state")
        if stage["nodes"] != before["nodes"] or stage["recipes"] != before["recipes"]:
            raise ValueError("Craft/build changed ungranted stock/recipes")
        for key in ("pouches", "goods", "structures", "events"):
            if stage[key] != expected[key]:
                raise ValueError(f"Craft/build {key} gameplay effect diverged")
        if stage["allocators"] != expected_allocators:
            raise ValueError("Craft/build changed ungranted ID allocation")
    if domain["receipts"] != before["receipts"] or after["receipts"] != expected["receipts"]:
        raise ValueError("Craft/build receipt effects diverged")


def _verify_gather_effect(db: sqlite3.Connection, before: dict[str, dict],
                          domain: dict[str, dict],
                          *, actor: str, room: str, arg: str, action_id: str,
                          command: str, response: str,
                          emissions: list[tuple[int, str]]) -> None:
    """A legacy gather grants only typed stock→pouch→event→receipt deltas.

    The realm clock's older host_action callback may be followed by installed
    SQLite receipt triggers. Verify their full economics before releasing this
    SAVEPOINT, including unrelated actors, sites, historical rows and receipts.
    Ordinary unrelated room `say` events remain an explicit separate lane.
    """
    after = _gather_evidence(db)
    if after["other_world"] != before["other_world"]:
        raise ValueError("Gather changed ungranted world gameplay state")

    # The genuine gather event and a permitted separate say event can advance
    # events AUTOINCREMENT. A trigger cannot increase that durable allocator
    # without a corresponding surviving event row, nor poison another family.
    def expected_allocators(events):
        result = dict(before["allocators"])
        new_event_ids = [row[0] for key, row in events.items()
                         if key not in before["events"]]
        if new_event_ids:
            result["events"] = max(result.get("events", 0), *new_event_ids)
        return result

    if (domain["allocators"] != expected_allocators(domain["events"])
            or after["allocators"] != expected_allocators(after["events"])):
        raise ValueError("Gather changed ungranted AUTOINCREMENT allocation")
    # A late receipt trigger might DELETE the genuine gather event and INSERT
    # a semantically identical replacement with a new event ID. Preserve the
    # exact domain-emitted rows captured BEFORE receipt-trigger callbacks.
    if any(after["events"].get(key) != row
           for key, row in domain["events"].items()):
        raise ValueError("Gather history was replaced after domain execution")
    # The *typed gather* domain contract emits only its own gather history.
    # In particular an AFTER INSERT receipt trigger and an AFTER INSERT gather
    # event trigger may not launder a new `say`, even with a plausible speaker
    # or huge explicit event ID. Separately selected trusted host handlers may
    # emit permitted unrelated speech AFTER this adapter returns; WorldClock
    # captures that explicit handler stage before scheduler acknowledgment.
    if after["events"] != domain["events"]:
        raise ValueError("Gather receipt inserted an ungranted gameplay event")
    expected = {name: dict(rows) for name, rows in before.items()
                if name not in ("other_world", "allocators")}
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
    # No new non-gather rows are permitted within the direct typed gather
    # domain, including privileged triggers fired by Crafting._gather itself.
    new_gathers = list(new_events.values())
    if (len(emissions) != (1 if planned else 0)
            or any(row[0] <= max((key[0] for key in old_event_ids), default=0)
            for row in new_events.values())
            or len(new_gathers) != (1 if planned else 0)
            or any(row[4] != "gather" for row in new_gathers)):
        raise ValueError("Gather produced ungranted gameplay history")
    if planned is not None:
        material, amount = planned
        if response != f"You gather {material} x{amount}.":
            raise ValueError("Gather response conflicts with authorized stock")
        # SQLite AUTOINCREMENT advances exactly once for the one gather event.
        # Compare with the *pre-domain* allocator, not with a trigger-tainted
        # post-domain snapshot: an installed events INSERT trigger could delete
        # the real event and replace it with an otherwise identical MAXINT id.
        expected_event_id = before["allocators"].get("events", 0) + 1
        if set(new_events) != {(expected_event_id,)}:
            raise ValueError("Gather history did not use its authorized event ID")
        node = before["nodes"][(room, material)]
        expected["nodes"][(room, material)] = (room, material, node[2] - amount, node[3])
        previous = before["pouches"].get((actor, material))
        expected["pouches"][(actor, material)] = (
            actor, material, (previous[2] if previous else 0) + amount)
        event = new_gathers[0]
        if (event[0:2] != emissions[0]
                or event[2:5] != (actor, room, "gather")
                or event[5] != json.dumps({"material": material, "amount": amount},
                                         sort_keys=True)):
            raise ValueError("Gather timestamp/history is not bound to the actual stock grant")
    for name in ("nodes", "pouches", "goods", "structures", "recipes", "receipts"):
        if after[name] != expected[name]:
            raise ValueError("Gather economic/receipt effect diverged")


def _verify_say_effect(db: sqlite3.Connection, before: dict[str, dict],
                       domain: dict[str, dict], *, actor: str, room: str,
                       arg: str, action_id: str, command: str, response: str,
                       emissions: list[tuple[int, str]]) -> None:
    """A typed host `say` grants one exact speech event and one action receipt.

    Privileged SQLite event/receipt triggers must not impersonate another
    speaker, add ungranted events or exhaust the durable event ID allocator.
    Separately selected trusted audit handlers may add unrelated speech after
    this function returns; those are independent WorldClock handler stages.
    """
    after = _say_evidence(db)
    # No say action grants inventory, geography, profiles or other MAIN rows.
    for stage in (domain, after):
        if (stage["schema"] != before["schema"]
                or stage["tables"] != before["tables"]):
            raise ValueError("Say action changed ungranted world effects")
    if (domain["receipts"] != before["receipts"]
            or any(domain["events"].get(key) != row
                   for key, row in before["events"].items())
            or after["events"] != domain["events"]):
        raise ValueError("Say action changed ungranted gameplay event history")
    expected_receipts = dict(before["receipts"])
    key = (actor, action_id)
    if key in expected_receipts:
        raise ValueError("Say receipt unexpectedly preexists")
    expected_receipts[key] = (actor, action_id, command, response)
    if after["receipts"] != expected_receipts:
        raise ValueError("Say action receipt effect diverged")

    message = "".join(char for char in arg if char.isprintable()).strip()
    valid = bool(message) and len(message) <= 280
    expected_response = (f"You say: {message}" if valid else
                         "Say what?" if not message else
                         "Messages must be 280 characters or fewer.")
    if response != expected_response:
        raise ValueError("Say response differs from authorized text")
    additions = [row for key, row in domain["events"].items()
                 if key not in before["events"]]
    if len(additions) != (1 if valid else 0) or len(emissions) != (1 if valid else 0):
        raise ValueError("Say produced ungranted gameplay event history")
    if valid:
        row = additions[0]
        prior_id = max((key[0] for key in before["events"]), default=0)
        if (row[0:2] != emissions[0] or row[0] <= prior_id
                or row[2:6] != (actor, room, "say",
                                   json.dumps({"text": message}, sort_keys=True))):
            raise ValueError("Say timestamp/history is not bound to the typed speaker/action")

    expected_allocators = dict(before["allocators"])
    if additions:
        expected_allocators["events"] = max(
            expected_allocators.get("events", 0), additions[0][0])
    if (domain["allocators"] != expected_allocators
            or after["allocators"] != expected_allocators):
        raise ValueError("Say changed ungranted AUTOINCREMENT allocation")


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
            make_before = (_gather_evidence(db)
                           if verb in ("craft", "build") else None)
            say_before = _say_evidence(db) if verb == "say" else None
            transfer_before = (_transfer_evidence(db)
                               if verb in ("go", "take", "drop") else None)
            transfer_plan = (_planned_transfer(db, transfer_before, engine,
                                               actual_actor, room, verb, arg)
                             if transfer_before is not None else None)
            # Only the trusted domain emitter can return the exact original
            # insertion time/ID before SQLite AFTER triggers rewrite rows.
            emissions: list[tuple[int, str]] = []
            constructions: list[tuple[int, str]] = []
            if verb in _ENGINE_ACTIONS:
                response = engine._perform(db, actual_actor, room, verb, arg,
                                           emissions=emissions)
            elif verb == "gather":
                response = crafting._gather(db, actual_actor, room, arg,
                                            emissions=emissions)
            else:
                response = crafting._make(db, actual_actor, room, verb, arg,
                                          emissions=emissions,
                                          structures=constructions)
            gather_domain = _gather_evidence(db) if gather_before is not None else None
            make_domain = _gather_evidence(db) if make_before is not None else None
            say_domain = _say_evidence(db) if say_before is not None else None
            transfer_domain = (_transfer_evidence(db)
                               if transfer_before is not None else None)
            db.execute(
                "INSERT INTO main.action_receipts(actor,action_id,command,response) VALUES(?,?,?,?)",
                (actual_actor, action_id, command, response),
            )
            if gather_before is not None:
                _verify_gather_effect(db, gather_before, gather_domain,
                                      actor=actual_actor,
                                      room=room, arg=arg, action_id=action_id,
                                      command=command, response=response,
                                      emissions=emissions)
            if make_before is not None:
                _verify_make_effect(db, make_before, make_domain,
                                    actor=actual_actor, room=room, verb=verb,
                                    arg=arg, action_id=action_id,
                                    command=command, response=response,
                                    emissions=emissions,
                                    constructions=constructions)
            if say_before is not None:
                _verify_say_effect(db, say_before, say_domain,
                                   actor=actual_actor, room=room, arg=arg,
                                   action_id=action_id, command=command,
                                   response=response, emissions=emissions)
            if transfer_before is not None:
                _verify_transfer_effect(db, transfer_before, transfer_domain,
                                        plan=transfer_plan, actor=actual_actor,
                                        action_id=action_id, command=command,
                                        response=response, emissions=emissions)
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT realm_clock_gameplay")
        db.execute("RELEASE SAVEPOINT realm_clock_gameplay")
        raise
    db.execute("RELEASE SAVEPOINT realm_clock_gameplay")
    return response
