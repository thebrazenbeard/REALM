"""Deterministic REALM core: rooms, players, actions, and durable history."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable


_DIRECTIONS = {"n": "north", "s": "south", "e": "east", "w": "west", "u": "up", "d": "down"}
_LONG_DIRECTIONS = frozenset(_DIRECTIONS.values())
_PLAYER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
_SEED_ROOMS = (
    ("atrium", "The Atrium", "An empty chamber waits for explorers. An unobtrusive sign reads: DEMO WORLD — NOT CANON."),
    ("corridor", "The Corridor", "A long corridor hums with the sound of machinery whose purpose has not yet been decided."),
    ("workshop", "The Workshop", "Tables and unclaimed tools hint at things that might be built together."),
)
_SEED_EXITS = (
    ("atrium", "north", "corridor"),
    ("corridor", "south", "atrium"),
    ("corridor", "east", "workshop"),
    ("workshop", "west", "corridor"),
)
_SEED_ITEMS = (
    ("token", "Brass token", "A small unmarked token for testing shared item ownership.", "atrium"),
    ("wrench", "Worn wrench", "A tool left behind in the demonstration workshop.", "workshop"),
)

_CORE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exits (
    room_id TEXT NOT NULL REFERENCES rooms(id),
    direction TEXT NOT NULL,
    destination TEXT NOT NULL REFERENCES rooms(id),
    PRIMARY KEY (room_id, direction)
);
CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY COLLATE NOCASE,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    room_id TEXT REFERENCES rooms(id),
    held_by TEXT REFERENCES players(id),
    CHECK ((room_id IS NOT NULL AND held_by IS NULL)
        OR (room_id IS NULL AND held_by IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    room_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_receipts (
    actor TEXT NOT NULL,
    action_id TEXT NOT NULL,
    command TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY(actor, action_id)
);
CREATE TABLE IF NOT EXISTS world_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_room_id ON events(room_id, id);
"""


def _schema_snapshot(db: sqlite3.Connection) -> dict[str, tuple[str, str, str | None]]:
    """SQLite's actual stored definitions, including implicit indexes.

    For a *legacy unbound* database we must detect changed constraints and
    indexes, not just matching table names or currently seeded rows.
    """
    return {
        name: (kind, table, " ".join(ddl.split()) if ddl is not None else None)
        for kind, name, table, ddl in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master"
        )
    }


@lru_cache(maxsize=4)
def _pristine_schema(extra_sql: str = "") -> dict[str, tuple[str, str, str | None]]:
    """Build a read-only canonical fingerprint from the one engine DDL source."""
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(_CORE_SCHEMA_SQL)
        if extra_sql:
            db.executescript(extra_sql)
        return _schema_snapshot(db)


