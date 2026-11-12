"""Loopback-only, player-bound bearer HTTP bridge for the REALM prototype.

The v1 wire format is provisional. This is NOT a public multiplayer/auth
service, not an HTTPS endpoint, and not a replacement for Telnet/GMCP support.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import errno
import json
import math
import os
import re
import socket
import sqlite3
from datetime import datetime, timezone
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .auth import CredentialStore
from .engine import Engine, _SEED_EXITS, _SEED_ITEMS, _SEED_ROOMS, _pristine_schema, _schema_snapshot
from .geography import Geography, MapUnavailable, _check_id
from .local_presence import LocalPresenceWitness, PresenceDenied, PresenceLease
from .presence_host_binding import bind_host_engine, unbind_host_engine
from .router import CommandRouter
from .scoped_credentials import CredentialStateError, ScopedCredentialStore
from .principal_roles import is_reserved_npc


MAX_BODY_BYTES = 4096
MAX_FEED_EVENTS = 100
MAX_SCENE_ENTRIES = 50
DEFAULT_RATE_REQUESTS_PER_MINUTE = 120
DEFAULT_RATE_BURST = 60
DEFAULT_RATE_MAX_PRINCIPALS = 256
DEFAULT_MAX_ACTIVE_REQUESTS = 32
DEFAULT_SOCKET_IDLE_TIMEOUT_SECONDS = 15.0
DEFAULT_BODY_DEADLINE_SECONDS = 10.0
DEFAULT_HEADER_DEADLINE_SECONDS = 10.0
_WORLD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_GEOGRAPHY_COLUMNS = {
    "geo_places": frozenset(("id", "name", "kind", "parent_id", "x", "y")),
    "geo_maps": frozenset(("space_id", "width", "height", "rows_json")),
    "geo_links": frozenset(("origin", "destination", "mode", "seconds")),
    "geo_ships": frozenset(("id", "name", "owner", "planet_id", "destination", "arrival_at")),
    "geo_ship_events": frozenset(("id", "occurred_at", "ship_id", "actor", "kind", "payload_json")),
    "geo_ship_receipts": frozenset(("ship_id", "actor", "action_id", "command", "response_json")),
}
_GEOGRAPHY_INDEXES = {"idx_geo_parent": ("parent_id",)}
_SHIP_CLOCK_COLUMNS = {
    "clock_events": frozenset(("event_id", "due_utc", "kind", "payload_json", "processed_utc")),
    "clock_state": frozenset(("id", "last_utc")),
    "npc_roster_utc_attestations": frozenset((
        "event_id", "world_id", "policy_id", "due_utc", "outcome_sha256",
        "grant_mac_hex", "execution_mac_hex",
    )),
}
_SHIP_CLOCK_INDEXES = {"idx_clock_due": ("due_utc", "event_id")}


class ShipAccessDenied(PermissionError):
    """An authenticated pilot has no owner entitlement to this ship."""


class Gateway:
    """Boundary between untrusted HTTP requests and deterministic engine actions.

    Remote player access requires a host-provisioned credential bound to a
    specific name. Local Engine methods are still host-trusted administrative
    APIs; this is not public account registration or session management.
    """

    def __init__(
        self, db_path: str | Path, *, world_id: str = "demo",
        adopt_unbound_world: bool = False,
        scoped_credentials: bool = False,
        scoped_guard_path: str | Path | None = None,
    ):
        if not isinstance(world_id, str) or not _WORLD_ID.fullmatch(world_id):
            raise ValueError("world_id must start with a letter and contain 1–64 letters, digits, '_' or '-'")
        self.engine = Engine(db_path)
        self._bind_world_id(world_id, adopt_unbound_world=adopt_unbound_world)
        # Geography is an optional world-authoring/map subsystem. Do not
        # install its ship tables in an ordinary fantasy/medieval Gateway (or
        # invalidate a still-pristine database before worldpack import).
        self._geography: Geography | None = None
        self._geography_lock = threading.Lock()
        # CommandRouter constructs the optional Crafting schema. Ordinary
        # Gateway startup and credential provisioning must not silently opt
        # a pristine world into crafting before worldpack selection either.
        self._router: CommandRouter | None = None
        self._router_lock = threading.Lock()
        self.credentials = CredentialStore(self.engine)
        self.scoped_credentials = (
            ScopedCredentialStore(self.engine, world_id, guard_path=scoped_guard_path)
            if scoped_credentials else None
        )
        self.world_id = world_id

    def authenticate_remote(self, token: str, *, required_capability: str) -> str | None:
        if self.scoped_credentials is None:
            return self.credentials.authenticate(token)
        try:
            return self.scoped_credentials.authenticate(
                token, required_capability=required_capability
            )
        except (CredentialStateError, ValueError):
            # A missing witness or clock rollback must fail closed, not expose a traceback.
            # A changed bound world ID is likewise a denied credential, not a
            # connection-aborting exception before the HTTP authorization gate.
            return None

    def _scoped_writer_guard(
        self, principal: str, token: str, capability: str
    ) -> Callable[[sqlite3.Connection], None]:
        """Revalidate under the action writer lock, on the actual world DB."""
        if self.scoped_credentials is None:
            raise PermissionError("Scoped world credential required")

        def check(db: sqlite3.Connection) -> None:
            try:
                actor = self.scoped_credentials.authenticate_in_writer(
                    db, token, required_capability=capability
                )
            except CredentialStateError as exc:
                raise PermissionError("Active scoped bearer required") from exc
            if actor is None or actor.casefold() != principal.casefold():
                raise PermissionError("Active scoped bearer required")

        return check

    def _legacy_writer_guard(
        self, principal: str, token: str,
    ) -> Callable[[sqlite3.Connection], None]:
        """Recheck a player-bound legacy bearer inside the world writer.

        The earlier HTTP authentication read is not durable permission to
        post worldwide or group chat after the credential is revoked.
        """
        if (not isinstance(token, str) or not 16 <= len(token) <= 255
                or not token.isascii()):
            raise PermissionError("Active player bearer required")
        digest = CredentialStore._digest(token)

        def check(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT player_name FROM main.player_credentials WHERE token_digest=?",
                (digest,),
            ).fetchone()
            if (row is None or row[0].casefold() != principal.casefold()
                    or is_reserved_npc(db, row[0])):
                raise PermissionError("Active player bearer required")

        return check

    def join_legacy_remote(self, player: str, token: str) -> dict:
        return self.join(player, writer_guard=self._legacy_writer_guard(player, token))

    def command_legacy_remote(self, player: str, command: str,
                              action_id: str | None, token: str) -> dict:
        # Retain the canonical Gateway.command interception seam used by the
        # host shutdown custody tests and trusted local host extensions.
        return self.command(
            player, command, action_id,
            writer_guard=self._legacy_writer_guard(player, token),
        )

    def join_remote(self, player: str, token: str) -> dict:
        """HTTP-only scoped join: the final check and join share one writer."""
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        guard = self._scoped_writer_guard(player, token, "world:join")
        join_emissions: list[tuple[int, str]] = []
        text = self.engine.join(player, writer_guard=guard, emissions=join_emissions)
        if len(join_emissions) != 1:
            raise RuntimeError("Host did not attest the join entry")
        return {"ok": True, "text": text,
                **self._response_cursors(player, entry_hint=join_emissions[0][0])}

    def command_remote(self, player: str, command: str, action_id: str | None,
                       token: str) -> dict:
        """HTTP-only scoped command. Craft and core paths both check in-writer."""
        if not isinstance(player, str) or not isinstance(command, str):
            raise ValueError("Player and command must be strings")
        if len(command) > 512:
            raise ValueError("Command is too long")
        guard = self._scoped_writer_guard(player, token, "command:write")
        text = self.router.act(player, command, action_id=action_id, writer_guard=guard)
        return {"ok": True, "text": text, **self._response_cursors(player)}

    def ship_depart_remote(
        self, token: str, ship_id: str, destination: str, action_id: str,
    ) -> dict:
        """Use host UTC and recheck ship:depart inside the ship writer transaction."""
        if self.scoped_credentials is None:
            raise PermissionError("Scoped ship credentials are required")
        if (not isinstance(ship_id, str) or not isinstance(destination, str)
                or not isinstance(action_id, str)):
            raise ValueError("Ship ID, destination and stable action ID are required")
        # Instantiating AuthenticatedShipCommands dereferences gateway.geography
        # for host path binding. On an unconfigured medieval world that would
        # install ship schema *before* its disabled-clock denial. Preflight the
        # already-enabled host subsystems through a query-only connection.
        with closing(self._read()) as db:
            if not self._schema_shape_ready(db, _SHIP_CLOCK_COLUMNS, _SHIP_CLOCK_INDEXES):
                raise RuntimeError("Ship world clock is not enabled or its schema is incomplete")
            if not self._schema_shape_ready(db, _GEOGRAPHY_COLUMNS, _GEOGRAPHY_INDEXES):
                raise RuntimeError("Ship geography is not configured or its schema is incomplete")
        from .ship_command_auth import AuthenticatedShipCommands
        from .world_clock import WorldClock
        pilot = AuthenticatedShipCommands(self, WorldClock(self.engine.db_path))
        result = pilot.depart(
            token, ship_id, destination,
            now=int(datetime.now(timezone.utc).timestamp()),
            action_id=action_id, scoped=True,
        )
        return {"ok": True, **result}

    def ship_events_remote(self, token: str, ship_id: str, after: int) -> dict:
        """Read ONLY a scoped ship pilot's own durable departure/arrival log.

        Existing ship:depart plus recorded ship ownership is the entire
        entitlement. There is no delegated crew read scope or public fleet feed.
        This read-only endpoint never grants pilot or host authority.
        """
        if self.scoped_credentials is None:
            raise PermissionError("Scoped ship pilot credential required")
        principal = self.authenticate_remote(token, required_capability="ship:depart")
        if principal is None:
            raise PermissionError("Active scoped ship pilot credential required")
        _check_id(ship_id, "Ship ID")
        if type(after) is not int or not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid ship history cursor")
        with closing(self._read()) as db:
            db.execute("BEGIN")
            world = db.execute(
                "SELECT value FROM main.world_meta WHERE key='world_id'"
            ).fetchone()
            if world is None or world[0] != self.world_id:
                raise PermissionError("Ship world identity is not current")
            joined = db.execute(
                "SELECT 1 FROM main.players WHERE id=? COLLATE NOCASE", (principal,)
            ).fetchone()
            # The same optional-schema boundary applies to this read. Missing
            # or partially installed geography means there is no owner ship
            # entitlement; never query absent tables or install them on GET.
            if joined is None or not self._schema_shape_ready(
                    db, _GEOGRAPHY_COLUMNS, _GEOGRAPHY_INDEXES):
                raise ShipAccessDenied("Ship is not owned by the authenticated pilot")
            ship = db.execute(
                "SELECT owner FROM main.geo_ships WHERE id=?", (ship_id,)
            ).fetchone()
            if ship is None or ship["owner"] != principal:
                # Unknown and other-owned ships have identical visibility.
                raise ShipAccessDenied("Ship is not owned by the authenticated pilot")
            head = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM main.geo_ship_events WHERE ship_id=?",
                (ship_id,),
            ).fetchone()[0]
            if after > head:
                raise ValueError("Cursor exceeds this ship's event head")
            rows = db.execute(
                "SELECT id,occurred_at,ship_id,actor,kind,payload_json "
                "FROM main.geo_ship_events WHERE ship_id=? AND id>? "
                "ORDER BY id LIMIT ?", (ship_id, after, MAX_FEED_EVENTS),
            ).fetchall()
            events = [
                {"id": row["id"], "at": row["occurred_at"],
                 "ship_id": row["ship_id"], "actor": row["actor"],
                 "kind": row["kind"], "payload": json.loads(row["payload_json"])}
                for row in rows
            ]
        return {"ok": True, "world_id": self.world_id, "ship_id": ship_id,
                "events": events, "cursor": events[-1]["id"] if events else after}

    def ship_owner_response_current(self, principal: str, ship_id: str) -> bool:
        """Fresh, read-only owner proof for an already-authorized HTTP result.

        The history query's earlier ownership snapshot is insufficient when
        ownership can transfer before its response leaves this process. The
        handler separately revalidates the credential before calling here.
        """
        with closing(self._read()) as db:
            db.execute("BEGIN")
            world = db.execute(
                "SELECT value FROM main.world_meta WHERE key='world_id'"
            ).fetchone()
            if world is None or world[0] != self.world_id:
                raise PermissionError("Ship world identity is not current")
            joined = db.execute(
                "SELECT 1 FROM main.players WHERE id=? COLLATE NOCASE", (principal,)
            ).fetchone()
            if joined is None or not self._schema_shape_ready(
                    db, _GEOGRAPHY_COLUMNS, _GEOGRAPHY_INDEXES):
                return False
            owner = db.execute(
                "SELECT owner FROM main.geo_ships WHERE id=?", (ship_id,)
            ).fetchone()
            return owner is not None and owner["owner"] == principal

    @property
    def router(self) -> CommandRouter:
        """Install optional crafting dispatch on first command use."""
        if self._router is None:
            with self._router_lock:
                if self._router is None:
                    self._router = CommandRouter(self.engine)
        return self._router

    @property
    def geography(self) -> Geography:
        """Initialize optional geography on first explicit host/map use.

        A ThreadingHTTPServer may serve simultaneous first map reads. Keep
        the schema install single-owner per Gateway without changing the
        public gateway.geography API or creating any default ship content.
        """
        if self._geography is None:
            with self._geography_lock:
                if self._geography is None:
                    self._geography = Geography(self.engine.db_path)
        return self._geography

    def _bind_world_id(self, world_id: str, *, adopt_unbound_world: bool) -> None:
        """Persist the host's world identity before any gateway can serve it.

        This check is transactional across competing gateway processes. An old
        unbound database containing gameplay history cannot be silently
        relabeled; only an explicit local operator adoption may bind it.
        """
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            bound = db.execute(
                "SELECT value FROM world_meta WHERE key='world_id'"
            ).fetchone()
            if bound is not None:
                if bound[0] != world_id:
                    raise ValueError("world database is already bound to a different world_id")
                return
            if self._has_unbound_world_data(db) and not adopt_unbound_world:
                raise ValueError("unbound existing world database requires explicit local adoption")
            db.execute(
                "INSERT INTO world_meta (key,value) VALUES ('world_id',?)", (world_id,)
            )

    @staticmethod
    def _has_unbound_world_data(db: sqlite3.Connection) -> bool:
        """Detect played *or authored* state in a legacy unbound database.

        A pristine engine creates only generic seed rooms/exits/items. Even
        unplayed authored fiction, provisioned tokens or future plugin tables
        must require deliberate operator adoption instead of silent relabeling.
        """
        for table in ("players", "events", "action_receipts", "world_meta"):
            if db.execute(f"SELECT EXISTS(SELECT 1 FROM {table})").fetchone()[0]:
                return True
        seeded = (
            ("rooms", "id,name,description", _SEED_ROOMS),
            ("exits", "room_id,direction,destination", _SEED_EXITS),
            ("items", "id,name,description,room_id", _SEED_ITEMS),
        )
        for table, columns, expected in seeded:
            if sorted(tuple(row) for row in db.execute(f"SELECT {columns} FROM {table}")) != sorted(expected):
                return True
        # The complete stored schema must match the one generated from the
        # same core DDL, not merely known names. This rejects added columns,
        # constraints, rewritten built-in indexes, triggers and views.
        if _schema_snapshot(db) != _pristine_schema():
            return True
        # sqlite_sequence may keep evidence even after events were deleted.
        if db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_sequence)").fetchone()[0]:
            return True
        return False

    def _read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.engine.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _schema_shape_ready(db: sqlite3.Connection, columns: dict, indexes: dict) -> bool:
        """Bounded read-only optional subsystem shape check, not a schema migration.

        Names alone are insufficient: host DDL may leave a named but malformed
        table, which would otherwise close the HTTP socket on a missing column.
        These are runtime-required columns/indexes, not full SQLite constraint
        or privileged-host tamper attestation.
        """
        kinds = {row["name"]: row["type"] for row in db.execute(
            "SELECT name,type FROM main.sqlite_master WHERE type IN ('table','index')"
        )}
        for name, required in columns.items():
            if kinds.get(name) != "table":
                return False
            actual = {row["name"] for row in db.execute(f'PRAGMA main.table_info("{name}")')}
            if not required.issubset(actual):
                return False
        for name, required in indexes.items():
            if kinds.get(name) != "index":
                return False
            actual = tuple(row["name"] for row in db.execute(f'PRAGMA main.index_info("{name}")'))
            if actual != required:
                return False
        return True

    def cursor(self) -> int:
        db = self._read()
        try:
            return int(db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
        finally:
            db.close()

    def _room_entry_cursor(self, player: str) -> int | None:
        """Advisory player-room entry cursor; feed still enforces authorization.

        Never infer entry from the global head. Read the current player and
        host-recorded latest arrival inside one SQLite snapshot. A malformed
        legacy ledger yields no hint rather than inventing room knowledge.
        """
        with closing(self._read()) as db:
            db.execute("BEGIN")
            subject = db.execute(
                "SELECT id,room_id FROM main.players WHERE id=? COLLATE NOCASE",
                (player,),
            ).fetchone()
            if subject is None:
                return None
            try:
                return self.engine._room_entry_floor(
                    db, subject["id"], subject["room_id"]
                )
            except ValueError:
                return None

    def _response_cursors(self, player: str, *, entry_hint: int | None = None) -> dict:
        """Keep the caller's committed join entry distinct from later rejoins.

        A global head sampled before a competing, authenticated room entry
        may be smaller than its host-recorded entry ID. Return a head at
        least as recent as the entry cursor; feed still revalidates access.
        """
        head = self.cursor()
        entry = entry_hint if entry_hint is not None else self._room_entry_cursor(player)
        return {"cursor":max(head, entry if entry is not None else 0),
                "room_entry_cursor":entry}

    def channel_access_revision(self, player: str) -> int:
        """Current host channel permission watermark for response-time checks."""
        from .channel_roster import channel_access_revision
        with closing(self._read()) as db:
            return channel_access_revision(db, player)

    def join(self, player: str, *,
             writer_guard: Callable[[sqlite3.Connection], None] | None = None) -> dict:
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        # The underlying join() validates syntax and preserves the current world.
        join_emissions: list[tuple[int, str]] = []
        if writer_guard is None:
            text = self.engine.join(player, emissions=join_emissions)
        else:
            text = self.engine.join(player, writer_guard=writer_guard,
                                    emissions=join_emissions)
        if len(join_emissions) != 1:
            raise RuntimeError("Host did not attest the join entry")
        return {"ok": True, "text": text,
                **self._response_cursors(player, entry_hint=join_emissions[0][0])}

    def command(self, player: str, command: str, action_id: str | None, *,
                writer_guard: Callable[[sqlite3.Connection], None] | None = None) -> dict:
        if not isinstance(player, str) or not isinstance(command, str):
            raise ValueError("Player and command must be strings")
        if len(command) > 512:
            raise ValueError("Command is too long")
        if writer_guard is None:
            text = self.router.act(player, command, action_id=action_id)
        else:
            text = self.router.act(
                player, command, action_id=action_id, writer_guard=writer_guard,
            )
        return {"ok": True, "text": text, **self._response_cursors(player)}

    def room(self, player: str) -> dict:
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        return {"ok": True, "text": self.engine.act(player, "look"), "cursor": self.cursor()}

    def scene(self, player: str) -> dict:
        """Read current room facts; saved position does not imply online presence.

        An actor's existence alone cannot establish that they are an NPC or
        that an interaction action is available. The response is bounded and
        tied to one SQLite read snapshot, including its event cursor.
        """
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        with closing(self._read()) as db:
            db.execute("BEGIN")
            subject = db.execute("SELECT id,room_id FROM players WHERE id=?", (player,)).fetchone()
            if subject is None:
                raise ValueError("Join this REALM before reading the scene")
            room_id = subject["room_id"]
            room = db.execute("SELECT id,name,description FROM rooms WHERE id=?", (room_id,)).fetchone()
            if room is None:
                raise ValueError("The saved room does not exist")
            exits = db.execute(
                "SELECT direction FROM exits WHERE room_id=? ORDER BY direction LIMIT ?",
                (room_id, MAX_SCENE_ENTRIES + 1),
            ).fetchall()
            items = db.execute(
                "SELECT id,name FROM items WHERE room_id=? ORDER BY id LIMIT ?",
                (room_id, MAX_SCENE_ENTRIES + 1),
            ).fetchall()
            occupants = db.execute(
                "SELECT id FROM players WHERE room_id=? AND id<>? COLLATE NOCASE "
                "ORDER BY id COLLATE NOCASE LIMIT ?",
                (room_id, subject["id"], MAX_SCENE_ENTRIES + 1),
            ).fetchall()
            cursor = int(db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
            return {
                "ok": True,
                "room": {key: room[key] for key in ("id", "name", "description")},
                "exits": [r["direction"] for r in exits[:MAX_SCENE_ENTRIES]],
                "items": [{"id": r["id"], "name": r["name"]} for r in items[:MAX_SCENE_ENTRIES]],
                "occupants": [{"name": r["id"]} for r in occupants[:MAX_SCENE_ENTRIES]],
                "presence_semantics": "saved_position_only",
                "truncated": {
                    "exits": len(exits) > MAX_SCENE_ENTRIES,
                    "items": len(items) > MAX_SCENE_ENTRIES,
                    "occupants": len(occupants) > MAX_SCENE_ENTRIES,
                },
                "cursor": cursor,
            }

    def map(self, player: str) -> dict:
        """Map for the joined character's persisted room, if one is authored.

        The HTTP handler must resolve the player from their credential before
        invoking this host-trusted method. Fog-of-war authorization remains
        unimplemented, so this route is not ready for public hosting.
        """
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        db = self._read()
        try:
            row = db.execute("SELECT id,room_id FROM players WHERE id=?", (player,)).fetchone()
            if row is None:
                raise ValueError("Join this REALM before reading the map")
            room_id = row["room_id"]
            # GET is a read boundary. An ordinary medieval world must not
            # install the optional geo_* ship schema just because a player
            # asked for an unconfigured map. Require an already complete
            # explicitly installed geography schema before accessing the
            # lazily cached host geography object.
            if not self._schema_shape_ready(db, _GEOGRAPHY_COLUMNS, _GEOGRAPHY_INDEXES):
                raise MapUnavailable("No map is configured for this location")
        finally:
            db.close()
        return {"ok": True, **self.geography.snapshot_for_room(room_id)}

    def feed(self, player: str, after: int) -> dict:
        from .channel_roster import event_text, visible_channel_events
        if not isinstance(player, str) or not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid player or cursor")
        db = self._read()
        try:
            # One read transaction binds the player's current room, latest
            # provable arrival, and event rows to the same SQLite snapshot.
            # Otherwise a move between separate SELECTs could disclose
            # history from a room the player was never authorized to see.
            db.execute("BEGIN")
            subject = db.execute("SELECT id,room_id FROM players WHERE id=?", (player,)).fetchone()
            if subject is None:
                raise ValueError("Join this REALM before reading the feed")
            # Current-room membership alone does not grant retroactive access
            # to its prior conversation. Check the actor's latest *overall*
            # location event, including departure. Looking only for a join or
            # move into the current room could fall back to an older join if
            # the newest return-arrival row had been removed, disclosing speech
            # from the period when the actor was absent. An unprovable or
            # contradictory history fails closed instead of guessing.
            entry = db.execute(
                "SELECT id,room_id,kind FROM events WHERE actor=? COLLATE NOCASE "
                "AND kind IN ('join','leave','move') ORDER BY id DESC LIMIT 1",
                (subject["id"],),
            ).fetchone()
            if (entry is None or entry["kind"] not in ("join", "move") or
                    entry["room_id"] != subject["room_id"]):
                raise ValueError("No provable current-room arrival; feed denied")
            from .channel_roster import channel_access_revision
            channel_revision = channel_access_revision(db, subject["id"])
            records = db.execute(
                "SELECT id,occurred_at,actor,room_id,kind,payload_json FROM events "
                "WHERE room_id=? AND kind NOT IN "
                "('ooc','crewchat','pvp_death','chat_permission_epoch') "
                "AND id>? AND id>=? ORDER BY id LIMIT ?",
                (subject["room_id"], after, entry["id"], MAX_FEED_EVENTS),
            ).fetchall()
            visible = [(row["id"], row, None) for row in records]
            channels = visible_channel_events(
                db, subject["id"], after=after, limit=MAX_FEED_EVENTS,
            )
            visible.extend((row["id"], row, None) for row in channels)
            # An opt-in PvP death is announced globally using ONLY its
            # host-authored text. Never project the combat room or raw JSON.
            installed = db.execute(
                "SELECT 1 FROM main.sqlite_master "
                "WHERE type='table' AND name='combat_announcements'"
            ).fetchone() is not None
            if installed:
                # World entry, not latest room entry, bounds global news.
                first_join = db.execute(
                    "SELECT MIN(id) FROM events WHERE actor=? COLLATE NOCASE AND kind='join'",
                    (subject["id"],),
                ).fetchone()[0]
                if first_join is not None:
                    public = db.execute(
                        "SELECT events.id,events.occurred_at,events.actor,a.message "
                        "FROM combat_announcements AS a "
                        "JOIN events ON events.id=a.event_id AND events.kind='pvp_death' "
                        "WHERE events.id>? AND events.id>=? ORDER BY events.id LIMIT ?",
                        (after, first_join, MAX_FEED_EVENTS),
                    ).fetchall()
                    visible.extend((row["id"], row, row["message"]) for row in public)
            events = []
            for _, row, public_message in sorted(visible, key=lambda item: item[0])[:MAX_FEED_EVENTS]:
                if public_message is not None:
                    events.append({
                        "id": row["id"], "kind": "pvp_death", "actor": row["actor"],
                        "text": public_message, "room": "global", "at": row["occurred_at"],
                    })
                    continue
                payload = json.loads(row["payload_json"])
                kind = row["kind"]
                if kind in {"ooc", "crewchat"}:
                    content = event_text(row["actor"], kind, payload)
                elif kind == "political_offer":
                    content = (
                        f"{row['actor']} offered {payload.get('item','an item')} "
                        f"to {payload.get('faction','a faction')} at "
                        f"{payload.get('site','a site')} "
                        f"({payload.get('points',0)} influence)."
                    )
                elif kind in {"political_pledge", "political_break"}:
                    verb_name = ("pledged allegiance to" if kind == "political_pledge"
                                 else "broke allegiance to")
                    content = f"{row['actor']} {verb_name} {payload.get('faction','a faction')}."
                elif kind == "say":
                    content = f"{row['actor']}: {payload.get('text','')}"
                elif kind == "emote":
                    content = f"{row['actor']} {payload.get('text','')}"
                elif kind == "pvp_hit":
                    victim = payload.get("victim")
                    damage = payload.get("damage")
                    if (isinstance(victim, str) and victim
                            and type(damage) is int and damage > 0):
                        content = f"{row['actor']} strikes {victim} for {damage} damage."
                    else:
                        content = f"{row['actor']} attacked."
                elif kind == "join":
                    content = f"{row['actor']} entered the world."
                elif kind == "move":
                    content = f"{row['actor']} arrived."
                elif kind == "leave":
                    content = f"{row['actor']} departed."
                elif kind == "take":
                    content = f"{row['actor']} took {payload.get('item','an item')}."
                elif kind == "drop":
                    content = f"{row['actor']} dropped {payload.get('item','an item')}."
                elif kind == "give":
                    content = (f"{row['actor']} gave {payload.get('item','an item')} "
                               f"to {payload.get('to','someone')}.")
                elif kind == "gather":
                    content = f"{row['actor']} gathered {payload.get('material','resources')} x{payload.get('amount',0)}."
                elif kind in {"craft", "build"}:
                    past = "crafted" if kind == "craft" else "built"
                    content = f"{row['actor']} {past} {payload.get('output','something')}."
                else:
                    content = f"{row['actor']} acted."
                events.append(
                    {"id": row["id"], "kind": kind, "actor": row["actor"],
                     "text": content,
                     "room": None if kind in {"ooc", "crewchat"} else row["room_id"],
                     "at": row["occurred_at"]}
                )
            return {
                "ok": True, "events": events,
                "cursor": events[-1]["id"] if events else after,
                # A client must discard previously shown crewchat on ANY
                # changed trusted-host membership/role epoch, including a
                # 200 response with zero new events after revocation.
                "channel_revision": channel_revision,
            }
        finally:
            db.close()


class _PrincipalRateLimiter:
    """Bounded, per-authorized-player in-memory request admission.

    This is not a socket, IP, credential-guessing or multi-process limiter.
    A full principal table refuses newly seen players rather than evicting
    active buckets and letting an identity churn reset another's quota.
    """

    def __init__(
        self, requests_per_minute: int = DEFAULT_RATE_REQUESTS_PER_MINUTE,
        burst: int = DEFAULT_RATE_BURST,
        max_principals: int = DEFAULT_RATE_MAX_PRINCIPALS,
    ) -> None:
        if type(requests_per_minute) is not int or not 1 <= requests_per_minute <= 60_000:
            raise ValueError("rate_limit_requests_per_minute must be an integer in 1..60000")
        if type(burst) is not int or not 1 <= burst <= 4096:
            raise ValueError("rate_limit_burst must be an integer in 1..4096")
        if type(max_principals) is not int or not 1 <= max_principals <= 1024:
            raise ValueError("rate_limit_max_principals must be an integer in 1..1024")
        self._burst = burst
        self._rate_per_second = requests_per_minute / 60.0
        self._max_principals = max_principals
        self._stale_after = max(60.0, burst / self._rate_per_second)
        self._entries: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, principal: str) -> tuple[bool, int]:
        """Return admission and whole-second Retry-After without storing tokens."""
        key = principal.casefold()
        with self._lock:
            now = time.monotonic()
            old = self._entries.get(key)
            if old is None:
                if len(self._entries) >= self._max_principals:
                    for tracked, (_, last_seen) in tuple(self._entries.items()):
                        if now - last_seen >= self._stale_after:
                            del self._entries[tracked]
                    if len(self._entries) >= self._max_principals:
                        retry = min(
                            max(0.0, last_seen + self._stale_after - now)
                            for _, last_seen in self._entries.values()
                        )
                        return False, max(1, math.ceil(retry))
                tokens = float(self._burst)
            else:
                previous_tokens, last_seen = old
                tokens = min(
                    float(self._burst),
                    previous_tokens + max(0.0, now - last_seen) * self._rate_per_second,
                )
            if tokens < 1.0:
                self._entries[key] = (tokens, now)
                return False, max(1, math.ceil((1.0 - tokens) / self._rate_per_second))
            self._entries[key] = (tokens - 1.0, now)
            return True, 0


class GatewayServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets another listener forcibly claim our
    # already-bound port. Reserve the local game listener exclusively instead.
    # Keep the established POSIX quick-restart behavior unchanged.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    daemon_threads = True

    def __init__(
        self, gateway: Gateway, allowed_origins: tuple[str, ...], port: int = 8768,
        *, rate_limit_requests_per_minute: int = DEFAULT_RATE_REQUESTS_PER_MINUTE,
        rate_limit_burst: int = DEFAULT_RATE_BURST,
        rate_limit_max_principals: int = DEFAULT_RATE_MAX_PRINCIPALS,
        max_active_requests: int = DEFAULT_MAX_ACTIVE_REQUESTS,
        socket_idle_timeout_seconds: float = DEFAULT_SOCKET_IDLE_TIMEOUT_SECONDS,
        body_deadline_seconds: float = DEFAULT_BODY_DEADLINE_SECONDS,
        header_deadline_seconds: float = DEFAULT_HEADER_DEADLINE_SECONDS,
        presence_witness: LocalPresenceWitness | None = None,
    ):
        # No process-wide token or 0.0.0.0 bind option: gameplay uses
        # independent per-player credentials provisioned by the host.
        self.gateway = gateway
        if presence_witness is not None:
            if (not isinstance(presence_witness, LocalPresenceWitness)
                    or gateway.scoped_credentials is None):
                raise ValueError("Presence requires a host witness and scoped HTTP credentials")
        self.presence_witness = presence_witness
        self._presence_bound = False
        self._presence_lock = threading.Lock()
        # One current scoped credential fingerprint per canonical actor, not
        # one new witness lease per 2.2-second browser polling request.
        self._presence_sessions: OrderedDict[str, tuple[str, PresenceLease]] = OrderedDict()
        self._presence_closed = False
        self.allowed_origins = frozenset(allowed_origins)
        self._request_limiter = _PrincipalRateLimiter(
            rate_limit_requests_per_minute, rate_limit_burst,
            rate_limit_max_principals,
        )
        if type(max_active_requests) is not int or not 1 <= max_active_requests <= 1024:
            raise ValueError("max_active_requests must be an integer in 1..1024")
        for value, label in (
            (socket_idle_timeout_seconds, "socket_idle_timeout_seconds"),
            (body_deadline_seconds, "body_deadline_seconds"),
            (header_deadline_seconds, "header_deadline_seconds"),
        ):
            if (type(value) not in (int, float)
                    or not math.isfinite(value) or not 0 < value <= 120):
                raise ValueError(f"{label} must be finite in (0, 120] seconds")
        self.socket_idle_timeout_seconds = float(socket_idle_timeout_seconds)
        self.body_deadline_seconds = float(body_deadline_seconds)
        self.header_deadline_seconds = float(header_deadline_seconds)
        # Bound *all* accepted workers, including unauthenticated requests
        # and clients that never finish headers. Per-principal buckets alone
        # cannot protect thread creation and incomplete POST bodies.
        self._worker_slots = threading.BoundedSemaphore(max_active_requests)
        super().__init__(("127.0.0.1", port), GatewayHandler)
        try:
            if presence_witness is not None:
                bind_host_engine(gateway.engine, presence_witness)
                self._presence_bound = True
        except BaseException:
            super().server_close()
            raise

    def refresh_scoped_presence(self, principal: str, token: str,
                                capability: str) -> None:
        """Host-only, bounded recency proof from a freshly authenticated request.

        A browser's short GET has no durable socket identity. Retain at most
        one credential-bound lease between requests for 10s (witness policy),
        not a claim that the browser tab is continuously connected. Credential
        revocation and the world binding are checked again inside Combat's
        SQLite writer. Never admit legacy/shared-token identity claims.
        """
        witness = self.presence_witness
        scoped = self.gateway.scoped_credentials
        if (witness is None or scoped is None or
                not ScopedCredentialStore._valid_token(token)):
            return
        key = ScopedCredentialStore._digest(token)
        actor_key = principal.casefold()
        def resolver(db: sqlite3.Connection) -> str | None:
            return scoped.authenticate_in_writer(
                db, token, required_capability=capability,
            )
        with self._presence_lock:
            if self._presence_closed:
                return
            registered = self._presence_sessions.get(actor_key)
            if registered is not None:
                prior_key, active = registered
                if prior_key == key:
                    try:
                        active.refresh()
                    except PresenceDenied:
                        active.close()
                        self._presence_sessions.pop(actor_key, None)
                    else:
                        self._presence_sessions.move_to_end(actor_key)
                        return
                else:
                    # Host rotation supersedes this actor's old fingerprint;
                    # the previous lease cannot accumulate or regain access.
                    active.close()
                    self._presence_sessions.pop(actor_key, None)
            # Keep headroom for player-bound Telnet sockets in a combined
            # host. Evict the least recently refreshed HTTP lease before a
            # new admission, never silently grow with every browser poll.
            cap = max(1, min(witness.max_sessions - 8, 248))
            while len(self._presence_sessions) >= cap:
                _, (_, old) = self._presence_sessions.popitem(last=False)
                old.close()
            try:
                lease = witness.admit(
                    principal, protocol="http-scoped-request",
                    resolve_principal=resolver,
                )
            except PresenceDenied:
                # A full witness, expired or revoked credential should only
                # disable PvP eligibility; ordinary feed remains available.
                return
            self._presence_sessions[actor_key] = (key, lease)

    def server_close(self) -> None:
        with self._presence_lock:
            self._presence_closed = True
            active = tuple(lease for _, lease in self._presence_sessions.values())
            self._presence_sessions.clear()
        for lease in active:
            lease.close()
        if self._presence_bound:
            unbind_host_engine(self.gateway.engine, self.presence_witness)
            self._presence_bound = False
        super().server_close()

    def get_request(self) -> tuple[socket.socket, tuple]:
        request, client = super().get_request()
        try:
            request.settimeout(self.socket_idle_timeout_seconds)
        except OSError:
            request.close()
            raise
        return request, client

    def verify_request(self, request: socket.socket, client_address: tuple) -> bool:
        if not self._worker_slots.acquire(blocking=False):
            # Small, nonblocking overload response on the accept loop.
            # No handler thread is allocated and no request body is read.
            try:
                request.setblocking(False)
                request.send(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 34\r\n"
                    b"Connection: close\r\n"
                    b"Cache-Control: no-store\r\n\r\n"
                    b'{"ok":false,"error":"Server busy"}'
                )
            except OSError:
                pass
            return False
        return True

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request(request, client_address)
        except BaseException:
            # Thread.start can fail after verify_request acquired a slot.
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayServer
    protocol_version = "HTTP/1.1"

    def _expire_incomplete_headers(self, guard_token: object) -> None:
        """Interrupt a slow-trickled request before it consumes a worker forever.

        BaseHTTPRequestHandler's buffered readline() uses an inactivity socket
        timeout; a peer sending one byte before each timeout can otherwise keep
        the request line/header parser alive indefinitely. A bounded watchdog
        is needed while stdlib owns that parser. The worker semaphore already
        bounds the number of simultaneous watchdog threads.
        """
        # Timer.cancel() does not wait for an already-running callback. A
        # previous keep-alive request must not expire the next request's
        # freshly armed guard on this same handler instance.
        with self._header_guard_lock:
            if guard_token is not self._header_guard_token or not self._header_guard_pending:
                return
            self._header_guard_pending = False
            self._header_guard_timed_out = True
        try:
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def _finish_header_guard(self) -> bool:
        with self._header_guard_lock:
            self._header_guard_pending = False
            expired = self._header_guard_timed_out
        self._header_guard_timer.cancel()
        return expired

    def handle_one_request(self) -> None:
        # Do not count idle keep-alive time as header parsing time. peek()
        # waits for the first byte under the existing socket inactivity
        # timeout, preserving pipeline-buffered bytes and regular reuse.
        try:
            if not self.rfile.peek(1):
                self.close_connection = True
                return
        except OSError:
            self.close_connection = True
            return

        if not hasattr(self, "_header_guard_lock"):
            self._header_guard_lock = threading.Lock()
        with self._header_guard_lock:
            self._header_guard_pending = True
            self._header_guard_timed_out = False
            self._header_guard_token = object()
            guard_token = self._header_guard_token
        self._header_guard_timer = threading.Timer(
            self.server.header_deadline_seconds, self._expire_incomplete_headers,
            args=(guard_token,),
        )
        self._header_guard_timer.daemon = True
        self._header_guard_timer.start()
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError):
            # Peer reset during stdlib's request-line/header reads is not an
            # application failure. Keep post-parse exceptions observable.
            with self._header_guard_lock:
                parsing = self._header_guard_pending or self._header_guard_timed_out
            if not parsing:
                raise
            self.close_connection = True
        finally:
            self._finish_header_guard()

    def parse_request(self) -> bool:
        # A shutdown at the deadline can make a truncated header look like
        # EOF to http.client.parse_headers. Never dispatch an action based on
        # that partial parse, even if the stdlib parser returned True.
        parsed = super().parse_request()
        expired = self._finish_header_guard()
        if expired:
            self.close_connection = True
            return False
        return parsed

    def log_message(self, format: str, *args: object) -> None:
        # Avoid dumping authorization headers, player messages, and request URLs.
        return

    def _origin(self) -> str | None:
        return self.headers.get("Origin")

    def _origin_ok(self) -> bool:
        origin = self._origin()
        return origin is None or origin in self.server.allowed_origins

    def _headers(self, status: int, payload_length: int) -> None:
        # Some rejected POST requests are denied before their body is read.
        # Never reuse that HTTP/1.1 socket: unread bytes could be parsed as
        # another request, producing an HTML 400 or request desynchronization.
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(payload_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        retry_after = getattr(self, "_rate_retry_after", None)
        if status == 429 and retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        if status >= 400:
            self.send_header("Connection", "close")
        origin = self._origin()
        if origin is not None and origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self._headers(status, len(data))
            self.wfile.write(data)
        except OSError as error:
            # Local-host quiescence detaches selected accepted sockets to wake
            # buffered keep-alive readers. A completed world write may then
            # encounter EBADF while delivering only its HTTP response.
            # Never suppress application errors or errors on other sockets.
            revoked = getattr(self.server, "_realm_local_host_revoked", None)
            if (error.errno != errno.EBADF or not callable(revoked)
                    or not revoked(self.connection)):
                raise
            self.close_connection = True

    def _read_post_body(self, length: int) -> bytes:
        """Read bounded bytes against an absolute deadline, even if trickled."""
        deadline = time.monotonic() + self.server.body_deadline_seconds
        parts: list[bytes] = []
        remaining = length
        previous_timeout = self.connection.gettimeout()
        try:
            while remaining:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError("Body read deadline elapsed")
                self.connection.settimeout(min(self.server.socket_idle_timeout_seconds, budget))
                # read1 returns once available data arrives, unlike read(n)
                # which can wait for all n bytes while an attacker drip-feeds.
                chunk = self.rfile.read1(min(remaining, MAX_BODY_BYTES))
                if not chunk:
                    raise ConnectionError("Client closed an incomplete request body")
                parts.append(chunk)
                remaining -= len(chunk)
        finally:
            self.connection.settimeout(previous_timeout)
        return b"".join(parts)

    def _principal(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        scheme, separator, presented = auth.partition(" ")
        if separator != " " or scheme.casefold() != "bearer":
            return None
        path = urlsplit(self.path).path
        capability = {
            "/api/v1/join": "world:join",
            "/api/v1/command": "command:write",
            "/api/v1/room": "room:read",
            "/api/v1/scene": "room:read",
            "/api/v1/map": "map:read",
            "/api/v1/feed": "feed:read",
            "/api/v1/feed/wait": "feed:read",
            "/api/v1/ship/depart": "ship:depart",
            "/api/v1/ship/events": "ship:depart",
        }.get(path)
        if capability is None:
            return None
        return self.server.gateway.authenticate_remote(
            presented, required_capability=capability
        )

    def _gate(self, *, public_health: bool = False) -> str | None:
        # Request handlers may keep an HTTP/1.1 socket open across requests.
        # A prior rate response must not leak its retry metadata into a later
        # response on the same connection.
        self._rate_retry_after = None
        if not self._origin_ok():
            self._send(403, {"ok": False, "error": "Origin not allowed"})
            return None
        if public_health:
            return ""  # distinguished from no principal (None)
        principal = self._principal()
        if principal is None:
            self._send(401, {"ok": False, "error": "Valid player bearer token required"})
            return None
        # Both GET and POST, including derived long-poll handlers, go through
        # this admission gate. No SQLite transaction is retained here.
        allowed, retry_after = self.server._request_limiter.allow(principal)
        if not allowed:
            self._rate_retry_after = retry_after
            self._send(429, {"ok": False, "error": "Rate limit exceeded"})
            return None
        return principal

    def _player_matches(self, claimed: object, principal: str) -> bool:
        # A caller's player name is merely a backwards-compatible assertion.
        # The verified token, never JSON/query parameters, supplies the actor.
        if claimed is None or (isinstance(claimed, str) and claimed.casefold() == principal.casefold()):
            return True
        self._send(403, {"ok": False, "error": "Player does not match token"})
        return False

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin() or not self._origin_ok():
            self._send(403, {"ok": False, "error": "Origin not allowed"})
            return
        self._headers(204, 0)

    def _params(self) -> tuple[str, dict[str, list[str]]]:
        url = urlsplit(self.path)
        return url.path, parse_qs(url.query)

    def do_GET(self) -> None:  # noqa: N802
        path, params = self._params()
        if path == "/api/v1/health":
            if self._gate(public_health=True) is not None:
                self._send(200, {"ok": True, "world_id": self.server.gateway.world_id})
            return
        principal = self._gate()
        if principal is None:
            return
        ship_response_ship_id: str | None = None
        channel_revision = (
            self.server.gateway.channel_access_revision(principal)
            if path == "/api/v1/feed" else None
        )
        try:
            if path == "/api/v1/feed":
                if not self._player_matches(params.get("player", [None])[0], principal):
                    return
                raw_after = params.get("after", ["0"])[0]
                if not raw_after.isdecimal():
                    raise ValueError("Cursor must be a nonnegative integer")
                result = self.server.gateway.feed(principal, int(raw_after))
            elif path == "/api/v1/room":
                if not self._player_matches(params.get("player", [None])[0], principal):
                    return
                result = self.server.gateway.room(principal)
            elif path == "/api/v1/scene":
                if not self._player_matches(params.get("player", [None])[0], principal):
                    return
                result = self.server.gateway.scene(principal)
            elif path == "/api/v1/map":
                if not self._player_matches(params.get("player", [None])[0], principal):
                    return
                result = self.server.gateway.map(principal)
            elif path == "/api/v1/ship/events":
                # Strict query shape: no world selector, actor assertion, fleet
                # enumeration or repeated cursor/ship parameters.
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if (set(query) - {"ship_id", "after"} or
                        "ship_id" not in query or
                        any(len(values) != 1 for values in query.values())):
                    raise ValueError("Ship ID and optional cursor required")
                raw_after = query.get("after", ["0"])[0]
                if (not raw_after.isascii() or not raw_after.isdecimal() or
                        len(raw_after) > 19 or int(raw_after) > 2**63 - 1):
                    raise ValueError("Cursor must be a bounded nonnegative integer")
                _, _, token = self.headers.get("Authorization", "").partition(" ")
                result = self.server.gateway.ship_events_remote(
                    token, query["ship_id"][0], int(raw_after),
                )
                ship_response_ship_id = query["ship_id"][0]
                # Revoke/rotation during the read cannot return a stale grant.
                if self._principal() != principal:
                    self._send(401, {"ok": False, "error": "Active scoped pilot required"})
                    return
            else:
                self._send(404, {"ok": False, "error": "Unknown endpoint"})
                return
        except ShipAccessDenied:
            self._send(403, {"ok": False, "error": "Ship is not owned by this pilot"})
            return
        except PermissionError:
            self._send(401, {"ok": False, "error": "Active scoped pilot required"})
            return
        except MapUnavailable as error:
            self._send(404, {"ok": False, "error": str(error)})
            return
        except ValueError as error:
            self._send(400, {"ok": False, "error": str(error)})
            return
        # A bearer may have been revoked/rotated while a room/feed/map read
        # was executing. The initial authorization gate does not grant a
        # standing right to deliver results after that revocation. Recheck at
        # the response boundary, like the opt-in long-poll feed. This is not
        # an atomic guarantee against revocation after this check but before
        # the network write (nor a substitute for TLS/public-host policy).
        if self._principal() != principal:
            self._send(401, {"ok": False, "error": "Player bearer revoked or rotated"})
            return
        if (channel_revision is not None and
                channel_revision != self.server.gateway.channel_access_revision(principal)):
            self._send(409, {"ok": False, "error": "Channel membership changed; retry"})
            return
        # The history read may have succeeded under an owner entitlement that
        # has since transferred. Recheck against a new DB snapshot as close to
        # writing response bytes as practical. This does not atomically block
        # a later transfer during the socket write.
        if ship_response_ship_id is not None:
            try:
                if not self.server.gateway.ship_owner_response_current(
                        principal, ship_response_ship_id):
                    self._send(403, {"ok": False, "error": "Ship is not owned by this pilot"})
                    return
            except PermissionError:
                self._send(401, {"ok": False, "error": "Active scoped pilot required"})
                return
            # The owner proof itself performs SQLite reads. Revalidate the
            # credential once more after those reads before any event bytes.
            if self._principal() != principal:
                self._send(401, {"ok": False, "error": "Active scoped pilot required"})
                return
        if path == "/api/v1/feed":
            _, _, presented = self.headers.get("Authorization", "").partition(" ")
            self.server.refresh_scoped_presence(principal, presented, "feed:read")
            # Presence registration itself can wait for a SQLite writer.
            # Do not widen the interval in which a revoked bearer or changed
            # crew membership can receive previously authorized feed bytes.
            if self._principal() != principal:
                self._send(401, {"ok": False, "error": "Player bearer revoked or rotated"})
                return
            if (channel_revision is not None and
                    channel_revision != self.server.gateway.channel_access_revision(principal)):
                self._send(409, {"ok": False, "error": "Channel membership changed; retry"})
                return
        self._send(200, result)

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._params()
        principal = self._gate()
        if principal is None:
            return
        if path not in {"/api/v1/join", "/api/v1/command", "/api/v1/ship/depart"}:
            self._send(404, {"ok": False, "error": "Unknown endpoint"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(415, {"ok": False, "error": "Content-Type must be application/json"})
            return
        raw_length = self.headers.get("Content-Length", "")
        if (not raw_length.isascii() or not raw_length.isdecimal()
                or len(raw_length) > len(str(MAX_BODY_BYTES))
                or int(raw_length) > MAX_BODY_BYTES):
            self._send(413, {"ok": False, "error": "Invalid request length"})
            return
        try:
            content = self._read_post_body(int(raw_length))
        except TimeoutError:
            self.close_connection = True
            try:
                self._send(408, {"ok": False, "error": "Request body timed out"})
            except OSError:
                pass
            return
        except (ConnectionError, OSError):
            self.close_connection = True
            return
        try:
            obj = json.loads(content)
            if not isinstance(obj, dict):
                raise ValueError("Expected a JSON object")
            if not self._player_matches(obj.get("player"), principal):
                return
            # Private group responses include host-issued labels. Treat a
            # role change, revoke, or regrant after this point as a retry,
            # never send a stale authorized group response over the socket.
            command = obj.get("command")
            channel_revision = (
                self.server.gateway.channel_access_revision(principal)
                if path == "/api/v1/command" and isinstance(command, str)
                and command.strip().partition(" ")[0].casefold()
                in {"history", "listen", "crewchat", "ooc"}
                else None
            )
            if self.server.gateway.scoped_credentials is not None:
                # Recheck after reading slow/streamed bodies. This reduces the
                # revocation race, but is NOT atomic with the gameplay commit.
                if self._principal() != principal:
                    self._send(401, {"ok": False, "error": "Active scoped bearer required"})
                    return
                if path == "/api/v1/command":
                    _, _, presented = self.headers.get("Authorization", "").partition(" ")
                    # Admission precedes the combat writer; the actual command
                    # still revalidates the credential atomically in writer.
                    self.server.refresh_scoped_presence(
                        principal, presented, "command:write",
                    )
            if self.server.gateway.scoped_credentials is not None:
                # This token is untrusted until revalidated INSIDE the engine's
                # or crafting's BEGIN IMMEDIATE gameplay transaction.
                _, _, presented = self.headers.get("Authorization", "").partition(" ")
                if path == "/api/v1/join":
                    result = self.server.gateway.join_remote(principal, presented)
                elif path == "/api/v1/ship/depart":
                    result = self.server.gateway.ship_depart_remote(
                        presented, obj.get("ship_id"), obj.get("destination"),
                        obj.get("action_id"),
                    )
                else:
                    result = self.server.gateway.command_remote(
                        principal, obj.get("command"), obj.get("action_id"), presented
                    )
            elif path == "/api/v1/join":
                _, _, presented = self.headers.get("Authorization", "").partition(" ")
                result = self.server.gateway.join_legacy_remote(principal, presented)
            elif path == "/api/v1/ship/depart":
                raise PermissionError("Scoped ship credentials are required")
            else:
                _, _, presented = self.headers.get("Authorization", "").partition(" ")
                result = self.server.gateway.command_legacy_remote(
                    principal, obj.get("command"), obj.get("action_id"), presented,
                )
        except PermissionError:
            self._send(401, {"ok": False, "error": "Active scoped bearer required"})
            return
        except RuntimeError:
            if path != "/api/v1/ship/depart":
                raise
            self._send(409, {"ok": False, "error": "Ship world clock or state not ready"})
            return
        except (ValueError, json.JSONDecodeError) as error:
            self._send(400, {"ok": False, "error": str(error)})
            return
        if channel_revision is not None:
            if self._principal() != principal:
                self._send(401, {"ok": False, "error": "Player bearer revoked or rotated"})
                return
            if channel_revision != self.server.gateway.channel_access_revision(principal):
                self._send(409, {"ok": False, "error": "Channel membership changed; retry"})
                return
        if self._principal() != principal:
            self._send(401, {"ok": False, "error": "Player bearer revoked or rotated"})
            return
        self._send(200, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REALM local-only JSON gateway (prototype)")
    parser.add_argument("--db", type=Path, default=Path("realm.sqlite3"))
    parser.add_argument("--world-id", default="demo")
    parser.add_argument(
        "--adopt-unbound-world", action="store_true",
        help="Explicitly bind an existing pre-identity world database to --world-id (local operator only)",
    )
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument(
        "--scoped-credentials", action="store_true",
        help="Opt in to bounded world-scoped credentials instead of legacy bearers",
    )
    parser.add_argument(
        "--enable-local-presence", action="store_true",
        help="Host-enable 10-second authenticated HTTP recent-activity proof for opt-in PvP",
    )
    parser.add_argument("--demo-crafting", action="store_true", help="Install optional noncanonical gathering and recipe fixtures")
    parser.add_argument(
        "--allow-origin", action="append", default=[], metavar="ORIGIN",
        help="Exact browser origin permitted by CORS, e.g. http://127.0.0.1:8000",
    )
    operator = parser.add_mutually_exclusive_group()
    operator.add_argument("--provision-player", metavar="PLAYER", help="Provision a player token locally, then exit")
    operator.add_argument("--provision-scoped-player", metavar="PLAYER", help="Host-issue a bounded player bearer, then exit")
    operator.add_argument("--rotate-player", metavar="PLAYER", help="Rotate a player token locally, then exit")
    operator.add_argument("--revoke-player", metavar="PLAYER", help="Revoke a player token locally, then exit")
    operator.add_argument(
        "--install-politics", action="store_true",
        help="Host-only offline install of optional political allegiances, then exit",
    )
    operator.add_argument(
        "--install-leverage", action="store_true",
        help="Host-only offline political sites and approved resources; requires political oaths",
    )
    parser.add_argument(
        "--political-faction", action="append", default=[], metavar="ID=DISPLAY",
        help="Host-define a political faction; repeat with --install-politics",
    )
    parser.add_argument(
        "--political-site", action="append", default=[], metavar="ID@ROOM=DISPLAY",
        help="Add a political site with --install-leverage (repeatable)",
    )
    parser.add_argument(
        "--political-resource", action="append", default=[], metavar="ITEM=POINTS",
        help="Designate an existing scarce item with --install-leverage (1-5 points)",
    )
    operator.add_argument(
        "--install-pvp-policy", action="store_true",
        help="Host-only one-time default opt-in PvP policy; offline targeting stays forbidden",
    )
    from .combat import _TEMPLATES
    parser.add_argument(
        "--pvp-safe-room", action="append", default=[], metavar="ROOM",
        help="Protect a named existing room from PvP; repeat with --install-pvp-policy",
    )
    parser.add_argument(
        "--pvp-announcement-style", choices=sorted(_TEMPLATES), default=None,
        help="Host-select one original PK announcement when installing PvP policy",
    )
    args = parser.parse_args(argv)
    if args.political_faction and not args.install_politics:
        parser.error("--political-faction requires --install-politics")
    if (args.political_site or args.political_resource) and not args.install_leverage:
        parser.error("--political-site and --political-resource require --install-leverage")
    planned_sites = []
    planned_resources = []
    if args.install_leverage:
        from .political_leverage import _ITEM_ID
        from .political_oaths import _FACTION_ID, _DISPLAY
        for spec in args.political_site:
            ident_room, sep, display = spec.partition("=")
            ident, at, room = ident_room.partition("@")
            if (not sep or not at or _FACTION_ID.fullmatch(ident) is None
                    or _ITEM_ID.fullmatch(room) is None
                    or _DISPLAY.fullmatch(display) is None):
                parser.error("--political-site requires ID@ROOM=DISPLAY")
            planned_sites.append((ident, display, room))
        for spec in args.political_resource:
            ident, sep, score = spec.partition("=")
            if (not sep or _ITEM_ID.fullmatch(ident) is None
                    or not score.isascii() or not score.isdigit()
                    or int(score) not in range(1, 6)):
                parser.error("--political-resource requires ITEM=POINTS (1..5)")
            planned_resources.append((ident, int(score)))
    planned_factions = []
    if args.install_politics:
        from .political_oaths import _FACTION_ID, _DISPLAY
        for spec in args.political_faction:
            key, sep, display = spec.partition("=")
            if (not sep or _FACTION_ID.fullmatch(key) is None
                    or _DISPLAY.fullmatch(display) is None):
                parser.error("--political-faction must use a valid ID=DISPLAY")
            planned_factions.append((key, display))
    if args.enable_local_presence and not args.scoped_credentials:
        parser.error("--enable-local-presence requires --scoped-credentials")
    if args.install_pvp_policy and not args.scoped_credentials:
        parser.error("--install-pvp-policy requires --scoped-credentials")
    if (args.pvp_safe_room or args.pvp_announcement_style is not None) and not args.install_pvp_policy:
        parser.error("PvP safe-room and announcement options require --install-pvp-policy")
    if args.scoped_credentials and (args.provision_player or args.rotate_player or args.revoke_player):
        parser.error("legacy credential operator commands are unavailable in scoped mode")
    if args.provision_scoped_player and not args.scoped_credentials:
        parser.error("--provision-scoped-player requires --scoped-credentials")
    gateway = Gateway(
        args.db, world_id=args.world_id,
        adopt_unbound_world=args.adopt_unbound_world,
        scoped_credentials=args.scoped_credentials,
    )
    if args.provision_scoped_player:
        print(f"New scoped token for {args.provision_scoped_player}: "
              f"{gateway.scoped_credentials.provision(args.provision_scoped_player)}")
        return 0
    if args.provision_player:
        print(f"New token for {args.provision_player}: {gateway.credentials.provision(args.provision_player)}")
        return 0
    if args.rotate_player:
        print(f"Rotated token for {args.rotate_player}: {gateway.credentials.rotate(args.rotate_player)}")
        return 0
    if args.revoke_player:
        print("Revoked" if gateway.credentials.revoke(args.revoke_player) else "Not provisioned")
        return 0
    if args.install_leverage:
        from .political_leverage import WorldLeverage
        leverage = WorldLeverage(gateway.engine.db_path, world_id=gateway.world_id)
        leverage.install()
        leverage.configure_batch(sites=planned_sites, resources=planned_resources)
        print(f"Configured {len(planned_sites)} political sites and "
              f"{len(planned_resources)} approved resources.")
        return 0
    if args.install_politics:
        from .political_oaths import WorldPolitics
        politics = WorldPolitics(gateway.engine.db_path, world_id=gateway.world_id)
        politics.install()
        for key, display in planned_factions:
            politics.create_faction(key, display)
        print(f"Installed optional politics with {len(planned_factions)} configured factions.")
        return 0
    if args.install_pvp_policy:
        from .combat import Combat
        Combat.configure(
            gateway.engine, safe_rooms=tuple(args.pvp_safe_room),
            announcement_style=args.pvp_announcement_style or "herald",
            allow_offline_targets=False,
        )
        print("Installed one-time opt-in PvP policy; offline attacks remain forbidden.")
        return 0
    if args.demo_crafting:
        gateway.router.seed_demo_crafting()
    from .local_host_runner import _AcceptedSessions
    witness = (
        LocalPresenceWitness(gateway.engine, gateway.world_id, idle_seconds=10)
        if args.enable_local_presence else None
    )
    server = GatewayServer(
        gateway, tuple(args.allow_origin), args.port,
        presence_witness=witness,
    )
    # ThreadingHTTPServer daemon workers outlive server_close(), and an
    # authenticated keep-alive socket could otherwise write after the CLI
    # returns. Reuse the local host's accepted-socket custody for this ordinary
    # gateway entrypoint, without enabling a clock or Telnet listener.
    try:
        custody = _AcceptedSessions(server)
    except BaseException:
        # Server construction already bound its listener; if worker custody
        # cannot initialize, no request may be admitted by an orphan socket.
        # Keep the setup exception primary even if listener cleanup fails.
        try:
            server.server_close()
        except BaseException:
            pass
        raise
    try:
        print(f"REALM gateway listening on http://127.0.0.1:{server.server_port}", flush=True)
        print("Private-host player credentials required. Not an internet-facing game server.", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
    finally:
        try:
            server.server_close()
        finally:
            custody.quiesce()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

