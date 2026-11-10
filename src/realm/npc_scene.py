"""A host-owned, room-entry-scoped scene for optional REALM NPC proposals.

The model does not select the resident or receive global history, credentials,
player inventories, database paths, or events predating the resident's current
provable arrival. The host chooses an already joined ResidentController.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field

from .engine import Engine
from .residents import ResidentController


_MAX_SPEECH_EVENTS = 20
_DEFAULT_SPEECH_EVENTS = 8
_MAX_VIEW_CHARS = 6000


@dataclass(frozen=True, slots=True)
class ResidentScene:
    """One consistent SQLite observation, not an authorization receipt.

    The private engine reference is compared by object identity before use;
    Python host code can deliberately forge this value and is not sandboxed.
    The value's `view` is untrusted model *context*, never a world command.
    """

    actor: str
    room_id: str
    entry_id: int
    view: str
    _engine: Engine = field(repr=False, compare=False)


class ResidentSceneBuilder:
    """Derive a bounded room-local model view from one trusted Engine."""

    def __init__(self, engine: Engine, *, speech_events: int = _DEFAULT_SPEECH_EVENTS) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("Scene builder requires a world Engine")
        if type(speech_events) is not int or not 1 <= speech_events <= _MAX_SPEECH_EVENTS:
            raise ValueError("Scene speech limit must be 1–20")
        self.engine = engine
        self.speech_events = speech_events

    def _actor(self, db: sqlite3.Connection, controller: ResidentController) -> tuple[str, str]:
        if not isinstance(controller, ResidentController) or controller._engine is not self.engine:
            raise ValueError("Scene controller must belong to the same Engine")
        row = db.execute(
            "SELECT id,room_id FROM players WHERE id=? COLLATE NOCASE",
            (controller._resident,),
        ).fetchone()
        if row is None:
            raise ValueError("Host must join resident before scene construction")
        return row["id"], row["room_id"]

    def build(self, controller: ResidentController) -> ResidentScene:
        """Read only room state and post-arrival speech in one SQLite snapshot."""
        with closing(self.engine._connect()) as db:
            db.execute("BEGIN")
            actor, room_id = self._actor(db, controller)
            # A missing/tampered latest move or an intervening leave must
            # never cause fallback to an earlier arrival.
            entry_id = self.engine._room_entry_floor(db, actor, room_id)
            room = db.execute(
                "SELECT name,description FROM rooms WHERE id=?", (room_id,)
            ).fetchone()
            if room is None:
                raise ValueError("Resident room no longer exists")
            paths = [row[0] for row in db.execute(
                "SELECT direction FROM exits WHERE room_id=? ORDER BY direction", (room_id,)
            )]
            items = [row[0] for row in db.execute(
                "SELECT name FROM items WHERE room_id=? ORDER BY id", (room_id,)
            )]
            latest = db.execute(
                "SELECT actor,payload_json FROM events "
                "WHERE room_id=? AND id>=? AND kind='say' "
                "ORDER BY id DESC LIMIT ?",
                (room_id, entry_id, self.speech_events),
            ).fetchall()
            speech: list[dict[str, str]] = []
            for row in reversed(latest):
                try:
                    detail = json.loads(row["payload_json"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ValueError("Invalid speech ledger payload") from exc
                if not isinstance(detail, dict) or set(detail) != {"text"}:
                    raise ValueError("Invalid speech ledger payload")
                value = detail["text"]
                if (not isinstance(value, str) or not 1 <= len(value) <= 280
                        or not all(char.isprintable() for char in value)):
                    raise ValueError("Unsafe speech ledger payload")
                speech.append({"speaker": row["actor"], "text": value})
            view = json.dumps({
                "scope": "current room after this resident's provable arrival",
                "resident": actor,
                "room": {
                    "name": room["name"],
                    "description": room["description"],
                    "exits": paths,
                    "items": items,
                },
                "recent_speech": speech,
            }, ensure_ascii=True, separators=(",", ":"))
            if not 1 <= len(view) <= _MAX_VIEW_CHARS:
                raise ValueError("Authorized NPC scene exceeds model view budget")
            if not all(c.isprintable() for c in view):
                raise ValueError("Authorized NPC scene contains unsafe text")
            return ResidentScene(actor, room_id, entry_id, view, self.engine)

    def assert_current_in_connection(
        self, scene: ResidentScene, db: sqlite3.Connection,
    ) -> None:
        """Fail closed inside the Engine's existing writer transaction.

        The host passes this to ResidentController.commit(writer_guard=...),
        which Engine.act executes under its BEGIN IMMEDIATE before any
        receipt replay or world effect. No second transaction is opened.
        """
        if not isinstance(scene, ResidentScene) or scene._engine is not self.engine:
            raise ValueError("Stale NPC scene world mismatch")
        row = db.execute(
            "SELECT id,room_id FROM players WHERE id=? COLLATE NOCASE",
            (scene.actor,),
        ).fetchone()
        if row is None or row["id"] != scene.actor or row["room_id"] != scene.room_id:
            raise ValueError("Stale NPC scene after resident movement")
        try:
            floor = self.engine._room_entry_floor(db, scene.actor, scene.room_id)
        except ValueError as exc:
            raise ValueError("Stale NPC scene has no current-room arrival") from exc
        if floor != scene.entry_id:
            raise ValueError("Stale NPC scene after resident reentry")

    def assert_current(self, scene: ResidentScene) -> None:
        """Cheap pre-inference check; commit-time writer guard is authoritative."""
        with closing(self.engine._connect()) as db:
            db.execute("BEGIN")
            self.assert_current_in_connection(scene, db)
