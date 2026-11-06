"""Host-issued, event-grounded observation memory for one REALM database.

This is an opt-in local host adapter, not a public endpoint, model tool, or
general privacy retrofit for Engine.history/Gateway.feed. It intentionally
cannot infer entitlement to an old event from a resident's current room.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from .engine import Engine


MAX_RECALL = 100
MAX_SUMMARY = 500
# Only events with bounded, known public payload shapes qualify. New event
# kinds, especially whispers/private dialogue, require explicit policy work.
_PUBLIC_SHAPES: dict[str, frozenset[str]] = {
    "join": frozenset(),
    "move": frozenset({"from", "direction"}),
    "leave": frozenset({"to", "direction"}),
    "say": frozenset({"text"}),
    "take": frozenset({"item"}),
    "drop": frozenset({"item"}),
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_hash(row: sqlite3.Row) -> str:
    record = [row[k] for k in ("id", "occurred_at", "actor", "room_id", "kind", "payload_json")]
    serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _public_description(event: sqlite3.Row) -> tuple[str, str]:
    """Return a source-grounded description and epistemic classification."""
    kind = event["kind"]
    if kind not in _PUBLIC_SHAPES:
        raise ValueError("Event kind has no authorized public-observation policy")
    try:
        payload = json.loads(event["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Event payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Event payload has an unrecognized or private shape")
    # The separately reviewed NPC registration prototype labels an ordinary
    # join with one boolean marker. Do not generalize this into arbitrary
    # metadata, which could smuggle private facts through a public event.
    if kind == "join" and set(payload) == {"resident"} and type(payload["resident"]) is bool and payload["resident"]:
        checked_values = ()
    elif set(payload) == _PUBLIC_SHAPES[kind]:
        checked_values = payload.values()
    else:
        raise ValueError("Event payload has an unrecognized or private shape")
    for value in checked_values:
        if not isinstance(value, str) or not 1 <= len(value) <= 280 or not value.isprintable():
            raise ValueError("Event payload contains unsupported content")
    actor = event["actor"]
    if kind == "say":
        return f"{actor} said: {payload['text']}", "utterance_only_not_claim_truth"
    if kind == "join":
        return f"{actor} entered {event['room_id']}.", "committed_event"
    if kind == "move":
        return f"{actor} arrived in {event['room_id']}.", "committed_event"
    if kind == "leave":
        return f"{actor} departed {event['room_id']}.", "committed_event"
    verb = "took" if kind == "take" else "dropped"
    return f"{actor} {verb} {payload['item']}.", "committed_event"


class ActorMemory:
    """Per-world durable observation grants and *unverified* derived notes.

    Only trusted host code may call ``grant_latest_event`` or ``add_summary``.
    No model output can select a world, actor, event grant or revocation through
    this class's retrieval surface. Host policy must additionally establish
    sensory access, live presence and consent before issuing a grant.
    """

    def __init__(self, engine: Engine, *, world_id: str):
        if not isinstance(engine, Engine):
            raise TypeError("ActorMemory requires a REALM Engine")
        if not isinstance(world_id, str) or not world_id:
            raise ValueError("A durable world_id is required")
        self._engine = engine
        self.world_id = world_id
        with closing(engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_world(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS actor_memory_grants (
                    world_id TEXT NOT NULL,
                    observer TEXT NOT NULL COLLATE NOCASE REFERENCES players(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    source_room TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    revoked_at TEXT,
                    PRIMARY KEY (world_id, observer, event_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS actor_memory_summaries (
                    world_id TEXT NOT NULL,
                    observer TEXT NOT NULL COLLATE NOCASE,
                    event_id INTEGER NOT NULL,
                    text_unverified TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (world_id, observer, event_id),
                    FOREIGN KEY (world_id, observer, event_id)
                        REFERENCES actor_memory_grants(world_id, observer, event_id)
                        ON DELETE CASCADE
                )"""
            )

    def _assert_world(self, db: sqlite3.Connection) -> None:
        bound = db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()
        if bound is None or bound[0] != self.world_id:
            raise ValueError("Actor memory requires this database's exact bound world_id")

    @staticmethod
    def _actor(db: sqlite3.Connection, actor: str) -> str:
        if not isinstance(actor, str):
            raise ValueError("Observer must be a joined actor")
        row = db.execute("SELECT id FROM players WHERE id=?", (actor,)).fetchone()
        if row is None:
            raise ValueError("Observer must be a joined actor")
        return row["id"]

    @staticmethod
    def _event_id(event_id: int) -> int:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
            raise ValueError("Event ID must be a positive integer")
        return event_id

    @staticmethod
    def _source(db: sqlite3.Connection, event_id: int) -> sqlite3.Row:
        event = db.execute(
            "SELECT id,actor,room_id,kind,payload_json,occurred_at FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        if event is None:
            raise ValueError("Source event does not exist in the committed ledger")
        return event

    def grant_latest_event(self, observer: str, event_id: int) -> dict:
        """Host grants immediate, same-room observation of a committed event.

        A room history query cannot authorize this method: only the ledger
        head, or the preceding `leave` half of the head's atomic `leave/move`
        pair, can receive a fresh grant. The host MUST call this after commit
        and before another action, with any additional consent/presence/sensory
        policy already satisfied.
        """
        event_id = self._event_id(event_id)
        with closing(self._engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_world(db)
            actor = self._actor(db, observer)
            event = self._source(db, event_id)
            _public_description(event)  # deny unknown/private event payloads
            exists = db.execute(
                "SELECT revoked_at,source_hash FROM actor_memory_grants "
                "WHERE world_id=? AND observer=? AND event_id=?",
                (self.world_id, actor, event_id),
            ).fetchone()
            if exists is not None:
                if exists["revoked_at"] is not None:
                    raise ValueError("Revoked observation cannot be regranted")
                if exists["source_hash"] != _event_hash(event):
                    raise ValueError("Source event changed after observation grant")
                # Retrying a *previously granted* receipt is harmless even
                # after newer events: no retrospective entitlement is created.
                return {"world_id": self.world_id, "observer": actor, "event_id": event_id, "unchanged": True}
            latest = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            if latest != event_id:
                # Engine movement commits two ordered events in ONE action
                # transaction: leave(old room), move(destination). An old-room
                # witness needs the departure event, too. Accept only the
                # immediate predecessor of the current head when it is proven
                # to be that canonical pair, never arbitrary earlier history.
                valid_pair = False
                if event_id == latest - 1 and event["kind"] == "leave":
                    arrival = self._source(db, latest)
                    if arrival["kind"] == "move" and arrival["actor"] == event["actor"]:
                        try:
                            departure_payload = json.loads(event["payload_json"])
                            arrival_payload = json.loads(arrival["payload_json"])
                            _public_description(arrival)
                            valid_pair = (
                                departure_payload["to"] == arrival["room_id"]
                                and arrival_payload["from"] == event["room_id"]
                                and departure_payload["direction"] == arrival_payload["direction"]
                            )
                        except (ValueError, KeyError, TypeError):
                            pass
                if not valid_pair:
                    raise ValueError("No retrospective grants: source is not the latest committed action")
            # The latest JOIN or MOVE event before this source is the actor's
            # persisted event-time room. A later arrival cannot witness older
            # events; persistent position is not proof of online presence.
            location = db.execute(
                "SELECT room_id FROM events WHERE actor=? COLLATE NOCASE "
                "AND kind IN ('join','move') AND id<=? ORDER BY id DESC LIMIT 1",
                (actor, event_id),
            ).fetchone()
            if location is None or location[0] != event["room_id"]:
                raise ValueError("Observer was not in the event room at event time")
            db.execute(
                "INSERT INTO actor_memory_grants "
                "(world_id,observer,event_id,source_room,source_hash,granted_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.world_id, actor, event_id, event["room_id"], _event_hash(event), _utc()),
            )
            return {"world_id": self.world_id, "observer": actor, "event_id": event_id, "unchanged": False}

    def add_summary(self, observer: str, event_id: int, text_unverified: str) -> None:
        """Host stores one untrusted derived note, never an observation grant."""
        event_id = self._event_id(event_id)
        if (not isinstance(text_unverified, str) or not 1 <= len(text_unverified) <= MAX_SUMMARY
                or text_unverified != text_unverified.strip()
                or not text_unverified.isprintable()):
            raise ValueError("Derived note must be bounded printable single-line text")
        with closing(self._engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_world(db)
            actor = self._actor(db, observer)
            active = db.execute(
                "SELECT 1 FROM actor_memory_grants WHERE world_id=? AND observer=? "
                "AND event_id=? AND revoked_at IS NULL",
                (self.world_id, actor, event_id),
            ).fetchone()
            if active is None:
                raise ValueError("No active host-issued observation grant")
            db.execute(
                "INSERT INTO actor_memory_summaries "
                "(world_id,observer,event_id,text_unverified,created_at) VALUES(?,?,?,?,?)",
                (self.world_id, actor, event_id, text_unverified, _utc()),
            )

    def revoke(self, observer: str, event_id: int) -> bool:
        """Host revocation hides the source and any summary from future recall."""
        event_id = self._event_id(event_id)
        with closing(self._engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_world(db)
            actor = self._actor(db, observer)
            changed = db.execute(
                "UPDATE actor_memory_grants SET revoked_at=? WHERE world_id=? "
                "AND observer=? AND event_id=? AND revoked_at IS NULL",
                (_utc(), self.world_id, actor, event_id),
            )
            return changed.rowcount == 1

    def recall(self, observer: str, *, limit: int = 20) -> list[dict]:
        """Retrieve recent still-granted source evidence, never bare room history."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECALL:
            raise ValueError(f"Recall limit must be 1–{MAX_RECALL}")
        with closing(self._engine._connect()) as db:
            self._assert_world(db)
            actor = self._actor(db, observer)
            rows = db.execute(
                "SELECT e.id,e.actor,e.room_id,e.kind,e.payload_json,e.occurred_at,"
                "g.source_room,g.source_hash,s.text_unverified "
                "FROM actor_memory_grants g JOIN events e ON e.id=g.event_id "
                "LEFT JOIN actor_memory_summaries s ON s.world_id=g.world_id "
                "AND s.observer=g.observer AND s.event_id=g.event_id "
                "WHERE g.world_id=? AND g.observer=? AND g.revoked_at IS NULL "
                "ORDER BY e.id DESC LIMIT ?",
                (self.world_id, actor, limit),
            ).fetchall()
            memories = []
            for row in rows:
                if row["room_id"] != row["source_room"] or _event_hash(row) != row["source_hash"]:
                    continue  # fail closed on source-event mutation
                try:
                    description, epistemic_scope = _public_description(row)
                except ValueError:
                    continue
                memories.append({
                    "world_id": self.world_id,
                    "observer": actor,
                    "source_event_id": row["id"],
                    "room_id": row["room_id"],
                    "kind": row["kind"],
                    "source_text": description,
                    "evidence_scope": epistemic_scope,
                    "derived_summary_unverified": row["text_unverified"],
                })
            return memories
