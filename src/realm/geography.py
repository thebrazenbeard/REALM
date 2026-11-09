"""Generic, opt-in geography and ship-navigation layer for REALM.

This module owns no fictional canon. Its records coexist with the core Engine
tables in the same SQLite file, but it does not change Engine player positions
or expose unauthenticated network commands. A host must explicitly integrate
validated worldpack data and authenticated actor authorization.
"""

from __future__ import annotations

import heapq
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_PARENTS = {
    "universe": {None},
    "system": {"universe"},
    "planet": {"system"},
    "region": {"planet"},
    "settlement": {"region"},
    "room": {"settlement", "region", "planet"},
}
_TERRAIN = {
    " ": ("void", False),
    ".": ("plain", True),
    "^": ("forest", True),
    "~": ("water", False),
    "#": ("mountain", False),
    ":": ("settlement", True),
    "*": ("starport", True),
    "=": ("road", True),
    "+": ("structure", True),
    "o": ("landmark", True),
}
_MAX_DIMENSION = 256
_MAX_VIEWPORT = 32
_MAX_SQLITE_EPOCH = (1 << 63) - 1


class MapUnavailable(LookupError):
    """The requested Engine room has no configured spatial representation."""


def _check_id(value: str, label: str = "ID") -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must contain 1–64 letters, digits, '-' or '_' and start with a letter")