class Engine:
    """One database file is one isolated REALM instance.

    Connections are short-lived, and all mutating actions are SQLite transactions.
    The event ledger is append-only at the application layer; this is not an
    anti-tamper or multi-host authoritative service.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            # WAL initialization needs an exclusive database lock. Concurrent
            # first-time clients can collide during that short transition,
            # even when their later reads/writes are correctly serialized.
            for attempt in range(10):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 9:
                        raise
                    time.sleep(min(0.05 * (attempt + 1), 0.25))
            return conn
        except BaseException:
            conn.close()
            raise

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript(_CORE_SCHEMA_SQL)
            # executescript commits implicitly; lock the first-run seed phase
            # so concurrent clients cannot both initialize the demo rooms.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
                db.executemany("INSERT INTO rooms VALUES (?,?,?)", _SEED_ROOMS)
                db.executemany("INSERT INTO exits VALUES (?,?,?)", _SEED_EXITS)
                db.executemany(
                    "INSERT INTO items (id,name,description,room_id) VALUES (?,?,?,?)",
                    _SEED_ITEMS,
                )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _event(self, db: sqlite3.Connection, actor: str, room: str, kind: str,
               payload: dict, *, emissions: list[tuple[int, str]] | None = None) -> None:
        values = (self._now(), actor, room, kind, json.dumps(payload, sort_keys=True))
        sql = "INSERT INTO events (occurred_at,actor,room_id,kind,payload_json) VALUES (?,?,?,?,?)"
        if emissions is None:
            db.execute(sql, values)
        else:
            # RETURNING exposes the actual outer INSERT's tuple, prior to any
            # installed AFTER trigger rewriting it. Never infer original time
            # from the subsequently mutated events table or wall-clock range.
            with closing(db.execute(sql + " RETURNING id,occurred_at", values)) as cursor:
                emitted = cursor.fetchone()
            if emitted is None:
                raise ValueError("Event emitter did not return its inserted row")
            emissions.append((emitted[0], emitted[1]))

    def join(self, name: str, *, writer_guard: Callable[[sqlite3.Connection], None] | None = None) -> str:
        if not _PLAYER_RE.fullmatch(name):
            raise ValueError("Player name must start with a letter, use only letters, digits, '_' or '-', and be <=24 characters")
        with closing(self._connect()) as db, db:
            # Serialize first-time joins so two clients cannot race on one name.
            db.execute("BEGIN IMMEDIATE")
            if writer_guard is not None:
                writer_guard(db)
            player = db.execute("SELECT id,room_id FROM players WHERE id=?", (name,)).fetchone()
            if player is None:
                configured = db.execute("SELECT value FROM world_meta WHERE key='start_room'").fetchone()
                start_room = configured[0] if configured is not None else "atrium"
                db.execute("INSERT INTO players VALUES (?,?,?)", (name, start_room, self._now()))
                self._event(db, name, start_room, "join", {})
                return f"Welcome, {name}.\n{self._look(db, start_room, name)}"
            return f"Welcome back, {player['id']}.\n{self._look(db, player['room_id'], player['id'])}"

    def _look(self, db: sqlite3.Connection, room_id: str, name: str) -> str:
        room = db.execute("SELECT name,description FROM rooms WHERE id=?", (room_id,)).fetchone()
        paths = db.execute("SELECT direction FROM exits WHERE room_id=? ORDER BY direction", (room_id,)).fetchall()
        others = db.execute("SELECT id FROM players WHERE room_id=? AND id<>? ORDER BY id COLLATE NOCASE", (room_id, name)).fetchall()
        parts = [room["name"], room["description"], "Exits: " + (", ".join(x[0] for x in paths) or "none")]
        if others:
            parts.append("Also here: " + ", ".join(x[0] for x in others))
        items = db.execute("SELECT name FROM items WHERE room_id=? ORDER BY id", (room_id,)).fetchall()
        if items:
            parts.append("Items: " + ", ".join(item[0] for item in items))
        return "\n".join(parts)

    def act(self, name: str, raw_command: str, *, action_id: str | None = None,
            writer_guard: Callable[[sqlite3.Connection], None] | None = None) -> str:
        """Perform one action atomically; optional keys make retries idempotent.

        Action IDs are scoped to an actor and MUST be unique per logical command.
        This is local action deduplication, not network authentication.
        """
        command = raw_command.strip()
        # Blank/quit shortcuts return before the normal action transaction.
        # Scoped HTTP calls still need the same-world writer authority check:
        # otherwise a revoke after the handler's check gets HTTP 200.
        if writer_guard is not None and (
            not command or command.partition(" ")[0].casefold() in {"quit", "exit"}
        ):
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                writer_guard(db)
        if not command:
            return "Enter a command. Type 'help' for options."
        verb, _, arg = command.partition(" ")
        verb = verb.casefold()
        arg = arg.strip()
        if verb in _DIRECTIONS:
            shortcut = verb
            verb = "go"
            arg = _DIRECTIONS[shortcut]
        elif verb in _LONG_DIRECTIONS:
            arg = verb
            verb = "go"
        if action_id is not None and (not isinstance(action_id, str) or not 1 <= len(action_id) <= 128):
            raise ValueError("Action IDs must be nonempty strings up to 128 characters")
        if verb in {"quit", "exit"}:
            return "Goodbye. Your position is saved."
        with closing(self._connect()) as db, db:
            # Serialize competing actions such as two players taking one object.
            db.execute("BEGIN IMMEDIATE")
            if writer_guard is not None:
                writer_guard(db)
            player = db.execute("SELECT id,room_id FROM players WHERE id=?", (name,)).fetchone()
            if player is None:
                raise ValueError("Join this REALM before issuing commands")
            actor, room_id = player["id"], player["room_id"]
            if action_id is not None:
                receipt = db.execute(
                    "SELECT command,response FROM action_receipts WHERE actor=? AND action_id=?",
                    (actor, action_id),
                ).fetchone()
                if receipt is not None:
                    if receipt["command"] != command:
                        raise ValueError("Action ID already used for a different command")
                    # Room history is a read whose authorization boundary
                    # changes on every arrival. A cached old transcript is
                    # not an idempotent mutation receipt: re-evaluate the
                    # current room's entry floor even on a retried action ID.
                    if verb not in {"history", "listen"}:
                        return receipt["response"]
            response = self._perform(db, actor, room_id, verb, arg)
            if action_id is not None:
                if receipt is not None:
                    db.execute(
                        "UPDATE action_receipts SET response=? WHERE actor=? AND action_id=?",
                        (response, actor, action_id),
                    )
                else:
                    db.execute(
                        "INSERT INTO action_receipts (actor,action_id,command,response) VALUES (?,?,?,?)",
                        (actor, action_id, command, response),
                    )
            return response

    @staticmethod
    def _room_entry_floor(db: sqlite3.Connection, actor: str, room_id: str) -> int:
        """Prove current-room arrival, including any newer departures.

        Do not fall back to an older arrival when a newer return move has been
        removed or the event ledger contradicts persisted player location.
        This is one snapshot inside the command's BEGIN IMMEDIATE transaction.
        """
        entry = db.execute(
            "SELECT id,room_id,kind FROM events WHERE actor=? COLLATE NOCASE "
            "AND kind IN ('join','leave','move') ORDER BY id DESC LIMIT 1",
            (actor,),
        ).fetchone()
        if (entry is None or entry["kind"] not in {"join", "move"} or
                entry["room_id"] != room_id):
            raise ValueError("No provable current-room arrival; history denied")
        return entry["id"]

    def _perform(self, db: sqlite3.Connection, actor: str, room_id: str, verb: str,
                 arg: str, *, emissions: list[tuple[int, str]] | None = None) -> str:
            if verb in {"look", "l"}:
                return self._look(db, room_id, actor)
            if verb in {"where", "location"}:
                return f"You are in {db.execute('SELECT name FROM rooms WHERE id=?', (room_id,)).fetchone()[0]}."
            if verb == "go":
                direction = _DIRECTIONS.get(arg.casefold(), arg.casefold())
                dest = db.execute("SELECT destination FROM exits WHERE room_id=? AND direction=?", (room_id, direction)).fetchone()
                if dest is None:
                    return f"No exit leads {direction or 'that way'}."
                db.execute("UPDATE players SET room_id=? WHERE id=?", (dest[0], actor))
                # One transaction records both audiences: people left behind
                # learn about departure; destination occupants see arrival.
                self._event(db, actor, room_id, "leave", {"to": dest[0], "direction": direction},
                            emissions=emissions)
                self._event(db, actor, dest[0], "move", {"from": room_id, "direction": direction},
                            emissions=emissions)
                return self._look(db, dest[0], actor)
            if verb in {"inventory", "inv", "i"}:
                held = db.execute("SELECT name FROM items WHERE held_by=? ORDER BY id", (actor,)).fetchall()
                return "You carry: " + (", ".join(row[0] for row in held) if held else "nothing")
            if verb == "take":
                if not arg:
                    return "Take what?"
                item = db.execute(
                    "SELECT id,name FROM items WHERE room_id=? AND (id=? COLLATE NOCASE OR name=? COLLATE NOCASE)",
                    (room_id, arg, arg),
                ).fetchone()
                if item is None:
                    return "That item is not here."
                updated = db.execute(
                    "UPDATE items SET held_by=?,room_id=NULL WHERE id=? AND room_id=? AND held_by IS NULL",
                    (actor, item["id"], room_id),
                )
                if updated.rowcount != 1:
                    return "Someone else took that item first."
                self._event(db, actor, room_id, "take", {"item": item["id"]},
                            emissions=emissions)
                return f"You take {item['name']}."
            if verb == "drop":
                if not arg:
                    return "Drop what?"
                item = db.execute(
                    "SELECT id,name FROM items WHERE held_by=? AND (id=? COLLATE NOCASE OR name=? COLLATE NOCASE)",
                    (actor, arg, arg),
                ).fetchone()
                if item is None:
                    return "You are not carrying that item."
                db.execute("UPDATE items SET held_by=NULL,room_id=? WHERE id=? AND held_by=?", (room_id, item["id"], actor))
                self._event(db, actor, room_id, "drop", {"item": item["id"]},
                            emissions=emissions)
                return f"You drop {item['name']}."
            if verb == "who":
                people = db.execute("SELECT id FROM players ORDER BY id COLLATE NOCASE").fetchall()
                return "Players who have joined this world: " + ", ".join(p[0] for p in people)
            if verb == "say":
                message = "".join(c for c in arg if c.isprintable()).strip()
                if not message:
                    return "Say what?"
                if len(message) > 280:
                    return "Messages must be 280 characters or fewer."
                self._event(db, actor, room_id, "say", {"text": message},
                            emissions=emissions)
                return f"You say: {message}"
            if verb in {"history", "listen"}:
                if arg and (not arg.isdigit() or not 1 <= int(arg) <= 50):
                    return "Use 'history' or 'history N' (1–50)."
                limit = int(arg) if arg else 10
                entry_id = self._room_entry_floor(db, actor, room_id)
                events = db.execute(
                    "SELECT actor,kind,payload_json FROM events "
                    "WHERE room_id=? AND id>=? ORDER BY id DESC LIMIT ?",
                    (room_id, entry_id, limit),
                ).fetchall()
                items = []
                for event in reversed(events):
                    details = json.loads(event["payload_json"])
                    if event["kind"] == "say":
                        items.append(f"{event['actor']} said: {details['text']}")
                    elif event["kind"] == "move":
                        items.append(f"{event['actor']} arrived from {details['from']}")
                    elif event["kind"] == "leave":
                        items.append(f"{event['actor']} departed toward {details['to']}")
                    elif event["kind"] == "join":
                        items.append(f"{event['actor']} entered the world")
                    elif event["kind"] in {"take", "drop"}:
                        past = "took" if event["kind"] == "take" else "dropped"
                        items.append(f"{event['actor']} {past} {details['item']}")
                return "\n".join(items) if items else "No recorded activity here yet."
            if verb in {"help", "?"}:
                return "Commands: look, go <north|south|east|west>, n/s/e/w, take <item>, drop <item>, inventory, say <text>, listen, history [1-50], who, where, help, quit"
            return "Unknown command. Type 'help' for options."
