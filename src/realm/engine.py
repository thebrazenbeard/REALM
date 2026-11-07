"""Deterministic REALM core: rooms, players, actions, and durable history."""

from __future__ import annotations

import json
import errno
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .sqlite_runtime import wal_reset_fix_present


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
-- Event-time grants are recorded in the same transaction as their event.
-- seq is scoped to a *recipient*, never the world-global events sequence.
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

# Versioned, known additive extension. Keep immutable core DDL alone so the
# bound-world integrity check (PR47) can distinguish preexisting core damage
# from an explicitly supported missing/new extension at startup.
_EVENT_DELIVERY_DDL = (
"""CREATE TABLE IF NOT EXISTS event_deliveries (
    recipient TEXT NOT NULL COLLATE NOCASE REFERENCES players(id),
    seq INTEGER NOT NULL CHECK (seq > 0),
    event_id INTEGER NOT NULL REFERENCES events(id),
    revoked_at TEXT,
    PRIMARY KEY (recipient, seq),
    UNIQUE (recipient, event_id)
)""",
"CREATE INDEX IF NOT EXISTS idx_event_deliveries_event ON event_deliveries(event_id)",
)
_EVENT_DELIVERY_SCHEMA_SQL = ";\n".join(_EVENT_DELIVERY_DDL) + ";"
_EVENT_DELIVERY_VERSION_KEY = "event_delivery_schema_version"
_EVENT_DELIVERY_VERSION = "realm.event_delivery.v1"
_TRANSPORT_AUTH_MODE_KEY = "transport_auth_mode"
_TRANSPORT_AUTH_MODES = frozenset({"shared-token-demo", "player-bearer-http"})


def _claim_transport_auth_mode(db: sqlite3.Connection, mode: str) -> None:
    """Fence incompatible local adapters in the caller's writer transaction.

    A shared-secret Telnet identity is not a player-bound HTTP identity. The
    durable claim deliberately survives disconnect/server restart. Switching
    an already-played world between modes needs an explicit offline migration.
    """
    if mode not in _TRANSPORT_AUTH_MODES:
        raise ValueError("Unsupported world transport authentication mode")
    existing = db.execute(
        "SELECT value FROM world_meta WHERE key=?", (_TRANSPORT_AUTH_MODE_KEY,)
    ).fetchone()
    if existing is not None and existing[0] != mode:
        raise ValueError(
            "World transport authentication conflict: cannot mix shared-token "
            "Telnet identities with player-bound HTTP credentials"
        )
    if existing is None:
        # Older HTTP worlds might predate this marker, or a local operator
        # might accidentally delete it. Never let the shared-token adapter
        # acquire a world already carrying the HTTP credential table. This
        # remains best-effort in-file damage detection, not immutable custody.
        if mode == "shared-token-demo" and db.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='player_credentials')"
        ).fetchone()[0]:
            raise ValueError(
                "World transport authentication conflict: player-bound "
                "HTTP credential state already exists"
            )
        db.execute(
            "INSERT INTO world_meta(key,value) VALUES(?,?)",
            (_TRANSPORT_AUTH_MODE_KEY, mode),
        )


