"""Optional, deterministic, host-owned REALM simulation clock (phase one).

This in-process SQLite scheduler has no model, network listener, background
thread or worldpack dependency. An authorized host explicitly installs it into
one previously imported world, registers *fresh* NPC players, and advances
bounded ticks. A model cannot choose actors, command text, receipts, drives,
lease epochs or the database. Only deterministic, host-authored `say` commands
are supported. No combat, faction or item effects are authorized.

The scheduler's atomic apply transaction holds the fencing check, Engine
effect, action receipt, drive update and terminal intent disposition together.
That removes the otherwise dangerous Engine.act -> cursor crash gap.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .actor_memory import ActorMemory
from .engine import Engine, _PLAYER_RE
from .residents import ResidentController


RULESET_VERSION = "realm.deterministic-drives.v1"
MAX_RESIDENTS = 8
_CURIOSITY_SPEECH = "say The unknown deserves a careful question."
_DUTY_SPEECH = "say Some duties deserve attention before curiosity."
_NPC_REGISTRY_DDL = """CREATE TABLE IF NOT EXISTS npc_actor_registry (
    actor TEXT PRIMARY KEY COLLATE NOCASE REFERENCES players(id),
    persona TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""
_SCHEMA = """
CREATE TABLE IF NOT EXISTS simulation_control (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    world_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    planned_through INTEGER NOT NULL DEFAULT 0 CHECK(planned_through>=0),
    finalized_through INTEGER NOT NULL DEFAULT 0 CHECK(finalized_through>=0 AND finalized_through<=planned_through),
    lease_token TEXT,
    lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),
    lease_expires_unix INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS simulation_drives (
    actor TEXT PRIMARY KEY COLLATE NOCASE REFERENCES players(id),
    curiosity INTEGER NOT NULL CHECK(curiosity BETWEEN 0 AND 100),
    duty INTEGER NOT NULL CHECK(duty BETWEEN 0 AND 100),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0)
);
CREATE TABLE IF NOT EXISTS simulation_ticks (
    tick_seq INTEGER PRIMARY KEY CHECK(tick_seq>0),
    ruleset_version TEXT NOT NULL,
    resident_count INTEGER NOT NULL CHECK(resident_count>=0),
    status TEXT NOT NULL CHECK(status IN ('proposed','terminal'))
);
CREATE TABLE IF NOT EXISTS simulation_intents (
    tick_seq INTEGER NOT NULL REFERENCES simulation_ticks(tick_seq),
    actor TEXT NOT NULL REFERENCES simulation_drives(actor),
    action_id TEXT NOT NULL,
    expected_room TEXT NOT NULL REFERENCES rooms(id),
    required_listener TEXT REFERENCES players(id),
    drive_revision INTEGER NOT NULL CHECK(drive_revision>=0),
    command TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','committed','rejected')),
    response TEXT,
    PRIMARY KEY(tick_seq,actor),
    UNIQUE(actor,action_id)
);
"""


class LeaseHeld(RuntimeError):
    """Another local host controller currently owns the simulation lease."""


class LeaseLost(RuntimeError):
    """A lease has expired or a newer fencing epoch superseded this owner."""


class SimulationIntegrityError(RuntimeError):
    """Persisted simulation state conflicts with the expected host contract."""


@dataclass(frozen=True)
class TickOutcome:
    tick_seq: int
    actor: str
    action_id: str
    status: str
    response: str


@dataclass(frozen=True)
class NPCProposalSnapshot:
    """Host-read snapshot; not a grant of authority to the provider."""

    actor: str
    persona: str
    room_id: str
    room_name: str
    room_description: str
    curiosity: int
    duty: int
    drive_revision: int
    world_instance_id: str
    next_tick_seq: int


@dataclass(frozen=True)
class HostProposedAction:
    """Typed untrusted payload bound to a host-selected identity and receipt."""

    snapshot: NPCProposalSnapshot
    proposal: Mapping[str, object]
    action_id: str
    required_listener: str | None = None


def _digest_proposal(*, instance_id: str, tick_seq: int, actor: str,
                     expected_room: str, drive_revision: int, command: str,
                     required_listener: str | None = None) -> str:
    payload = {
        "instance_id": instance_id,
        "tick_seq": tick_seq,
        "actor": actor,
        "room": expected_room,
        "drive_revision": drive_revision,
        "command": command,
        "ruleset_version": RULESET_VERSION,
    }
    if required_listener is not None:
        payload["required_listener"] = required_listener
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _statements(script: str):
    # Fixed trusted DDL only. `sqlite3.executescript` would commit an open
    # transaction and violate all-or-nothing installation.
    return (statement for part in script.split(";") if (statement := part.strip()))


def _expected_schema() -> dict[str, str]:
    with closing(sqlite3.connect(":memory:")) as db:
        for statement in _statements(_SCHEMA):
            db.execute(statement)
        return {name: " ".join(ddl.split()) for name, ddl in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name LIKE 'simulation_%'"
        )}


