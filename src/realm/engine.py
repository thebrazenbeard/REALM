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

    @staticmethod
    def _verify_join_entry(
        db: sqlite3.Connection, receipts: list[tuple[int, str]],
        actor: str, room_id: str,
    ) -> None:
        """Reject a join event suppressed or rewritten by a SQLite trigger.

        The row returned by INSERT ... RETURNING proves that the statement
        produced an ID, not that an AFTER trigger left its event in the world.
        Read back the exact event and actor location before COMMIT.
        """
        if len(receipts) != 1:
            raise RuntimeError("Join must emit exactly one host entry event")
        event_id, occurred_at = receipts[0]
        row = db.execute(
            "SELECT actor,room_id,kind,occurred_at FROM main.events WHERE id=?",
            (event_id,),
        ).fetchone()
        player = db.execute(
            "SELECT room_id FROM main.players WHERE id=? COLLATE NOCASE",
            (actor,),
        ).fetchone()
        if (row is None or row["actor"] != actor
                or row["room_id"] != room_id or row["kind"] != "join"
                or row["occurred_at"] != occurred_at or player is None
                or player["room_id"] != room_id):
            raise RuntimeError("Join entry event or actor room failed readback")

    def join(self, name: str, *, writer_guard: Callable[[sqlite3.Connection], None] | None = None,
             emissions: list[tuple[int, str]] | None = None) -> str:
        if not _PLAYER_RE.fullmatch(name):
            raise ValueError("Player name must start with a letter, use only letters, digits, '_' or '-', and be <=24 characters")
        if emissions is not None and emissions:
            raise ValueError("Join entry receipt list must be empty")
        join_receipts = emissions if emissions is not None else []
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
                self._event(db, name, start_room, "join", {}, emissions=join_receipts)
                self._verify_join_entry(db, join_receipts, name, start_room)
                return f"Welcome, {name}.\n{self._look(db, start_room, name)}"
            # Authenticated returning joins establish a new observation floor,
            # preserving saved location and inventory but not prior-session
            # room history. Writer guard has already validated this principal.
            self._event(db, player["id"], player["room_id"], "join", {}, emissions=join_receipts)
            self._verify_join_entry(db, join_receipts, player["id"], player["room_id"])
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
            writer_guard: Callable[[sqlite3.Connection], None] | None = None,
            private_channels: bool = True) -> str:
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
        read_only = verb in {"look", "l", "examine", "x", "where", "location",
                             "inventory", "inv", "i", "who", "history", "listen",
                             "help", "?", "quit", "exit", "allegiance", "factions", "sites", "control"} or (
                                 verb == "pvp" and arg.casefold() in {"status", "news"})
        if (verb == "attack" or (verb == "pvp" and arg.casefold() != "news")) and writer_guard is None:
            # A shared-token terminal can claim another player's name. Combat
            # consent and private HP status require player-bound authority.
            # News is a public, host-authored, globally redacted message.
            raise PermissionError("PvP requires a player-bound authenticated writer guard")
        if verb in {"quit", "exit"} and action_id is None:
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
            receipt_command = command
            if verb in {"pledge", "renounce", "offer"} and not private_channels:
                return "Political pledges require player-bound credentials."
            if verb in {"emote", "ooc", "crewchat"} and not private_channels:
                # A shared-token terminal has no authenticated actor. Its
                # caller can choose any name and forge local or global speech.
                return "Channel posting requires player-bound credentials."
            if verb == "crewchat":
                from .channel_roster import member_for_actor
                member = member_for_actor(db, actor)
                if member is None:
                    return "No active group channel membership."
                # This proof is re-evaluated before every cached receipt.
                # Role changes cannot replay old role-labeled responses.
                receipt_command = json.dumps(
                    [command, member["world_id"], member["group_id"],
                     member["joined_floor"], member["role"]],
                    separators=(",", ":"),
                )
            if (not read_only and verb not in {"say", "emote", "ooc", "crewchat"} and
                    (verb != "pvp" or arg.casefold() == "on")):
                from .combat import Combat
                # Check BEFORE replay, so a receipt minted while alive cannot
                # authorize a defeated actor's old mutation after death.
                Combat.require_living_for_mutation(db, actor)
            receipt = None
            if action_id is not None:
                receipt = db.execute(
                    "SELECT command,response FROM action_receipts WHERE actor=? AND action_id=?",
                    (actor, action_id),
                ).fetchone()
                if receipt is not None:
                    if receipt["command"] != receipt_command:
                        raise ValueError("Action ID already used for a different command")
                    # A read receipt is never a standing authorization grant.
                    # Re-evaluate the room, item ownership and speech entry
                    # floor on each retry; only mutations replay old results.
                    if not read_only:
                        if verb == "pvp" and arg.casefold() in {"on", "off"}:
                            from .combat import Combat
                            policy = Combat._policy(db)
                            _, currently_enabled = Combat._status(db, actor, int(policy["max_hp"]))
                            if currently_enabled != (arg.casefold() == "on"):
                                # Consent can change after an earlier success.
                                # Never reapply a stale opt-in, or acknowledge
                                # a stale opt-out as if protection were active.
                                state = "ON" if currently_enabled else "OFF"
                                return f"Previous PvP command superseded; PvP is currently {state}."
                        if verb in {"pledge", "renounce"}:
                            from .political_oaths import current_faction_id
                            now = current_faction_id(db, actor)
                            if (verb == "renounce" or
                                    (verb == "pledge" and
                                     (now is None or now.casefold() != arg.casefold()))):
                                return ("Previous political command superseded; "
                                        "use allegiance to inspect current state.")
                        if verb == "offer":
                            from .political_leverage import (
                                NONCOMMIT_OFFER_REPLIES, verify_finalized_offer,
                            )
                            if receipt["response"].startswith(f"{actor} offered "):
                                # Every successful historical offer needs a
                                # surviving item, event, score and exact receipt.
                                # Past faction changes, moves or later offerings
                                # do not invalidate the original real event.
                                verify_finalized_offer(
                                    db, actor, room_id, arg, receipt["response"],
                                    action_id=action_id,
                                    receipt_command=receipt_command,
                                    historical_replay=True,
                                )
                            elif receipt["response"] not in NONCOMMIT_OFFER_REPLIES:
                                raise RuntimeError(
                                    "Political offer has an unrecognized receipt"
                                )
                        return receipt["response"]
            # Preserve the existing four-argument interpreter seam for trusted
            # adapters and injected transaction/error probes. Only combat
            # needs the action ID inside its durable PK event record.
            if verb in {"pvp", "attack"}:
                response = self._perform(db, actor, room_id, verb, arg,
                                         action_id=action_id)
            elif not private_channels:
                response = self._perform(db, actor, room_id, verb, arg,
                                         private_channels=False)
            else:
                response = self._perform(db, actor, room_id, verb, arg)
            # A read-only action ID binds command identity, not its current
            # contents. Keep a blank sentinel; never retain private observation
            # text or update that receipt during later read retries.
            if action_id is not None and receipt is None:
                db.execute(
                    "INSERT INTO action_receipts (actor,action_id,command,response) VALUES (?,?,?,?)",
                    (actor, action_id, receipt_command, "" if read_only else response),
                )
            # Installed SQLite triggers can mutate political state after the
            # action receipt insertion, after _perform's immediate readback.
            # Recheck source/effect custody as the final SQL inside this writer.
            # Rejected offers do not carry new effect assertions.
            if verb == "offer" and response.startswith(f"{actor} offered "):
                from .political_leverage import verify_finalized_offer
                verify_finalized_offer(
                    db, actor, room_id, arg, response,
                    action_id=action_id, receipt_command=receipt_command,
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
                 arg: str, *, emissions: list[tuple[int, str]] | None = None,
                 action_id: str | None = None,
                 private_channels: bool = True) -> str:
            if verb in {"pvp", "attack"}:
                from .combat import Combat
                if verb == "attack" or arg.casefold() == "on":
                    Combat.require_living_for_mutation(db, actor)
                return Combat.perform(db, engine=self, actor=actor, room=room_id,
                                      verb=verb, arg=arg, action_id=action_id)
            if verb not in {"quit", "exit", "look", "l", "examine", "x", "where",
                            "location", "inventory", "inv", "i", "who",
                            "history", "listen", "help", "?", "say",
                            "emote", "ooc", "crewchat", "allegiance", "factions", "sites", "control"}:
                from .combat import Combat
                # This is the connection-aware guard for trusted adapters
                # invoking _perform directly rather than Engine.act.
                Combat.require_living_for_mutation(db, actor)
            if verb in {"quit", "exit"}:
                return "Goodbye. Your position is saved."
            if verb in {"pledge", "renounce", "offer"} and not private_channels:
                return "Political pledges require player-bound credentials."
            if verb in {"allegiance", "factions", "pledge", "renounce"}:
                from .political_oaths import perform
                return perform(db, actor, room_id, verb, arg, self._event)
            if verb in {"sites", "control", "offer"}:
                from .political_leverage import perform
                return perform(db, actor, room_id, verb, arg, self._event)
            if verb in {"look", "l"} and (not arg or arg.casefold() in {"around", "here", "room"}):
                return self._look(db, room_id, actor)
            if verb in {"look", "l", "examine", "x"}:
                target = arg[3:] if arg.casefold().startswith("at ") else arg
                if not target:
                    return "Examine what?"
                item = db.execute(
                    "SELECT name,description FROM items "
                    "WHERE (room_id=? OR held_by=?) "
                    "AND (id=? COLLATE NOCASE OR name=? COLLATE NOCASE) "
                    "ORDER BY CASE WHEN room_id=? THEN 0 ELSE 1 END, id LIMIT 1",
                    (room_id, actor, target, target, room_id),
                ).fetchone()
                return (f"{item['name']}\n{item['description']}" if item is not None
                        else "You cannot examine that here.")
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
            if verb == "give":
                # Saved room positions establish a local transfer audience,
                # not a claim that the recipient is connected right now.
                target = re.fullmatch(r"(.+)\s+to\s+(\S+)", arg, flags=re.IGNORECASE)
                if target is None or not _PLAYER_RE.fullmatch(target.group(2)):
                    return "Use 'give <item> to <player>'."
                item_name, recipient_name = target.groups()
                if recipient_name.casefold() == actor.casefold():
                    return "You cannot give an item to yourself."
                recipient = db.execute(
                    "SELECT id FROM players WHERE id=? COLLATE NOCASE AND room_id=?",
                    (recipient_name, room_id),
                ).fetchone()
                if recipient is None:
                    return "Recipient is not in this room."
                # Explicit stable item IDs disambiguate duplicate display names.
                item = db.execute(
                    "SELECT id,name FROM items WHERE held_by=? AND id=? COLLATE NOCASE",
                    (actor, item_name),
                ).fetchone()
                if item is None:
                    matches = db.execute(
                        "SELECT id,name FROM items WHERE held_by=? AND name=? COLLATE NOCASE "
                        "ORDER BY id LIMIT 2",
                        (actor, item_name),
                    ).fetchall()
                    if len(matches) > 1:
                        return "Item name is ambiguous; use the item ID."
                    item = matches[0] if matches else None
                if item is None:
                    return "You are not carrying that item."
                changed = db.execute(
                    "UPDATE items SET held_by=? WHERE id=? AND held_by=? AND room_id IS NULL",
                    (recipient["id"], item["id"], actor),
                )
                if changed.rowcount != 1:
                    return "You are not carrying that item."
                self._event(db, actor, room_id, "give", {"item": item["id"], "to": recipient["id"]},
                            emissions=emissions)
                return f"You give {item['name']} to {recipient['id']}."
            if verb == "who":
                people = db.execute("SELECT id FROM players ORDER BY id COLLATE NOCASE").fetchall()
                return "Players who have joined this world: " + ", ".join(p[0] for p in people)
            if verb == "emote" and not private_channels:
                return "Channel posting requires player-bound credentials."
            if verb in {"ooc", "crewchat"}:
                if not private_channels:
                    return "Channel posting requires player-bound credentials."
                from .channel_roster import channel_room, event_text, member_for_actor
                message = "".join(c for c in arg if c.isprintable()).strip()
                if not message:
                    return "Say what to the channel?"
                if len(message) > 280:
                    return "Messages must be 280 characters or fewer."
                if verb == "ooc":
                    payload = {"text": message}
                    target = "@ooc"
                else:
                    member = member_for_actor(db, actor)
                    if member is None:
                        return "No active group channel membership."
                    payload = {"text": message, "group_id": member["group_id"],
                               "group_display": member["display_name"], "role": member["role"]}
                    target = channel_room(member["group_id"])
                self._event(db, actor, target, verb, payload, emissions=emissions)
                return event_text(actor, verb, payload)
            if verb == "say":
                message = "".join(c for c in arg if c.isprintable()).strip()
                if not message:
                    return "Say what?"
                if len(message) > 280:
                    return "Messages must be 280 characters or fewer."
                self._event(db, actor, room_id, "say", {"text": message},
                            emissions=emissions)
                return f"You say: {message}"
            if verb == "emote":
                action = "".join(c for c in arg if c.isprintable()).strip()
                if not action:
                    return "Emote what?"
                if len(action) > 280:
                    return "Emotes must be 280 characters or fewer."
                self._event(db, actor, room_id, "emote", {"text": action},
                            emissions=emissions)
                return f"{actor} {action}"
            if verb in {"history", "listen"}:
                from .channel_roster import event_text, visible_channel_events
                if arg and (not arg.isdigit() or not 1 <= int(arg) <= 50):
                    return "Use 'history' or 'history N' (1–50)."
                limit = int(arg) if arg else 10
                entry_id = self._room_entry_floor(db, actor, room_id)
                local = db.execute(
                    "SELECT id,actor,kind,payload_json FROM events "
                    "WHERE room_id=? AND kind NOT IN ('ooc','crewchat','chat_permission_epoch') "
                    "AND id>=? ORDER BY id DESC LIMIT ?",
                    (room_id, entry_id, limit),
                ).fetchall()
                channels = visible_channel_events(
                    db, actor, after=0, limit=limit, latest=True,
                    include_group=private_channels,
                )
                events = sorted(
                    [*local, *channels], key=lambda row: row["id"], reverse=True,
                )[:limit]
                items = []
                for event in reversed(events):
                    details = json.loads(event["payload_json"])
                    if event["kind"] in {"ooc", "crewchat"}:
                        items.append(event_text(event["actor"], event["kind"], details))
                    elif event["kind"] == "political_offer":
                        items.append(
                            f"{event['actor']} offered {details.get('item','an item')} "
                            f"to {details.get('faction','a faction')} at "
                            f"{details.get('site','a site')} "
                            f"({details.get('points',0)} influence)."
                        )
                    elif event["kind"] in {"political_pledge", "political_break"}:
                        verb_name = ("pledged allegiance to"
                                     if event["kind"] == "political_pledge"
                                     else "broke allegiance to")
                        items.append(f"{event['actor']} {verb_name} {details.get('faction','a faction')}.")
                    elif event["kind"] == "say":
                        items.append(f"{event['actor']} said: {details['text']}")
                    elif event["kind"] == "emote":
                        items.append(f"{event['actor']} {details['text']}")
                    elif event["kind"] == "move":
                        items.append(f"{event['actor']} arrived from {details['from']}")
                    elif event["kind"] == "leave":
                        items.append(f"{event['actor']} departed toward {details['to']}")
                    elif event["kind"] == "join":
                        items.append(f"{event['actor']} entered the world")
                    elif event["kind"] in {"take", "drop"}:
                        past = "took" if event["kind"] == "take" else "dropped"
                        items.append(f"{event['actor']} {past} {details['item']}")
                    elif event["kind"] == "give":
                        items.append(f"{event['actor']} gave {details['item']} to {details['to']}")
                return "\n".join(items) if items else "No recorded activity here yet."
            if verb in {"help", "?"}:
                return "Commands: look [item], examine <item> (x), go <north|south|east|west>, n/s/e/w, take <item>, drop <item>, give <item> to <player>, inventory, say <text>, emote <text>, ooc <text>, crewchat <text> (host-granted group), listen, history [1-50], who, where, help, quit. Optional host politics: factions, allegiance, pledge <faction>, renounce; optional contested sites: sites, control <site>, offer <item> to <site>. Optional host-enabled PvP: pvp status|news|on|off, attack <player>."
            return "Unknown command. Type 'help' for options."