@contextmanager
def _world_startup_lock(db_path: Path):
    """Serialize first-open journal choice across local processes and threads.

    SQLite cannot change journal mode while a transaction is active, so a
    SQLite transaction cannot by itself fence the journal selection and the
    initial schema creation together. The persistent sidecar is an advisory
    OS lock; never unlink it on release (doing so can split lock identities).
    Active databases must remain on local storage, not a shared NAS mount.
    """
    lock_path = db_path.resolve().with_suffix(db_path.suffix + ".init.lock")
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            # Windows byte-range locks reserve one byte at offset zero.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
        else:
            import fcntl

        deadline = time.monotonic() + 15.0
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring world startup lock: {lock_path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


# Only exact, host-authored public room event shapes can be distributed.
# An unknown future event is retained in the ledger but has NO implicit readers.
_DELIVERY_SHAPES = {
    "join": {frozenset(), frozenset({"resident"})},
    "move": {frozenset({"from", "direction"})},
    "leave": {frozenset({"to", "direction"})},
    "take": {frozenset({"item"})},
    "drop": {frozenset({"item"})},
    "say": {frozenset({"text"})},
    "gather": {frozenset({"material", "amount"})},
    "craft": {frozenset({"recipe", "output"})},
    "build": {frozenset({"recipe", "output"})},
}


def _append_event_with_deliveries(
    db: sqlite3.Connection, actor: str, room: str, kind: str, payload: dict,
) -> int:
    """Append one event and its event-time per-player outbox, atomically.

    The current prototype's witness rule is *persisted room position*, NOT an
    authenticated online session or opt-in recording policy. Legacy events
    have no deliveries and cannot be granted retroactively by room position.
    Only trusted Engine/Crafting writers may invoke this, under their SQLite
    writer transaction; models and clients cannot choose audience members.
    """
    row = db.execute(
        "INSERT INTO events (occurred_at,actor,room_id,kind,payload_json) VALUES (?,?,?,?,?)",
        (Engine._now(), actor, room, kind, json.dumps(payload, sort_keys=True)),
    )
    event_id = int(row.lastrowid)
    if kind not in _DELIVERY_SHAPES or frozenset(payload) not in _DELIVERY_SHAPES[kind]:
        return event_id
    if kind == "join" and payload and payload != {"resident": True}:
        return event_id
    # The mover has already changed rooms by the time the departure event is
    # appended; include their own event even in the old room's outbox.
    recipients = db.execute(
        "SELECT id FROM players WHERE room_id=? UNION SELECT id FROM players WHERE id=?",
        (room, actor),
    ).fetchall()
    for recipient in recipients:
        player_id = recipient[0]
        next_seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM event_deliveries WHERE recipient=?",
            (player_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO event_deliveries(recipient,seq,event_id) VALUES(?,?,?)",
            (player_id, next_seq, event_id),
        )
    if recipients:
        # This marker is attached to the *first committed grant*, not blank
        # database creation. A pristine Gateway/worldpack import remains clean.
        # It travels in the same writer transaction as the event and grant.
        marker = db.execute(
            "SELECT value FROM world_meta WHERE key=?",
            (_EVENT_DELIVERY_VERSION_KEY,),
        ).fetchone()
        if marker is None:
            db.execute(
                "INSERT INTO world_meta(key,value) VALUES(?,?)",
                (_EVENT_DELIVERY_VERSION_KEY, _EVENT_DELIVERY_VERSION),
            )
        elif marker[0] != _EVENT_DELIVERY_VERSION:
            raise WorldIntegrityError("Event delivery schema version is inconsistent")
    return event_id


class WorldIntegrityError(ValueError):
    """Existing world data/schema is damaged; startup must not auto-repair."""


class Engine:
    """One database file is one isolated REALM instance.

    Connections are short-lived, and all mutating actions are SQLite transactions.
    The event ledger is append-only at the application layer; this is not an
    anti-tamper or multi-host authoritative service.
    """

    def __init__(self, db_path: str | Path, *, journal_mode: str | None = None):
        """Open a world without implicitly migrating its existing journal.

        New worlds default to WAL. An explicitly selected DELETE journal is
        useful for disposable testing on hosts whose SQLite WAL runtime is not
        yet qualified. Switching an existing database is an offline migration,
        not a side effect of constructing another Engine instance.
        """
        if journal_mode is not None:
            if not isinstance(journal_mode, str) or journal_mode.upper() not in {"WAL", "DELETE"}:
                raise ValueError("journal_mode must be WAL or DELETE")
            journal_mode = journal_mode.upper()
        self.db_path = Path(db_path)
        self._requested_journal_mode = journal_mode
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with _world_startup_lock(self.db_path):
            self._new_database = not self.db_path.exists() or self.db_path.stat().st_size == 0
            self._initialize()
            self._new_database = False
            if journal_mode is not None:
                with closing(sqlite3.connect(str(self.db_path))) as db:
                    actual = str(db.execute("PRAGMA journal_mode").fetchone()[0]).upper()
                if actual != journal_mode:
                    raise RuntimeError(
                        f"World journal claim failed: expected {journal_mode}, loaded {actual}"
                    )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            current = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
            has_schema = conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None
            # A process may have initialized this file after our pre-open
            # existence check. In that case honor its selected journal.
            new_empty_world = self._new_database and not has_schema
            safe_wal_runtime = wal_reset_fix_present(sqlite3.sqlite_version_info)
            desired = self._requested_journal_mode or (
                ("WAL" if safe_wal_runtime else "DELETE") if new_empty_world else current
            )
            if desired == "WAL" and not safe_wal_runtime:
                raise RuntimeError(
                    "Loaded SQLite WAL-reset version gate failed; use an updated "
                    "SQLite runtime for existing WAL worlds, or DELETE for a fresh world"
                )
            if current != desired:
                if not new_empty_world:
                    raise ValueError(
                        f"Existing world uses {current}; changing to {desired} requires "
                        "an explicit offline journal migration"
                    )
                # WAL initialization requires an exclusive lock, which may
                # briefly collide during simultaneous first-world startup.
                for attempt in range(10):
                    try:
                        applied = str(conn.execute(f"PRAGMA journal_mode={desired}").fetchone()[0]).upper()
                        if applied != desired:
                            raise sqlite3.OperationalError(
                                f"Requested SQLite journal {desired}, got {applied}"
                            )
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
            # CREATE TABLE IF NOT EXISTS is useful on a *new* database but can
            # silently hide missing core tables on a bound world. Inspect the
            # existing authority marker before running any idempotent DDL.
            # Extra extension tables are allowed; changed/missing core table
            # and index definitions are not an implicit migration.
            has_world_metadata_table = db.execute(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='world_meta')"
            ).fetchone()[0]
            existing_schema = _schema_snapshot(db)
            expected_core = _pristine_schema()
            # A bound/claimed world must not execute unreviewed SQL triggers
            # or views. An attacker with local DB write access could install a
            # grant-deleting trigger while leaving all expected tables intact.
            # Legitimate extension *tables/indexes* remain allowed.
            if has_world_metadata_table and db.execute(
                "SELECT EXISTS(SELECT 1 FROM world_meta)"
            ).fetchone()[0]:
                executable_extras = sorted(
                    name for name, definition in existing_schema.items()
                    if definition[0] in {"trigger", "view"}
                )
                if executable_extras:
                    raise WorldIntegrityError(
                        "Bound world integrity failure: unexpected trigger/view "
                        "schema objects: " + ", ".join(executable_extras)
                    )
            expected_extension = {
                name: definition
                for name, definition in _pristine_schema(_EVENT_DELIVERY_SCHEMA_SQL).items()
                if name not in expected_core
            }
            marker = None
            if has_world_metadata_table:
                marker = db.execute(
                    "SELECT value FROM world_meta WHERE key=?",
                    (_EVENT_DELIVERY_VERSION_KEY,),
                ).fetchone()
            if marker is not None and marker[0] != _EVENT_DELIVERY_VERSION:
                raise WorldIntegrityError("Bound world event delivery schema version is unknown")
            # Extra extension tables are permissible, but a partially missing,
            # rewritten or already-marked delivery extension is *damage*, not
            # a blank legacy world to silently backfill from current rooms.
            has_extension = any(name in existing_schema for name in expected_extension)
            if marker is not None or has_extension:
                changed_delivery = [
                    name for name, definition in expected_extension.items()
                    if existing_schema.get(name) != definition
                ]
                if changed_delivery:
                    raise WorldIntegrityError(
                        "Bound world event delivery integrity failure: "
                        + ", ".join(sorted(changed_delivery))
                    )
            if has_world_metadata_table and db.execute(
                "SELECT EXISTS(SELECT 1 FROM world_meta)"
            ).fetchone()[0]:
                changed_core = [
                    name for name, definition in expected_core.items()
                    if existing_schema.get(name) != definition
                ]
                if changed_core:
                    raise WorldIntegrityError(
                        "Bound world integrity failure: missing or modified core SQLite "
                        "schema objects: " + ", ".join(sorted(changed_core))
                    )
            db.executescript(_CORE_SCHEMA_SQL)
            # executescript commits implicitly; lock the first-run seed phase
            # so concurrent clients cannot both initialize the demo rooms.
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
                # An imported world can lose its rooms through external damage
                # (or a local writer disabling foreign-key enforcement). Never
                # disguise that loss by installing the generic demo under the
                # *existing* world identity. Likewise, a previously used demo
                # with surviving history/metadata is not a fresh database.
                has_metadata = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM world_meta)"
                ).fetchone()[0]
                has_history = any(
                    db.execute(f"SELECT EXISTS(SELECT 1 FROM {table})").fetchone()[0]
                    for table in ("players", "events", "action_receipts", "items", "exits")
                )
                has_past_ids = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_sequence)"
                ).fetchone()[0]
                if has_metadata or has_history or has_past_ids:
                    raise WorldIntegrityError(
                        "World integrity failure: missing all rooms in a previously "
                        "initialized or bound world; refusing automatic demo reseed"
                    )
                db.executemany("INSERT INTO rooms VALUES (?,?,?)", _SEED_ROOMS)
                db.executemany("INSERT INTO exits VALUES (?,?,?)", _SEED_EXITS)
                db.executemany(
                    "INSERT INTO items (id,name,description,room_id) VALUES (?,?,?,?)",
                    _SEED_ITEMS,
                )
            # Only after bound-world integrity checks pass, install the known
            # additive privacy extension inside this same writer transaction.
            # A damaged legacy world must not acquire schema objects as a side
            # effect of a failed routine Engine restart.
            for ddl in _EVENT_DELIVERY_DDL:
                db.execute(ddl)
            # Upgrade a pre-marker deployment that already contains committed
            # grants; do not infer or manufacture grants for old event rows.
            if db.execute("SELECT EXISTS(SELECT 1 FROM event_deliveries)").fetchone()[0]:
                db.execute(
                    "INSERT OR IGNORE INTO world_meta(key,value) VALUES(?,?)",
                    (_EVENT_DELIVERY_VERSION_KEY, _EVENT_DELIVERY_VERSION),
                )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _event(self, db: sqlite3.Connection, actor: str, room: str, kind: str, payload: dict) -> int:
        return _append_event_with_deliveries(db, actor, room, kind, payload)

    def join(self, name: str, *, transport_auth_mode: str | None = None) -> str:
        if not _PLAYER_RE.fullmatch(name):
            raise ValueError("Player name must start with a letter, use only letters, digits, '_' or '-', and be <=24 characters")
        with closing(self._connect()) as db, db:
            # Serialize first-time joins so two clients cannot race on one name.
            db.execute("BEGIN IMMEDIATE")
            if transport_auth_mode is not None:
                _claim_transport_auth_mode(db, transport_auth_mode)
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

    def act(
        self, name: str, raw_command: str, *, action_id: str | None = None,
        transport_auth_mode: str | None = None,
    ) -> str:
        """Perform one action atomically; optional keys make retries idempotent.

        Action IDs are scoped to an actor and MUST be unique per logical command.
        This is local action deduplication, not network authentication.
        """
        command = raw_command.strip()
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
            if transport_auth_mode is not None:
                _claim_transport_auth_mode(db, transport_auth_mode)
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
                    return receipt["response"]
            response = self._perform(db, actor, room_id, verb, arg)
            if action_id is not None:
                db.execute(
                    "INSERT INTO action_receipts (actor,action_id,command,response) VALUES (?,?,?,?)",
                    (actor, action_id, command, response),
                )
            return response

    def _perform(self, db: sqlite3.Connection, actor: str, room_id: str, verb: str, arg: str) -> str:
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
                self._event(db, actor, room_id, "leave", {"to": dest[0], "direction": direction})
                self._event(db, actor, dest[0], "move", {"from": room_id, "direction": direction})
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
                self._event(db, actor, room_id, "take", {"item": item["id"]})
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
                self._event(db, actor, room_id, "drop", {"item": item["id"]})
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
                self._event(db, actor, room_id, "say", {"text": message})
                return f"You say: {message}"
            if verb in {"history", "listen"}:
                if arg and (not arg.isdigit() or not 1 <= int(arg) <= 50):
                    return "Use 'history' or 'history N' (1–50)."
                limit = int(arg) if arg else 10
                events = db.execute(
                    "SELECT e.actor,e.kind,e.payload_json FROM event_deliveries d "
                    "JOIN events e ON e.id=d.event_id "
                    "WHERE d.recipient=? AND d.revoked_at IS NULL AND e.room_id=? "
                    "ORDER BY e.id DESC LIMIT ?",
                    (actor, room_id, limit),
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