def _check_name(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 120 or not value.strip():
        raise ValueError("Name must contain 1–120 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Names must not contain control characters")


class Geography:
    """Persistent geography and deterministic route planner for one REALM DB.

    This is a local administrative API, not an authorization boundary. A
    connected world server must validate who may create places, operate ships,
    or observe hidden locations before calling it.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS geo_places (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    parent_id TEXT REFERENCES geo_places(id),
                    x INTEGER,
                    y INTEGER,
                    CHECK ((x IS NULL AND y IS NULL) OR (x IS NOT NULL AND y IS NOT NULL))
                );
                CREATE INDEX IF NOT EXISTS idx_geo_parent ON geo_places(parent_id);
                CREATE TABLE IF NOT EXISTS geo_maps (
                    space_id TEXT PRIMARY KEY REFERENCES geo_places(id),
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    rows_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS geo_links (
                    origin TEXT NOT NULL REFERENCES geo_places(id),
                    destination TEXT NOT NULL REFERENCES geo_places(id),
                    mode TEXT NOT NULL CHECK(mode IN ('walk','ship')),
                    seconds INTEGER NOT NULL CHECK(seconds > 0),
                    PRIMARY KEY(origin,destination,mode)
                );
                CREATE TABLE IF NOT EXISTS geo_ships (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    planet_id TEXT NOT NULL REFERENCES geo_places(id),
                    destination TEXT REFERENCES geo_places(id),
                    arrival_at INTEGER,
                    CHECK((destination IS NULL AND arrival_at IS NULL)
                      OR (destination IS NOT NULL AND arrival_at IS NOT NULL))
                );
                CREATE TABLE IF NOT EXISTS geo_ship_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at INTEGER NOT NULL,
                    ship_id TEXT NOT NULL REFERENCES geo_ships(id),
                    actor TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS geo_ship_receipts (
                    ship_id TEXT NOT NULL REFERENCES geo_ships(id),
                    actor TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    PRIMARY KEY(ship_id,actor,action_id)
                );
                """
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _place(db: sqlite3.Connection, place_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM geo_places WHERE id=?", (place_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown place: {place_id}")
        return row

    def add_place(self, place_id: str, name: str, kind: str,
                  parent_id: str | None = None, *, x: int | None = None,
                  y: int | None = None) -> None:
        """Insert one hierarchical place; a room ID may mirror Engine.rooms.id."""
        _check_id(place_id)
        _check_name(name)
        if kind not in _PARENTS:
            raise ValueError(f"Unsupported place kind: {kind}")
        if (x is None) != (y is None):
            raise ValueError("Map positions require both x and y")
        if x is not None and (type(x) is not int or type(y) is not int or x < 0 or y < 0):
            raise ValueError("Map positions must be nonnegative integers")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent_kind = None
            if parent_id is not None:
                _check_id(parent_id, "Parent ID")
                parent_kind = self._place(db, parent_id)["kind"]
            if parent_kind not in _PARENTS[kind]:
                raise ValueError(f"A {kind} cannot be a child of {parent_kind}")
            if parent_id is None and (x is not None or y is not None):
                raise ValueError("Root universe cannot have parent-relative coordinates")
            if parent_id and x is not None:
                map_row = db.execute("SELECT width,height FROM geo_maps WHERE space_id=?", (parent_id,)).fetchone()
                if map_row is not None and (x >= map_row["width"] or y >= map_row["height"]):
                    raise ValueError("Coordinates lie outside the parent map")
            db.execute(
                "INSERT INTO geo_places(id,name,kind,parent_id,x,y) VALUES(?,?,?,?,?,?)",
                (place_id, name, kind, parent_id, x, y),
            )

    def set_map(self, space_id: str, rows: list[str]) -> None:
        """Declare a terrain raster. '^' represents woods, '~' water."""
        if not isinstance(rows, list) or not rows or not all(isinstance(x, str) for x in rows):
            raise ValueError("Map must be a nonempty list of text rows")
        width = len(rows[0])
        if not 1 <= width <= _MAX_DIMENSION or len(rows) > _MAX_DIMENSION:
            raise ValueError("Map dimensions must be within 1–256")
        if any(len(row) != width or any(char not in _TERRAIN for char in row) for row in rows):
            raise ValueError("Map rows must be rectangular and use supported terrain glyphs")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._place(db, space_id)["kind"] == "room":
                raise ValueError("Room places cannot contain terrain maps")
            outside = db.execute(
                "SELECT id FROM geo_places WHERE parent_id=? AND x IS NOT NULL AND (x>=? OR y>=?) LIMIT 1",
                (space_id, width, len(rows)),
            ).fetchone()
            if outside:
                raise ValueError(f"Existing child {outside['id']} would lie outside the map")
            db.execute(
                "INSERT INTO geo_maps(space_id,width,height,rows_json) VALUES(?,?,?,?) "
                "ON CONFLICT(space_id) DO UPDATE SET width=excluded.width,height=excluded.height,rows_json=excluded.rows_json",
                (space_id, width, len(rows), json.dumps(rows)),
            )

    def snapshot(self, space_id: str, *, center_x: int | None = None,
                 center_y: int | None = None, viewport: int = _MAX_VIEWPORT,
                 player_room_id: str | None = None) -> dict:
        """Return a bounded tile/marker payload for terminal or HTML clients.

        Coordinates in `tiles`, `markers` and `player` are viewport-relative;
        `origin` identifies their absolute offset in the parent terrain map.
        No visibility/access control is provided here: world server must filter.
        """
        if type(viewport) is not int or not 1 <= viewport <= _MAX_VIEWPORT:
            raise ValueError("Viewport must be an integer between 1 and 32")
        with self._connect() as db:
            space = self._place(db, space_id)
            layout = db.execute("SELECT * FROM geo_maps WHERE space_id=?", (space_id,)).fetchone()
            if layout is None:
                raise ValueError(f"No map exists for {space_id}")
            rows = json.loads(layout["rows_json"])
            width = min(viewport, layout["width"])
            height = min(viewport, layout["height"])
            # Default to the upper left, not an arbitrary camera/player position.
            if (center_x is None) != (center_y is None):
                raise ValueError("Map camera needs both coordinates")
            if center_x is not None and (type(center_x) is not int or type(center_y) is not int):
                raise ValueError("Map camera coordinates must be integers")
            origin_x = 0 if center_x is None else max(0, min(layout["width"] - width, center_x - width // 2))
            origin_y = 0 if center_y is None else max(0, min(layout["height"] - height, center_y - height // 2))
            glyphs = set()
            tiles = []
            for y in range(height):
                for x in range(width):
                    glyph = rows[origin_y + y][origin_x + x]
                    glyphs.add(glyph)
                    terrain, passable = _TERRAIN[glyph]
                    tiles.append({"x": x, "y": y, "glyph": glyph,
                                  "terrain": terrain, "passable": passable})
            markers = []
            children = db.execute(
                "SELECT id,name,kind,x,y FROM geo_places "
                "WHERE parent_id=? AND x IS NOT NULL ORDER BY id", (space_id,)
            ).fetchall()
            for child in children:
                x, y = child["x"] - origin_x, child["y"] - origin_y
                if 0 <= x < width and 0 <= y < height:
                    markers.append({"id": child["id"], "name": child["name"],
                                    "kind": child["kind"], "x": x, "y": y})
            player = self._marker_for_room(db, space_id, player_room_id, origin_x, origin_y, width, height)
            return {
                "space": {"id": space["id"], "name": space["name"], "kind": space["kind"]},
                "origin": {"x": origin_x, "y": origin_y},
                "width": width, "height": height,
                "tiles": tiles, "markers": markers, "player": player,
                "legend": [{"glyph": ch, "terrain": _TERRAIN[ch][0]} for ch in sorted(glyphs)],
            }

    def snapshot_for_room(self, room_id: str, *, viewport: int = _MAX_VIEWPORT) -> dict:
        """Choose the closest configured ancestor map containing a room.

        An Engine room with no imported geography returns MapUnavailable, not
        a synthetic or guessed world map. The caller must authorize player
        identity and filter undiscovered/secret places separately.
        """
        with self._connect() as db:
            child = db.execute("SELECT * FROM geo_places WHERE id=?", (room_id,)).fetchone()
            if child is None or child["kind"] != "room":
                raise MapUnavailable("No map is configured for this location")
            for _ in range(16):
                if child["parent_id"] is None:
                    break
                parent = db.execute("SELECT * FROM geo_places WHERE id=?", (child["parent_id"],)).fetchone()
                if parent is None:
                    break
                has_map = db.execute("SELECT 1 FROM geo_maps WHERE space_id=?", (parent["id"],)).fetchone()
                if has_map and child["x"] is not None:
                    return self.snapshot(parent["id"], center_x=child["x"],
                                         center_y=child["y"], viewport=viewport,
                                         player_room_id=room_id)
                child = parent
        raise MapUnavailable("No map is configured for this location")

    @staticmethod
    def _marker_for_room(db: sqlite3.Connection, space_id: str, room_id: str | None,
                         origin_x: int, origin_y: int, width: int, height: int) -> dict | None:
        if room_id is None:
            return None
        # Resolve a descendant room to its direct child of this map layer.
        place = db.execute("SELECT * FROM geo_places WHERE id=?", (room_id,)).fetchone()
        if place is None or place["kind"] != "room":
            return None
        for _ in range(16):  # bounded by current kind hierarchy depth
            if place["parent_id"] == space_id:
                if place["x"] is None:
                    return None
                x, y = place["x"] - origin_x, place["y"] - origin_y
                return {"x": x, "y": y} if 0 <= x < width and 0 <= y < height else None
            if place["parent_id"] is None:
                return None
            place = db.execute("SELECT * FROM geo_places WHERE id=?", (place["parent_id"],)).fetchone()
            if place is None:
                return None
        return None

    def link(self, origin: str, destination: str, mode: str, *, seconds: int = 1) -> None:
        """Create a directed explicit route. Ship links connect planets."""
        if mode not in {"walk", "ship"}:
            raise ValueError("Travel mode must be 'walk' or 'ship'")
        if type(seconds) is not int or not 1 <= seconds <= 86400 * 30:
            raise ValueError("Travel time must be 1–2,592,000 seconds")
        if origin == destination:
            raise ValueError("Travel routes must change location")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            start, end = self._place(db, origin), self._place(db, destination)
            if mode == "ship" and (start["kind"] != "planet" or end["kind"] != "planet"):
                raise ValueError("Ship route endpoints must be planets")
            if mode == "walk" and (start["kind"] != "room" or end["kind"] != "room"):
                raise ValueError("Walking route endpoints must be rooms")
            if mode == "walk" and self._planet(db, origin) != self._planet(db, destination):
                raise ValueError("Walking cannot cross planets")
            db.execute(
                "INSERT INTO geo_links(origin,destination,mode,seconds) VALUES(?,?,?,?) "
                "ON CONFLICT(origin,destination,mode) DO UPDATE SET seconds=excluded.seconds",
                (origin, destination, mode, seconds),
            )

    @staticmethod
    def _planet(db: sqlite3.Connection, place_id: str) -> str | None:
        row = db.execute("SELECT id,kind,parent_id FROM geo_places WHERE id=?", (place_id,)).fetchone()
        while row:
            if row["kind"] == "planet":
                return row["id"]
            if row["parent_id"] is None:
                return None
            row = db.execute("SELECT id,kind,parent_id FROM geo_places WHERE id=?", (row["parent_id"],)).fetchone()
        return None

    def route(self, origin: str, destination: str, mode: str) -> dict | None:
        """Shortest path over author-declared edges; never invents shortcuts."""
        if mode not in {"walk", "ship"}:
            raise ValueError("Unknown travel mode")
        with self._connect() as db:
            self._place(db, origin)
            self._place(db, destination)
            if origin == destination:
                return {"places": [origin], "seconds": 0, "mode": mode}
            links = db.execute("SELECT origin,destination,seconds FROM geo_links WHERE mode=?", (mode,)).fetchall()
        graph = {}
        for edge in links:
            graph.setdefault(edge["origin"], []).append((edge["destination"], edge["seconds"]))
        queue = [(0, (origin,), origin)]
        best = {origin: 0}
        while queue:
            cost, path, node = heapq.heappop(queue)
            if cost != best[node]:
                continue
            if node == destination:
                return {"places": list(path), "seconds": cost, "mode": mode}
            for neighbor, seconds in sorted(graph.get(node, [])):
                next_cost = cost + seconds
                if next_cost < best.get(neighbor, float("inf")):
                    best[neighbor] = next_cost
                    heapq.heappush(queue, (next_cost, path + (neighbor,), neighbor))
        return None

    def register_ship(self, ship_id: str, name: str, owner: str, planet_id: str) -> None:
        """Host-admin call: names are not an authentication mechanism."""
        _check_id(ship_id, "Ship ID")
        _check_name(name)
        _check_id(owner, "Owner")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._place(db, planet_id)["kind"] != "planet":
                raise ValueError("Ships must start at a planet")
            db.execute("INSERT INTO main.geo_ships(id,name,owner,planet_id) VALUES(?,?,?,?)",
                       (ship_id, name, owner, planet_id))

    def ship_status(self, ship_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM main.geo_ships WHERE id=?", (ship_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown ship")
            return {"id": row["id"], "name": row["name"], "owner": row["owner"],
                    "planet": row["planet_id"], "destination": row["destination"],
                    "arrival_at": row["arrival_at"],
                    "state": "in_transit" if row["destination"] else "docked"}

    @staticmethod
    def _receipt(db: sqlite3.Connection, ship_id: str, actor: str,
                 action_id: str | None, command: str) -> dict | None:
        if action_id is None:
            return None
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
            raise ValueError("Action ID must be a nonempty string up to 128 characters")
        row = db.execute(
            "SELECT command,response_json FROM main.geo_ship_receipts "
            "WHERE ship_id=? AND actor=? AND action_id=?", (ship_id, actor, action_id),
        ).fetchone()
        if row is None:
            return None
        if row["command"] != command:
            raise ValueError("Action ID already used for a different command")
        return json.loads(row["response_json"])

    @staticmethod
    def _store_receipt(db: sqlite3.Connection, ship_id: str, actor: str,
                       action_id: str | None, command: str, response: dict) -> None:
        if action_id is not None:
            db.execute(
                "INSERT INTO main.geo_ship_receipts(ship_id,actor,action_id,command,response_json) "
                "VALUES(?,?,?,?,?)",
                (ship_id, actor, action_id, command, json.dumps(response, sort_keys=True)),
            )

    def depart(self, ship_id: str, destination: str, *, actor: str,
               now: int | None = None, action_id: str | None = None) -> dict:
        """Depart on a direct ship link; actor's identity is trusted from host.

        Remote clients MUST NOT be allowed to supply their own authenticated
        actor name. This guard is ownership matching, not authentication.
        """
        at = int(time.time()) if now is None else now
        self._valid_epoch(at)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._depart_on_connection(db, ship_id, destination, actor, at, action_id)

    @staticmethod
    def _valid_epoch(at: int) -> None:
        if type(at) is not int or not 0 <= at <= _MAX_SQLITE_EPOCH:
            raise ValueError("Time must be a nonnegative integer fitting SQLite signed 64-bit")

    def _depart_on_connection(self, db: sqlite3.Connection, ship_id: str,
                              destination: str, actor: str, at: int,
                              action_id: str | None) -> dict:
        ship = db.execute("SELECT * FROM main.geo_ships WHERE id=?", (ship_id,)).fetchone()
        if ship is None:
            raise ValueError("Unknown ship")
        if ship["owner"] != actor:
            raise PermissionError("Actor is not the registered ship owner")
        command = "depart:" + destination
        receipt = self._receipt(db, ship_id, actor, action_id, command)
        if receipt is not None:
            return receipt
        if ship["destination"] is not None:
            raise ValueError("Ship is already in transit")
        link = db.execute(
            "SELECT seconds FROM main.geo_links WHERE origin=? AND destination=? AND mode='ship'",
            (ship["planet_id"], destination),
        ).fetchone()
        if link is None:
            raise ValueError("No direct ship route is declared")
        arrival = at + link["seconds"]
        self._valid_epoch(arrival)
        db.execute("UPDATE main.geo_ships SET destination=?,arrival_at=? WHERE id=? AND destination IS NULL",
                   (destination, arrival, ship_id))
        payload = {"from": ship["planet_id"], "to": destination, "arrival_at": arrival}
        db.execute("INSERT INTO main.geo_ship_events(occurred_at,ship_id,actor,kind,payload_json) VALUES(?,?,?,?,?)",
                   (at, ship_id, actor, "depart", json.dumps(payload, sort_keys=True)))
        response = {"ship": ship_id, "state": "in_transit", **payload}
        self._store_receipt(db, ship_id, actor, action_id, command, response)
        return response

    def depart_in_transaction(self, db: sqlite3.Connection, ship_id: str,
                              destination: str, *, actor: str, now: int,
                              action_id: str) -> dict:
        """Host-only departure in an already-open SAME-world SQLite transaction.

        The caller owns the outer transaction and must authenticate actor.
        SAVEPOINT guarantees exception atomicity when a caller catches errors.
        """
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("Expected an SQLite connection")
        if not db.in_transaction:
            raise RuntimeError("Existing SQLite transaction required")
        main_file = next(
            (row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"),
            None,
        )
        if not main_file or Path(main_file).resolve() != self.db_path.resolve():
            raise ValueError("Connection points to a different world database")
        if db.row_factory is not sqlite3.Row:
            raise ValueError("Connection must use sqlite3.Row row_factory")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Shared departure requires enabled foreign keys")
        self._valid_epoch(now)
        if action_id is None:
            raise ValueError("Shared departure requires stable action_id")
        db.execute("SAVEPOINT realm_ship_departure")
        try:
            response = self._depart_on_connection(
                db, ship_id, destination, actor, now, action_id)
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT realm_ship_departure")
            db.execute("RELEASE SAVEPOINT realm_ship_departure")
            raise
        else:
            db.execute("RELEASE SAVEPOINT realm_ship_departure")
            return response

    def arrive(self, ship_id: str, *, now: int | None = None,
               action_id: str | None = None) -> dict:
        """Standalone host tick/admin call. No autonomous loop is implied."""
        at = int(time.time()) if now is None else now
        if type(at) is not int or not 0 <= at <= _MAX_SQLITE_EPOCH:
            raise ValueError("Time must be a nonnegative integer fitting SQLite signed 64-bit")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._arrive_on_connection(db, ship_id, at, action_id)

    def arrive_in_transaction(self, db: sqlite3.Connection, ship_id: str, *,
                              now: int, action_id: str) -> dict:
        """Host-only clock adapter: use the caller's SAME world transaction.

        No new connection, outer BEGIN, COMMIT or outer ROLLBACK occurs.
        A nested SQLite SAVEPOINT makes this method exception-atomic even
        when its host caller catches an error and commits other work.
        WorldClock.tick passes its write-transaction connection to the trusted
        callback, which must supply the host-authorized ship, UTC epoch second
        and stable event ID as action_id. This method neither schedules a
        departure's arrival nor authenticates a remote caller.
        """
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("Expected an SQLite connection")
        if not db.in_transaction:
            raise RuntimeError("Existing SQLite transaction required")
        main_file = next(
            (row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"),
            None,
        )
        if not main_file or Path(main_file).resolve() != self.db_path.resolve():
            raise ValueError("Connection points to a different world database")
        if db.row_factory is not sqlite3.Row:
            raise ValueError("Connection must use sqlite3.Row row_factory")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Shared arrival requires enabled foreign keys")
        if type(now) is not int or not 0 <= now <= _MAX_SQLITE_EPOCH:
            raise ValueError("Time must be a nonnegative integer fitting SQLite signed 64-bit")
        if action_id is None:
            raise ValueError("Shared arrival requires stable action_id")
        db.execute("SAVEPOINT realm_ship_arrival")
        try:
            response = self._arrive_on_connection(db, ship_id, now, action_id)
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT realm_ship_arrival")
            db.execute("RELEASE SAVEPOINT realm_ship_arrival")
            raise
        else:
            db.execute("RELEASE SAVEPOINT realm_ship_arrival")
            return response

    def _arrive_on_connection(self, db: sqlite3.Connection, ship_id: str,
                              at: int, action_id: str | None) -> dict:
        ship = db.execute("SELECT * FROM main.geo_ships WHERE id=?", (ship_id,)).fetchone()
        if ship is None:
            raise ValueError("Unknown ship")
        command = "arrive"
        receipt = self._receipt(db, ship_id, "system", action_id, command)
        if receipt is not None:
            return receipt
        if ship["destination"] is None:
            raise ValueError("Ship is already docked")
        if at < ship["arrival_at"]:
            raise ValueError("Ship has not reached its destination")
        db.execute("UPDATE main.geo_ships SET planet_id=?,destination=NULL,arrival_at=NULL WHERE id=?",
                   (ship["destination"], ship_id))
        payload = {"from": ship["planet_id"], "to": ship["destination"]}
        db.execute("INSERT INTO main.geo_ship_events(occurred_at,ship_id,actor,kind,payload_json) VALUES(?,?,?,?,?)",
                   (at, ship_id, "system", "arrive", json.dumps(payload, sort_keys=True)))
        response = {"ship": ship_id, "state": "docked", "planet": ship["destination"]}
        self._store_receipt(db, ship_id, "system", action_id, command, response)
        return response