def _expected_npc_registry() -> str:
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute(_NPC_REGISTRY_DDL)
        return " ".join(db.execute(
            "SELECT sql FROM sqlite_master WHERE name='npc_actor_registry'"
        ).fetchone()[0].split())


class SimulationClock:
    """A trusted in-process controller, never an API endpoint or model tool."""

    def __init__(self, engine: Engine, *, expected_world_id: str) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("SimulationClock requires a REALM Engine")
        if not isinstance(expected_world_id, str) or not expected_world_id.strip():
            raise ValueError("Explicit expected world identity is required")
        self.engine = engine
        self.expected_world_id = expected_world_id
        self._owner_token = secrets.token_urlsafe(24)
        self._lease_epoch: int | None = None

    @staticmethod
    def _now(db: sqlite3.Connection) -> int:
        # Liveness TTL is local SQLite-clock metadata, not a deterministic
        # simulation input or proof of clock consistency across machines.
        return int(db.execute("SELECT CAST(strftime('%s','now') AS INTEGER)").fetchone()[0])

    def _world_matches(self, db: sqlite3.Connection) -> None:
        bound = db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()
        if bound is None or bound[0] != self.expected_world_id:
            raise ValueError("World database is not bound to expected_world_id")

    def install(self) -> str:
        """Explicit opt-in schema installation; fails without modifying wrong worlds."""
        expected = _expected_schema()
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._world_matches(db)
            current = {
                name: " ".join(ddl.split()) for name, ddl in db.execute(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'simulation_%'"
                )
            }
            if current and current != expected:
                raise SimulationIntegrityError("Simulation SQL schema is incomplete or unexpected")
            existing_npc_registry = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='npc_actor_registry'"
            ).fetchone()
            if existing_npc_registry is not None and (
                " ".join(existing_npc_registry[0].split()) != _expected_npc_registry()
            ):
                raise SimulationIntegrityError("NPC principal-reservation schema differs")
            # Use PR23's exact NPC reservation schema. Engine.join, Telnet and
            # CredentialStore.provision enforce this table under writer locks.
            db.execute(_NPC_REGISTRY_DDL)
            for statement in _statements(_SCHEMA):
                db.execute(statement)
            control = db.execute("SELECT * FROM simulation_control WHERE singleton=1").fetchone()
            if control is None:
                instance_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO simulation_control "
                    "(singleton,world_id,instance_id,ruleset_version) VALUES (1,?,?,?)",
                    (self.expected_world_id, instance_id, RULESET_VERSION),
                )
                return instance_id
            if control["world_id"] != self.expected_world_id or control["ruleset_version"] != RULESET_VERSION:
                raise SimulationIntegrityError("Persisted simulation world/ruleset binding differs")
            return control["instance_id"]

    def _control(self, db: sqlite3.Connection) -> sqlite3.Row:
        self._world_matches(db)
        row = db.execute("SELECT * FROM simulation_control WHERE singleton=1").fetchone()
        if row is None:
            raise ValueError("Host must explicitly install the simulation clock")
        if row["world_id"] != self.expected_world_id or row["ruleset_version"] != RULESET_VERSION:
            raise SimulationIntegrityError("Simulation world/ruleset changed")
        return row

    def _fence(self, db: sqlite3.Connection) -> sqlite3.Row:
        row = self._control(db)
        if (self._lease_epoch is None or row["lease_token"] != self._owner_token
                or row["lease_epoch"] != self._lease_epoch
                or row["lease_expires_unix"] <= self._now(db)):
            raise LeaseLost("Simulation scheduler lease is expired or superseded")
        return row

    def acquire_lease(self, *, ttl_seconds: int = 30) -> int:
        """Acquire/renew a local SQLite lease with monotonically fenced epoch."""
        if type(ttl_seconds) is not int or not 5 <= ttl_seconds <= 300:
            raise ValueError("Lease TTL must be 5–300 whole seconds")
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._control(db)
            now = self._now(db)
            owned = row["lease_token"] == self._owner_token and row["lease_expires_unix"] > now
            if not owned and row["lease_token"] is not None and row["lease_expires_unix"] > now:
                raise LeaseHeld("Another scheduler holds this world's simulation lease")
            epoch = row["lease_epoch"] if owned else row["lease_epoch"] + 1
            db.execute(
                "UPDATE simulation_control SET lease_token=?,lease_epoch=?,lease_expires_unix=? WHERE singleton=1",
                (self._owner_token, epoch, now + ttl_seconds),
            )
            self._lease_epoch = epoch
            return epoch

    def register_npc(self, name: str, *, persona: str, curiosity: int, duty: int) -> None:
        """Host-provision one *fresh*, non-credentialed NPC; never adopt a human."""
        if not isinstance(name, str) or not _PLAYER_RE.fullmatch(name):
            raise ValueError("NPC name must follow REALM player naming rules")
        if (not isinstance(persona, str) or not 1 <= len(persona) <= 240
                or not all(ch.isprintable() for ch in persona) or not persona.strip()):
            raise ValueError("NPC persona must be a printable one-line description (1–240 chars)")
        if any(type(x) is not int or not 0 <= x <= 100 for x in (curiosity, duty)):
            raise ValueError("Host drive values must be whole numbers in 0–100")
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            control = self._control(db)
            if control["planned_through"] != control["finalized_through"]:
                raise ValueError("Cannot register NPC while a tick is unresolved")
            if db.execute("SELECT COUNT(*) FROM simulation_drives").fetchone()[0] >= MAX_RESIDENTS:
                raise ValueError("Phase-one simulation resident cap exceeded")
            if db.execute("SELECT 1 FROM players WHERE id=? COLLATE NOCASE", (name,)).fetchone():
                raise ValueError("NPC name collides with existing human/resident player")
            has_creds = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='player_credentials'"
            ).fetchone()
            if has_creds and db.execute(
                "SELECT 1 FROM player_credentials WHERE player_name=? COLLATE NOCASE", (name,)
            ).fetchone():
                raise ValueError("NPC name collides with a credentialed player")
            room_meta = db.execute("SELECT value FROM world_meta WHERE key='start_room'").fetchone()
            room = room_meta[0] if room_meta else "atrium"
            if not db.execute("SELECT 1 FROM rooms WHERE id=?", (room,)).fetchone():
                raise ValueError("World start room does not exist")
            db.execute(
                "INSERT INTO players(id,room_id,created_at) VALUES (?,?,?)",
                (name, room, self.engine._now()),
            )
            # The shared PR23/PR28 public event-shape contract permits this
            # exact marker. Do not introduce unreviewed join metadata.
            self.engine._event(db, name, room, "join", {"resident": True})
            db.execute(
                "INSERT INTO npc_actor_registry(actor,persona,created_at) VALUES (?,?,?)",
                (name, persona, self.engine._now()),
            )
            db.execute(
                "INSERT INTO simulation_drives(actor,curiosity,duty,revision) VALUES (?,?,?,0)",
                (name, curiosity, duty),
            )

    def snapshot_npc_actor(self, actor: str) -> NPCProposalSnapshot:
        """Read host facts for a provider prompt; never authorize from this read."""
        if not isinstance(actor, str) or not _PLAYER_RE.fullmatch(actor):
            raise ValueError("NPC actor name is invalid")
        with closing(self.engine._connect()) as db:
            control = self._control(db)
            row = db.execute(
                "SELECT p.id,p.room_id,r.name AS room_name,r.description AS room_description,"
                "n.persona,d.curiosity,d.duty,d.revision "
                "FROM simulation_drives d JOIN players p ON p.id=d.actor "
                "JOIN npc_actor_registry n ON n.actor=p.id "
                "JOIN rooms r ON r.id=p.room_id WHERE p.id=? COLLATE NOCASE",
                (actor,),
            ).fetchone()
            if row is None or row["id"] != actor:
                raise ValueError("Actor is not an exact host-registered simulation NPC")
            return NPCProposalSnapshot(
                actor=row["id"], persona=row["persona"], room_id=row["room_id"],
                room_name=row["room_name"], room_description=row["room_description"],
                curiosity=row["curiosity"], duty=row["duty"], drive_revision=row["revision"],
                world_instance_id=control["instance_id"],
                next_tick_seq=control["planned_through"] + 1,
            )

    def plan_next_tick(self, *, host_actions: tuple[HostProposedAction, ...] | None = None) -> int:
        """Persist bounded proposals before effects, never model-chosen authority.

        With no host actions, preserve the deterministic model-disabled tick.
        Otherwise trusted host code binds one or more untrusted `say` payloads
        to exact NPC/room/drive snapshots and host-owned action receipts.
        Slow provider inference must occur before this method, outside locks.
        """
        if host_actions is not None:
            if (not isinstance(host_actions, tuple) or not 1 <= len(host_actions) <= MAX_RESIDENTS
                    or any(not isinstance(action, HostProposedAction)
                           or not isinstance(action.snapshot, NPCProposalSnapshot)
                           for action in host_actions)):
                raise ValueError("Host actions must be a bounded tuple of typed proposals")
            names = [action.snapshot.actor for action in host_actions]
            if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
                raise ValueError("Host may propose only once per distinct actor per tick")
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            control = self._fence(db)
            if control["planned_through"] != control["finalized_through"]:
                raise ValueError("Finish or reject the existing tick before planning another")
            all_residents = db.execute(
                "SELECT d.actor,d.curiosity,d.duty,d.revision,p.room_id FROM simulation_drives d "
                "JOIN players p ON p.id=d.actor ORDER BY d.actor COLLATE NOCASE"
            ).fetchall()
            if len(all_residents) > MAX_RESIDENTS:
                raise ValueError("Phase-one simulation resident cap exceeded")
            roster = {row["actor"]: row for row in all_residents}
            if host_actions is None:
                residents = all_residents
                authorized = {}
            else:
                if any(name not in roster for name in names):
                    raise ValueError("Host proposal actor is not a registered simulation resident")
                residents = [roster[name] for name in names]
                authorized = {action.snapshot.actor: action for action in host_actions}
            tick = control["planned_through"] + 1
            # Check the full proposed batch under one writer lock BEFORE any
            # tick/intent row is created; a stale or forged model output cannot
            # leave a partially planned simulation tick.
            checked: dict[str, tuple[str, str, str | None]] = {}
            for row in residents:
                if host_actions is None:
                    command = _CURIOSITY_SPEECH if row["curiosity"] >= row["duty"] else _DUTY_SPEECH
                    action_id = f"sim:{control['instance_id']}:{tick}:{row['actor']}"
                    listener = None
                else:
                    action = authorized[row["actor"]]
                    snap = action.snapshot
                    if (snap.actor != row["actor"] or snap.room_id != row["room_id"]
                            or snap.drive_revision != row["revision"]
                            or snap.curiosity != row["curiosity"] or snap.duty != row["duty"]
                            or snap.world_instance_id != control["instance_id"]
                            or snap.next_tick_seq != tick):
                        raise ValueError("Model proposal snapshot became stale before host planning")
                    # Provider never supplies `action_id`, `actor`, `listener`,
                    # `world_id` or privileges: they are host action fields.
                    command = ResidentController(
                        self.engine, row["actor"], allowed_verbs=frozenset({"say"}),
                    ).validate(action.proposal).command
                    action_id = action.action_id
                    listener = action.required_listener
                    if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
                        raise ValueError("Host action IDs must be 1–128 characters")
                    if listener is not None:
                        if (not isinstance(listener, str) or not _PLAYER_RE.fullmatch(listener)
                                or listener.casefold() == row["actor"].casefold()):
                            raise ValueError("Required listener must be a distinct host NPC")
                        peer = db.execute(
                            "SELECT p.id,p.room_id FROM players p "
                            "JOIN simulation_drives d ON d.actor=p.id "
                            "JOIN npc_actor_registry n ON n.actor=p.id "
                            "WHERE p.id=? COLLATE NOCASE", (listener,),
                        ).fetchone()
                        if peer is None or peer["id"] != listener or peer["room_id"] != row["room_id"]:
                            raise ValueError("Required listener moved or is not registered with this room")
                    if db.execute(
                        "SELECT 1 FROM action_receipts WHERE actor=? AND action_id=?",
                        (row["actor"], action_id),
                    ).fetchone():
                        raise SimulationIntegrityError("Host action ID is already committed elsewhere")
                checked[row["actor"]] = (command, action_id, listener)
            status = "proposed" if residents else "terminal"
            db.execute(
                "INSERT INTO simulation_ticks(tick_seq,ruleset_version,resident_count,status) VALUES (?,?,?,?)",
                (tick, RULESET_VERSION, len(residents), status),
            )
            for row in residents:
                command, action_id, listener = checked[row["actor"]]
                digest = _digest_proposal(
                    instance_id=control["instance_id"], tick_seq=tick, actor=row["actor"],
                    expected_room=row["room_id"], drive_revision=row["revision"],
                    command=command, required_listener=listener,
                )
                db.execute(
                    "INSERT INTO simulation_intents "
                    "(tick_seq,actor,action_id,expected_room,required_listener,drive_revision,command,proposal_sha256,status) "
                    "VALUES (?,?,?,?,?,?,?,?,'proposed')",
                    (tick, row["actor"], action_id, row["room_id"], listener, row["revision"], command, digest),
                )
            db.execute(
                "UPDATE simulation_control SET planned_through=?,finalized_through=? WHERE singleton=1",
                (tick, tick if not residents else control["finalized_through"]),
            )
            return tick

    def pending_tick(self) -> int | None:
        """Read-only inspection; does not claim ownership or acquire a lease."""
        with closing(self.engine._connect()) as db:
            control = self._control(db)
            if control["finalized_through"] == control["planned_through"]:
                return None
            return control["finalized_through"] + 1

    def _grant_speech_observations(self, db: sqlite3.Connection, *, actor: str,
                                   room_id: str, event_id: int, command: str) -> None:
        """One event, authorized co-room simulation NPCs, same write txn.

        Memory remains opt-in: without ActorMemory tables there is no grant.
        Half-installed memory schemas must fail closed and roll back speech.
        Humans are never auto-granted from durable room membership, which
        does not prove live presence or consent.
        """
        present = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('actor_memory_grants','actor_memory_summaries')"
        )}
        marker = db.execute(
            "SELECT value FROM world_meta WHERE key='actor_memory_enabled'"
        ).fetchone()
        if not present:
            if marker is not None:
                raise SimulationIntegrityError("Actor memory was enabled but its schema is missing")
            return
        if marker is not None and marker[0] != "1":
            raise SimulationIntegrityError("Actor memory opt-in marker is invalid")
        if present != {"actor_memory_grants", "actor_memory_summaries"}:
            raise SimulationIntegrityError("Actor memory schema is incomplete; denying speech without grants")
        source = db.execute(
            "SELECT actor,room_id,kind,payload_json FROM events WHERE id=?", (event_id,),
        ).fetchone()
        if (source is None or source["actor"] != actor or source["room_id"] != room_id
                or source["kind"] != "say"):
            raise SimulationIntegrityError("Speech event ID does not match the host action")
        try:
            text = json.loads(source["payload_json"])["text"]
        except (ValueError, TypeError, KeyError) as exc:
            raise SimulationIntegrityError("Speech event has malformed payload") from exc
        if text != command.partition(" ")[2].strip():
            raise SimulationIntegrityError("Speech source differs from accepted command")
        observers = db.execute(
            "SELECT p.id FROM simulation_drives d "
            "JOIN npc_actor_registry n ON n.actor=d.actor "
            "JOIN players p ON p.id=d.actor "
            "WHERE p.room_id=? ORDER BY p.id COLLATE NOCASE", (room_id,),
        ).fetchall()
        if actor not in {observer["id"] for observer in observers}:
            raise SimulationIntegrityError("Source actor lacks registered NPC observation principal")
        for observer in observers:
            ActorMemory._grant_new_speech_in_transaction(
                db, world_id=self.expected_world_id, observer=observer["id"], event_id=event_id,
            )

    def apply_pending_tick(self) -> list[TickOutcome]:
        """Atomically guard each effect, receipt, drive change and disposition."""
        outcomes: list[TickOutcome] = []
        while True:
            with closing(self.engine._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                control = self._fence(db)
                if control["finalized_through"] == control["planned_through"]:
                    return outcomes
                tick = control["finalized_through"] + 1
                row = db.execute(
                    "SELECT * FROM simulation_intents WHERE tick_seq=? AND status='proposed' "
                    "ORDER BY actor COLLATE NOCASE LIMIT 1", (tick,),
                ).fetchone()
                if row is None:
                    # Other workers cannot update an intent without this same
                    # SQLite writer lock. Only a fully terminal tick advances.
                    remaining = db.execute(
                        "SELECT COUNT(*) FROM simulation_intents WHERE tick_seq=? AND status='proposed'",
                        (tick,),
                    ).fetchone()[0]
                    if remaining:
                        raise SimulationIntegrityError("Unresolved simulation intents remain")
                    db.execute("UPDATE simulation_ticks SET status='terminal' WHERE tick_seq=?", (tick,))
                    db.execute(
                        "UPDATE simulation_control SET finalized_through=? WHERE singleton=1",
                        (tick,),
                    )
                    continue
                digest = _digest_proposal(
                    instance_id=control["instance_id"], tick_seq=tick, actor=row["actor"],
                    expected_room=row["expected_room"], drive_revision=row["drive_revision"],
                    command=row["command"], required_listener=row["required_listener"],
                )
                if digest != row["proposal_sha256"]:
                    raise SimulationIntegrityError("Persisted proposal checksum does not match")
                # The untrusted proposal boundary remains explicit even though
                # phase one has only trusted deterministic speech templates.
                accepted = ResidentController(
                    self.engine, row["actor"], allowed_verbs=frozenset({"say"}),
                ).validate({"command": row["command"]})
                player = db.execute(
                    "SELECT id,room_id FROM players WHERE id=? COLLATE NOCASE", (row["actor"],)
                ).fetchone()
                drives = db.execute(
                    "SELECT curiosity,duty,revision FROM simulation_drives WHERE actor=? COLLATE NOCASE",
                    (row["actor"],),
                ).fetchone()
                valid = (player is not None and player["id"] == row["actor"]
                         and player["room_id"] == row["expected_room"]
                         and drives is not None and drives["revision"] == row["drive_revision"])
                if valid and row["required_listener"] is not None:
                    peer = db.execute(
                        "SELECT p.id,p.room_id FROM players p "
                        "JOIN simulation_drives d ON d.actor=p.id "
                        "JOIN npc_actor_registry n ON n.actor=p.id "
                        "WHERE p.id=? COLLATE NOCASE", (row["required_listener"],),
                    ).fetchone()
                    valid = (peer is not None and peer["id"] == row["required_listener"]
                             and peer["room_id"] == row["expected_room"])
                if not valid:
                    response, status = "Stale room, listener or drive revision; proposal rejected.", "rejected"
                else:
                    preexisting = db.execute(
                        "SELECT 1 FROM action_receipts WHERE actor=? AND action_id=?",
                        (row["actor"], row["action_id"]),
                    ).fetchone()
                    if preexisting is not None:
                        raise SimulationIntegrityError("Unfinalized intent already has a receipt")
                    verb, _, arg = accepted.command.partition(" ")
                    response = self.engine._perform(
                        db, row["actor"], player["room_id"], verb.casefold(), arg.strip(),
                    )
                    if not response.startswith("You say: "):
                        raise SimulationIntegrityError("Deterministic speech did not commit")
                    # SQLite last_insert_rowid() is connection-scoped. `say`
                    # inserted exactly one event in this same write txn; bind
                    # and verify it before any receipt/drive update. No room
                    # history MAX lookup after another action is involved.
                    event_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                    self._grant_speech_observations(
                        db, actor=row["actor"], room_id=player["room_id"],
                        event_id=event_id, command=accepted.command,
                    )
                    db.execute(
                        "INSERT INTO action_receipts(actor,action_id,command,response) VALUES (?,?,?,?)",
                        (row["actor"], row["action_id"], accepted.command, response),
                    )
                    # Drive updates are part of the *same* transaction and
                    # therefore cannot double-apply on crash/restart.
                    if drives["curiosity"] >= drives["duty"]:
                        next_curiosity, next_duty = max(0, drives["curiosity"] - 2), min(100, drives["duty"] + 1)
                    else:
                        next_curiosity, next_duty = min(100, drives["curiosity"] + 1), max(0, drives["duty"] - 2)
                    db.execute(
                        "UPDATE simulation_drives SET curiosity=?,duty=?,revision=revision+1 WHERE actor=?",
                        (next_curiosity, next_duty, row["actor"]),
                    )
                    status = "committed"
                db.execute(
                    "UPDATE simulation_intents SET status=?,response=? WHERE tick_seq=? AND actor=?",
                    (status, response, tick, row["actor"]),
                )
                outcomes.append(TickOutcome(
                    tick_seq=tick, actor=row["actor"], action_id=row["action_id"],
                    status=status, response=response,
                ))
