"""Opt-in, host-driven REALM deadline queue using UTC and one world SQLite DB.

The host must call enable() explicitly *after* initial world import. No thread,
timer, daemon, player route or public action is created here. Each tick calls a
trusted host handler in the SAME SQLite transaction as its completion receipt.
Only SQLite effects made through that connection are atomic with the receipt.
External messages, model output, RPC and network effects are NOT exactly once;
use a transactional outbox plus idempotent external consumer for those.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .ship_history_custody import ShipHistoryCustody

_EVENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_KIND = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_PAYLOAD_BYTES = 4096
_MAX_TICK_BATCH = 1000
_MAX_FORWARD_SECONDS = 86400
_UTC = timezone.utc


def _nonclock_world_state(db: sqlite3.Connection) -> tuple[tuple, dict]:
    """World-local persistent state guarded across scheduler receipt writes.

    A selected trusted callback can make its own validated state transition;
    the clock observes that state *before* writing the processed receipt.
    Neither its receipt nor its final UTC watermark grants any additional
    gameplay, credential, geography, crafting or host-extension mutation.
    Core events are guarded separately so unrelated new `say` history remains
    permitted; clock tables have their own exact receipt/watermark checks.
    """
    schema = tuple(tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ))
    # These have stronger existing typed or historical checks and specific
    # rollback failures; do not preempt their established diagnostics.
    ignored = frozenset({"events", "clock_events", "clock_state",
                         "gathering_nodes", "material_pouches",
                         "geo_ships", "geo_ship_events", "geo_ship_receipts"})
    tables = {}
    for kind, name, _owner, _sql in schema:
        if kind != "table" or name in ignored:
            continue
        quoted_name = '"' + name.replace('"', '""') + '"'
        tables[name] = Counter(tuple(row) for row in db.execute(
            "SELECT * FROM main." + quoted_name))
    return schema, tables


def _autoincrement_state(db: sqlite3.Connection) -> Counter | None:
    """Snapshot SQLite's durable allocator, which ordinary table guards skip."""
    if db.execute("SELECT 1 FROM main.sqlite_schema "
                  "WHERE type='table' AND name='sqlite_sequence'").fetchone():
        return Counter(tuple(row) for row in db.execute(
            "SELECT name,seq FROM main.sqlite_sequence"))
    return None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clock_events (
    event_id TEXT PRIMARY KEY,
    due_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    processed_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_clock_due
    ON clock_events(due_utc, event_id) WHERE processed_utc IS NULL;
CREATE TABLE IF NOT EXISTS clock_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    last_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS npc_roster_utc_attestations (
    event_id TEXT PRIMARY KEY REFERENCES clock_events(event_id),
    world_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    due_utc TEXT NOT NULL,
    outcome_sha256 TEXT NOT NULL,
    grant_mac_hex TEXT NOT NULL,
    execution_mac_hex TEXT NOT NULL
);
"""


class ClockSkewError(ValueError):
    """UTC wall clock moved backward or unusually far forward."""


@dataclass(frozen=True)
class DueEvent:
    event_id: str
    due_at: datetime
    kind: str
    payload: dict
    processed_at: datetime | None = None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("A timezone-aware UTC datetime is required")
    return value.astimezone(_UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _payload(value: dict) -> str:
    if type(value) is not dict:
        raise ValueError("Event payload must be a JSON object")
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Event payload must be finite valid JSON") from exc
    try:
        encoded = canonical.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Event payload contains invalid Unicode") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Event payload exceeds 4096 UTF-8 bytes")
    return canonical


def _event(row: sqlite3.Row) -> DueEvent:
    return DueEvent(
        event_id=row["event_id"], due_at=datetime.fromisoformat(row["due_utc"]),
        kind=row["kind"], payload=json.loads(row["payload_json"]),
        processed_at=(datetime.fromisoformat(row["processed_utc"])
                      if row["processed_utc"] is not None else None),
    )



def _verify_roster_utc_ack(db: sqlite3.Connection, event: DueEvent, outcome: object) -> tuple[str, ...]:
    """Refuse to acknowledge a roster event without exact in-transaction receipts.

    The host's typed result is necessary but not sufficient: a swallowed handler
    exception, stale result, or omitted actor receipt must never advance the UTC
    queue. WAIT is valid within a signed execution; all-WAIT is currently invalid
    because the planner refuses worlds without harvestable resources.
    """
    from .npc_roster_utc_policy import NpcRosterGatherResult, _roster_result_sha256

    if type(outcome) is not NpcRosterGatherResult or outcome.event_id != event.event_id:
        raise ValueError("Roster host callback did not return exact completed outcome")
    assignments = outcome.proposal.assignments
    if not 2 <= len(assignments) <= 8:
        raise ValueError("Roster completion requires a bounded actor plan")
    actions = tuple(a for a in assignments if a.site is not None)
    if (outcome.executed != len(actions)
            or outcome.waiting != len(assignments) - len(actions)
            or len(outcome.responses) != len(actions)):
        raise ValueError("Roster completion counts or WAIT status do not match plan")
    if len(set(a.actor.casefold() for a in assignments)) != len(assignments):
        raise ValueError("Roster completion repeats an actor")
    if not actions:
        raise ValueError("All-WAIT roster cannot come from current harvest planner")
    world_row = db.execute(
        "SELECT value FROM main.world_meta WHERE key='world_id'"
    ).fetchone()
    if (world_row is None or outcome.proposal.world_id != world_row["value"]
            or event.payload.get("world_id") != world_row["value"]):
        raise ValueError("Roster outcome is not bound to authoritative world")
    for assignment, response in zip(actions, outcome.responses):
        action_id = "npcR_" + hashlib.sha256(
            f"{event.event_id}|{assignment.actor}".encode("utf-8")
        ).hexdigest()[:50]
        receipt = db.execute(
            "SELECT command,response FROM main.action_receipts "
            "WHERE actor=? AND action_id=?",
            (assignment.actor, action_id),
        ).fetchone()
        if (receipt is None
                or receipt["command"] != "gather " + assignment.site.material
                or receipt["response"] != response):
            raise ValueError("Roster clock receipt lacks exact actor action receipt")
    attest = db.execute(
        "SELECT world_id,policy_id,due_utc,outcome_sha256,grant_mac_hex,execution_mac_hex "
        "FROM main.npc_roster_utc_attestations WHERE event_id=?",
        (event.event_id,),
    ).fetchone()
    if (attest is None
            or attest["world_id"] != world_row["value"]
            or attest["policy_id"] != event.payload.get("policy_id")
            or attest["due_utc"] != _stamp(event.due_at)
            or attest["outcome_sha256"] != _roster_result_sha256(outcome)
            or any(not re.fullmatch(r"[0-9a-f]{64}", attest[key])
                   for key in ("grant_mac_hex", "execution_mac_hex"))):
        raise ValueError("Roster clock receipt lacks bound host execution attestation")
    # Preserve both MAC fields exactly across later trigger/callback DML.
    # The clock has no host key; the executor checked the grant before writing.
    return tuple(attest)


class WorldClock:
    """One per-world database; host-only, disabled until explicit enable().

    The host must perform worldpack import before enable(). Rollback of the
    *entire SQLite file* can roll back queue state; no external witness exists.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def enable(self) -> None:
        """Opt in only after an initialized world is bound/imported."""
        if not self.db_path.is_file():
            raise RuntimeError("Clock requires an initialized, bound world before enable")
        with closing(sqlite3.connect(str(self.db_path), timeout=10)) as db, db:
            db.execute("PRAGMA busy_timeout=10000")
            has_meta = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_meta'"
            ).fetchone()
            if not has_meta or not db.execute(
                "SELECT 1 FROM world_meta WHERE key='world_id' AND length(value)>0"
            ).fetchone():
                raise RuntimeError("Clock requires an initialized, bound world before enable")
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        if not self.db_path.is_file():
            raise RuntimeError("World clock must be explicitly enabled")
        db = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clock_events'"
            ).fetchone()
            if exists is None:
                raise RuntimeError("World clock must be explicitly enabled")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
        finally:
            db.close()

    def schedule(self, event_id: str, *, due_at: datetime,
                 kind: str, payload: dict) -> DueEvent:
        """Idempotently enqueue a host-validated event, or reject key reuse."""
        if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
            raise ValueError("Invalid event_id")
        if not isinstance(kind, str) or _KIND.fullmatch(kind) is None:
            raise ValueError("Invalid event kind")
        due = _stamp(due_at)
        data = _payload(payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM clock_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if (existing["due_utc"], existing["kind"], existing["payload_json"]) != (due, kind, data):
                    raise ValueError("Event ID already used for a different event")
                return _event(existing)
            db.execute(
                "INSERT INTO clock_events(event_id,due_utc,kind,payload_json) VALUES(?,?,?,?)",
                (event_id, due, kind, data),
            )
            return DueEvent(event_id, datetime.fromisoformat(due), kind, json.loads(data))

    def lookup(self, event_id: str) -> DueEvent | None:
        """Read a queued/completed event without modifying world state."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM clock_events WHERE event_id=?", (event_id,)
            ).fetchone()
            return _event(row) if row else None

    def tick(self, now_utc: datetime, handler: Callable[[sqlite3.Connection, DueEvent], object],
             *, limit: int = 100, acknowledge_forward_jump: bool = False) -> list[DueEvent]:
        """Apply at most limit due effects AND receipts in one SQLite transaction.

        Handler is host-trusted, must use the supplied db and MUST NOT commit,
        roll back, close, attach unrelated databases, or emit external effects.
        Exceptions abort the whole batch. Handler's work is not authorization.
        """
        now = _utc(now_utc)
        if type(acknowledge_forward_jump) is not bool:
            raise ValueError("Forward-jump acknowledgment must be a boolean")
        if type(limit) is not int or not 1 <= limit <= _MAX_TICK_BATCH:
            raise ValueError("Tick limit must be an integer from 1 to 1000")
        if not callable(handler):
            raise ValueError("Tick handler must be callable")
        stamp = _stamp(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT last_utc FROM main.clock_state WHERE id=1").fetchone()
            if state is not None:
                prior = datetime.fromisoformat(state["last_utc"])
                elapsed = (now - prior).total_seconds()
                if elapsed < 0:
                    raise ClockSkewError("UTC wall clock moved backward")
                if elapsed > _MAX_FORWARD_SECONDS and not acknowledge_forward_jump:
                    raise ClockSkewError("UTC advanced more than 24 hours; host acknowledgment required")
            # Snapshot every pre-existing scheduler row in Python memory.
            # A callback's TEMP trigger could otherwise pre-complete, delete,
            # or rewrite an event outside this tick's selected batch.
            before_events = {
                row["event_id"]: (
                    row["due_utc"], row["kind"], row["payload_json"], row["processed_utc"]
                )
                for row in db.execute(
                    "SELECT event_id,due_utc,kind,payload_json,processed_utc "
                    "FROM main.clock_events"
                )
            }
            # The ship ledger belongs to this world and must remain stable on
            # *every* tick, not only when one selected event is an arrival.
            # Snapshot before any callback/clock receipt trigger can touch it.
            ship_custody = ShipHistoryCustody(db)
            rows = db.execute(
                "SELECT * FROM main.clock_events WHERE processed_utc IS NULL AND due_utc <= ? "
                "ORDER BY due_utc, event_id LIMIT ?", (stamp, limit),
            ).fetchall()
            processed = []
            roster_proofs = []
            # Economy history is durable across *every* host UTC tick, not
            # only batches selecting a fresh signed roster. A later audit-only
            # or idle clock trigger must not rewrite already signed gathers,
            # change stock/pouches or forge a new gather and still gain ACK.
            # Preserve the complete historical event rows (not just kind=gather)
            # so kind rewrites cannot evade postconditions. Ordinary *new* say
            # events remain allowed; a new gather needs a selected signed
            # roster grant or a separately governed legacy host action (see
            # the bounded legacy exception below). Legacy clocks without
            # economy tables remain usable.
            roster_in_batch = any(r["kind"] == "npc_roster_gather" for r in rows)
            # Selected legacy host-action kinds can have real gather/craft
            # effects validated by their own adapter. The clock captures
            # those effects immediately after each callback, while separate
            # typed adapter proof must qualify the in-handler transition.
            economy_tables = {
                r["name"] for r in db.execute(
                    "SELECT name FROM main.sqlite_master WHERE type='table' "
                    "AND name IN ('events','gathering_nodes','material_pouches')"
                )
            }
            if roster_in_batch and economy_tables != {
                    "events", "gathering_nodes", "material_pouches"}:
                raise ValueError("Roster tick requires complete economy storage")
            event_ledger_before = (
                {
                    r["id"]: tuple(r)
                    for r in db.execute("SELECT * FROM main.events")
                }
                if "events" in economy_tables
                else None
            )
            node_before = (
                {
                    (r["room_id"], r["material"]): (r["remaining"], r["batch_yield"])
                    for r in db.execute(
                        "SELECT room_id,material,remaining,batch_yield FROM main.gathering_nodes"
                    )
                }
                if "gathering_nodes" in economy_tables else None
            )
            pouch_before = (
                {
                    (r["actor"], r["material"]): r["quantity"]
                    for r in db.execute(
                        "SELECT actor,material,quantity FROM main.material_pouches"
                    )
                }
                if "material_pouches" in economy_tables else None
            )
            expected_nodes = dict(node_before) if node_before is not None else None
            expected_pouches = dict(pouch_before) if pouch_before is not None else None

            def node_state():
                return {
                    (r["room_id"], r["material"]): (r["remaining"], r["batch_yield"])
                    for r in db.execute(
                        "SELECT room_id,material,remaining,batch_yield "
                        "FROM main.gathering_nodes"
                    )
                } if expected_nodes is not None else None

            def pouch_state():
                return {
                    (r["actor"], r["material"]): r["quantity"]
                    for r in db.execute(
                        "SELECT actor,material,quantity FROM main.material_pouches"
                    )
                } if expected_pouches is not None else None

            # New gathers are authorized only while the selected trusted
            # handler runs. An installed trigger on the *later* scheduler
            # receipt or UTC watermark must not add a second forged gather
            # (including a negative-ID row) and inherit a legacy-kind bypass.
            def gather_state():
                return {
                    r["id"]: tuple(r) for r in db.execute(
                        "SELECT * FROM main.events WHERE kind='gather'"
                    )
                } if event_ledger_before is not None else None

            def new_nongather_state():
                # New unrelated say/audit history is allowed only at the
                # selected trusted-handler boundary, never from the later
                # scheduler receipt or final clock watermark.
                return {
                    r["id"]: tuple(r) for r in db.execute(
                        "SELECT * FROM main.events WHERE kind!='gather'"
                    ) if r["id"] not in event_ledger_before
                } if event_ledger_before is not None else None

            def new_clock_schedules():
                # A callback may schedule future pending work; the clock's own
                # receipt cannot silently enqueue ungranted future actions.
                return {
                    r["event_id"]: tuple(r) for r in db.execute(
                        "SELECT event_id,due_utc,kind,payload_json,processed_utc "
                        "FROM main.clock_events"
                    ) if r["event_id"] not in before_events
                }

            expected_gathers = gather_state()
            gather_events = {}
            # Optional host-owned components may bind exact transaction-start
            # evidence before any selected callback mutates this world.
            prepare = getattr(handler, "begin_host_tick", None)
            if prepare is not None:
                if not callable(prepare):
                    raise TypeError("Host tick preparation must be callable")
                prepare(db, tuple(_event(row) for row in rows))
            expected_world_state = _nonclock_world_state(db)
            initial_world_schema = expected_world_state[0]
            expected_new_nongather = new_nongather_state()
            expected_new_schedules = new_clock_schedules()
            expected_autoincrement = _autoincrement_state(db)
            unexpected_allocator_drift = False
            for row in rows:
                event = _event(row)
                # A previous scheduler receipt trigger must not contaminate
                # what a later valid handler adopts as its starting inventory.
                if (node_state() != expected_nodes or pouch_state() != expected_pouches
                        or gather_state() != expected_gathers):
                    raise ValueError("Roster inventory drifted before next UTC action")
                if _nonclock_world_state(db) != expected_world_state:
                    raise ValueError("Host UTC receipt altered ungranted world state")
                if (new_nongather_state() != expected_new_nongather
                        or new_clock_schedules() != expected_new_schedules):
                    raise ValueError("UTC receipt added ungranted events or schedules")
                if event.kind in ("npc_roster_gather", "host_action",
                                  "npc_economic_gather", "trusted_game_action"):
                    prior_event_ids = {
                        r["id"] for r in db.execute("SELECT id FROM main.events")
                    }
                outcome = handler(db, event)
                ship_custody.after_handler(db, event)
                # The trusted callback owns its authorized state transition.
                # Freeze the full main-world nonclock result at this boundary,
                # before our scheduler receipt can fire privileged triggers.
                expected_world_state = _nonclock_world_state(db)
                if expected_world_state[0] != initial_world_schema:
                    raise ValueError("Host UTC handler changed main-world schema")
                expected_new_nongather = new_nongather_state()
                expected_new_schedules = new_clock_schedules()
                expected_autoincrement = _autoincrement_state(db)
                if event.kind == "npc_roster_gather":
                    initial_proof = _verify_roster_utc_ack(db, event, outcome)
                    roster_proofs.append((event, outcome, initial_proof))
                    new_gathers = [
                        r for r in db.execute("SELECT * FROM main.events")
                        if r["id"] not in prior_event_ids and r["kind"] == "gather"
                    ]
                    actions = tuple(
                        a for a in outcome.proposal.assignments if a.site is not None
                    )
                    if len(new_gathers) != len(actions):
                        raise ValueError("Roster action lacks exact gather history")
                    pending_gathers = list(new_gathers)
                    for assignment in actions:
                        quantity = assignment.expected_units
                        site = assignment.site
                        key = (site.room_id, site.material)
                        stock, batch_yield = expected_nodes[key]
                        expected_nodes[key] = (stock - quantity, batch_yield)
                        key = (assignment.actor, site.material)
                        expected_pouches[key] = expected_pouches.get(key, 0) + quantity
                        matching = [
                            r for r in pending_gathers
                            if r["actor"] == assignment.actor
                            and r["room_id"] == site.room_id
                            and json.loads(r["payload_json"]) == {
                                "material": site.material, "amount": quantity
                            }
                        ]
                        if len(matching) != 1:
                            raise ValueError("Roster action gather history mismatches grant")
                        gathered = matching[0]
                        gather_events[gathered["id"]] = tuple(gathered)
                        pending_gathers.remove(gathered)
                    expected_gathers.update(gather_events)
                    if node_state() != expected_nodes or pouch_state() != expected_pouches:
                        raise ValueError("Roster grant includes ungranted inventory effects")
                elif event.kind in ("host_action", "npc_economic_gather",
                                    "trusted_game_action"):
                    # These legacy writers have their own host-side action
                    # validators. Bind their observed post-handler inventory
                    # before scheduler receipt DML can fire a hostile trigger.
                    # Receipt presence is necessary, NOT proof of exact effect
                    # semantics; typed per-action attestation remains pending.
                    new_nodes = node_state()
                    new_pouches = pouch_state()
                    if ((new_nodes != expected_nodes or new_pouches != expected_pouches)
                            and db.execute(
                                "SELECT 1 FROM main.action_receipts WHERE action_id=?",
                                (event.event_id,),
                            ).fetchone() is None):
                        raise ValueError("Legacy host economy change lacks action receipt")
                    expected_nodes, expected_pouches = new_nodes, new_pouches
                    # This is a post-handler *observation*, not a semantic
                    # authorization of its in-handler economic effects. The
                    # typed legacy action adapter owns that distinct proof.
                    for gathered in db.execute("SELECT * FROM main.events WHERE kind='gather'"):
                        if gathered["id"] not in prior_event_ids:
                            gather_events[gathered["id"]] = tuple(gathered)
                    expected_gathers.update(gather_events)
                if gather_state() != expected_gathers:
                    raise ValueError("Roster gather history changed within ungranted UTC action")
                updated = db.execute(
                    "UPDATE main.clock_events SET processed_utc=? "
                    "WHERE event_id=? AND processed_utc IS NULL",
                    (stamp, event.event_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("A clock receipt changed during host transaction")
                if _nonclock_world_state(db) != expected_world_state:
                    raise ValueError("Clock receipt introduced ungranted world effects")
                if (new_nongather_state() != expected_new_nongather
                        or new_clock_schedules() != expected_new_schedules):
                    raise ValueError("Clock receipt inserted ungranted event or future action")
                # Retain this alarm until the *existing* signed gather and
                # ship-history verifiers have produced their specific errors.
                # A later handler must never erase evidence of receipt drift.
                unexpected_allocator_drift |= (
                    _autoincrement_state(db) != expected_autoincrement)
                processed.append(event)
            db.execute(
                "INSERT INTO main.clock_state(id,last_utc) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET last_utc=excluded.last_utc",
                (stamp,),
            )
            if _nonclock_world_state(db) != expected_world_state:
                raise ValueError("UTC watermark introduced ungranted world effects")
            if (new_nongather_state() != expected_new_nongather
                    or new_clock_schedules() != expected_new_schedules):
                raise ValueError("UTC watermark inserted ungranted event or future action")
            unexpected_allocator_drift |= (
                _autoincrement_state(db) != expected_autoincrement)
            # Handler-created TEMP triggers can alter the durable receipts or
            # clock watermark after UPDATE rowcount checks succeeded. Verify
            # the final main-schema state, including earlier batch members,
            # while still inside the same rollback-capable transaction.
            state_after = db.execute(
                "SELECT last_utc FROM main.clock_state WHERE id=1"
            ).fetchone()
            if state_after is None or state_after["last_utc"] != stamp:
                raise RuntimeError("Clock state postcondition failed during host transaction")
            expected_receipts = {event.event_id: stamp for event in processed}
            unseen = set(before_events)
            for row in db.execute(
                "SELECT event_id,due_utc,kind,payload_json,processed_utc "
                "FROM main.clock_events"
            ):
                event_id = row["event_id"]
                if event_id in before_events:
                    unseen.remove(event_id)
                    before = before_events[event_id]
                    expected = (
                        before[0], before[1], before[2],
                        expected_receipts.get(event_id, before[3]),
                    )
                    actual = (
                        row["due_utc"], row["kind"], row["payload_json"],
                        row["processed_utc"],
                    )
                    if actual != expected:
                        raise RuntimeError("Clock event receipt/metadata postcondition failed during host transaction")
                elif row["processed_utc"] is not None:
                    # A trusted host can enqueue a new pending future event
                    # in its transaction, but cannot forge its completion.
                    raise RuntimeError("Clock new event postcondition failed during host transaction")
            if unseen:
                raise RuntimeError("Clock event deletion postcondition failed during host transaction")
            # Clock receipts, state updates and later events may fire MAIN triggers
            # that invalidate a roster verified earlier in this tick. Run the
            # final read-only postcondition after *all* transaction DML.
            if roster_proofs:
                final_state = db.execute(
                    "SELECT last_utc FROM main.clock_state WHERE id=1"
                ).fetchone()
                if final_state is None or final_state["last_utc"] != stamp:
                    raise ValueError("Roster tick lost authoritative UTC clock state")
            for event, outcome, original_proof in roster_proofs:
                if _verify_roster_utc_ack(db, event, outcome) != original_proof:
                    raise ValueError("Roster attestation changed after initial verification")
                main_event = db.execute(
                    "SELECT due_utc,kind,payload_json,processed_utc "
                    "FROM main.clock_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if (main_event is None or main_event["processed_utc"] != stamp
                        or main_event["due_utc"] != _stamp(event.due_at)
                        or main_event["kind"] != event.kind
                        or main_event["payload_json"] != _payload(event.payload)):
                    raise ValueError("Roster UTC completion changed before commit")
            # Both full-ledger custody checks are read-only and run after all
            # selected callbacks, scheduler receipts, watermark writes, and
            # roster postconditions. Neither may observe a later tick write.
            finalizer = getattr(handler, "verify_final_precommit", None)
            if finalizer is not None:
                if not callable(finalizer):
                    raise TypeError("Host tick finalizer must be callable")
                finalizer(db, tuple(processed))
            ship_custody.before_commit(db)
            if _nonclock_world_state(db) != expected_world_state:
                raise ValueError("Finalizer introduced ungranted world effects")
            if (new_nongather_state() != expected_new_nongather
                    or new_clock_schedules() != expected_new_schedules):
                raise ValueError("Finalizer inserted ungranted event or future action")
            # LAST read-only check: protects economy state even if an earlier
            # finalizer is implemented incorrectly and performs late DML.
            final_economy_tables = {
                r["name"] for r in db.execute(
                    "SELECT name FROM main.sqlite_master WHERE type='table' "
                    "AND name IN ('events','gathering_nodes','material_pouches')"
                )
            }
            if final_economy_tables != economy_tables:
                raise ValueError("Host UTC batch changed economy storage schema")
            if node_state() != expected_nodes:
                raise ValueError("Roster resource inventory changed outside authorized host actions")
            if pouch_state() != expected_pouches:
                raise ValueError("Roster pouch inventory changed outside authorized host actions")
            if event_ledger_before is not None:
                # Every old event retains its exact columns, including former
                # say/join rows. New non-gathers (including say/audit) are
                # permitted; a new gather must match a signed selected grant.
                final_event_rows = list(db.execute("SELECT * FROM main.events"))
                final_ledger = {r["id"]: tuple(r) for r in final_event_rows}
                if any(final_ledger.get(event_id) != before
                       for event_id, before in event_ledger_before.items()):
                    raise ValueError("Roster tick altered historical event ledger")
                if any(final_ledger.get(event_id) != signed
                       for event_id, signed in gather_events.items()):
                    raise ValueError("Roster tick altered signed gather history")
                if gather_state() != expected_gathers:
                    raise ValueError("Roster gather history changed after host action")
                if any(r["kind"] == "gather"
                       and r["id"] not in event_ledger_before
                       and r["id"] not in gather_events
                       for r in final_event_rows):
                    raise ValueError("Roster gather history contains ungranted effects")
            if (unexpected_allocator_drift
                    or _autoincrement_state(db) != expected_autoincrement):
                raise ValueError("UTC receipt changed durable AUTOINCREMENT allocation")
            return processed
