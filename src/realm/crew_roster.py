"""Opt-in, host-only, player-bound first-officer appointment ledger.

This module provides a durable role **display** truth source; it does not
authorize ship departure, transfer ownership, add an HTTP route, or publish
credentials. The ship's persisted registered owner must authenticate under
the very same SQLite writer transaction that changes the role.

No schema is installed on module import or CrewRoster construction. The
host must explicitly call install() on an existing world whose id is bound.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .auth import CredentialStore
from .engine import _PLAYER_RE
from .title_flags import _IDENTIFIER, _MAX_EPOCH


_ACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_REQUIRED_TABLES = ("crew_role_state", "crew_role_events", "crew_role_receipts")


class CrewRoster:
    """One world-bound, durable current first officer plus audit receipts.

    The caller must hold a host-provisioned credential issued to the current
    registered owner; neither a client-supplied owner string nor model output
    establishes appointment authority. This class is not a public API.
    """

    def __init__(self, db_path: str | Path, *, world_id: str):
        if not isinstance(world_id, str) or _IDENTIFIER.fullmatch(world_id) is None:
            raise ValueError("Crew roster requires a valid world identifier")
        if not isinstance(db_path, (str, Path)):
            raise TypeError("Crew roster requires a world path")
        self.db_path = Path(db_path).resolve()
        self.world_id = world_id

    @contextmanager
    def _connect(self, *, write: bool = False):
        # Do not silently create a second world by mistyping a path.
        if not self.db_path.is_file():
            raise FileNotFoundError("Crew roster world file does not exist")
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if write:
                    db.commit()
            except BaseException:
                if write:
                    db.rollback()
                raise

    def _check_world(self, db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT value FROM main.world_meta WHERE key='world_id'"
        ).fetchone()
        if row is None or row[0] != self.world_id:
            raise PermissionError("Crew roster world identity mismatch")

    @staticmethod
    def _installed(db: sqlite3.Connection) -> bool:
        found = {row[0] for row in db.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' "
            "AND name IN ('crew_role_state','crew_role_events','crew_role_receipts')"
        )}
        if not found:
            return False
        if found != set(_REQUIRED_TABLES):
            raise RuntimeError("Crew roster schema is only partially installed")
        return True

    def _require_installed(self, db: sqlite3.Connection) -> None:
        if not self._installed(db):
            raise RuntimeError("Host must explicitly install the crew role schema")

    def install(self) -> None:
        """Install explicit host-only role state/receipt tables on one world."""
        with self._connect(write=True) as db:
            self._check_world(db)
            if db.execute(
                "SELECT 1 FROM main.sqlite_master WHERE name='geo_ships' "
                "AND type='table'"
            ).fetchone() is None:
                raise RuntimeError("Geography ships must be installed before crew roles")
            # executescript() would silently COMMIT our BEGIN IMMEDIATE
            # before creating the schema. Keep all DDL inside this one
            # transaction so a partially invalid installation rolls back.
            statements = (
                """CREATE TABLE IF NOT EXISTS main.crew_role_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_utc INTEGER NOT NULL CHECK(occurred_utc>=0),
                    ship_id TEXT NOT NULL REFERENCES geo_ships(id),
                    owner TEXT NOT NULL,
                    officer TEXT,
                    kind TEXT NOT NULL CHECK(kind IN ('appoint','revoke'))
                )""",
                """CREATE TABLE IF NOT EXISTS main.crew_role_state (
                    ship_id TEXT PRIMARY KEY REFERENCES geo_ships(id),
                    owner TEXT NOT NULL,
                    officer TEXT NOT NULL REFERENCES players(id),
                    event_id INTEGER NOT NULL REFERENCES crew_role_events(id)
                )""",
                """CREATE TABLE IF NOT EXISTS main.crew_role_receipts (
                    ship_id TEXT NOT NULL REFERENCES geo_ships(id),
                    actor TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    PRIMARY KEY(ship_id,actor,action_id)
                )""",
                """CREATE TRIGGER IF NOT EXISTS main.crew_role_owner_invalidation
                AFTER UPDATE OF owner ON geo_ships
                WHEN NEW.owner <> OLD.owner
                BEGIN
                    DELETE FROM crew_role_state WHERE ship_id=NEW.id;
                END""",
            )
            for statement in statements:
                db.execute(statement)
            self._require_installed(db)

    @staticmethod
    def _check_request(ship_id: str, action_id: str, now_utc: int) -> None:
        if not isinstance(ship_id, str) or _IDENTIFIER.fullmatch(ship_id) is None:
            raise ValueError("Invalid crew ship ID")
        if not isinstance(action_id, str) or _ACTION_ID.fullmatch(action_id) is None:
            raise ValueError("Invalid crew action ID")
        if type(now_utc) is not int or not 0 <= now_utc <= _MAX_EPOCH:
            raise ValueError("Crew appointment requires a valid host UTC second")

    @staticmethod
    def _authenticate_owner(db: sqlite3.Connection, token: str, ship_id: str) -> str:
        if (not isinstance(token, str) or not 16 <= len(token) <= 255
                or not token.isascii()):
            raise PermissionError("Current ship owner credential required")
        principal = db.execute(
            "SELECT player_name FROM main.player_credentials WHERE token_digest=?",
            (CredentialStore._digest(token),),
        ).fetchone()
        ship = db.execute(
            "SELECT owner FROM main.geo_ships WHERE id=?", (ship_id,),
        ).fetchone()
        if ship is None or principal is None or principal[0] != ship[0]:
            raise PermissionError("Current ship owner credential required")
        joined = db.execute(
            "SELECT id FROM main.players WHERE id=? COLLATE NOCASE",
            (principal[0],),
        ).fetchone()
        if joined is None or joined[0] != principal[0]:
            raise PermissionError("Current ship owner must be a joined principal")
        return principal[0]

    @staticmethod
    def _active(db: sqlite3.Connection, ship_id: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT s.ship_id,s.owner,s.officer,s.event_id "
            "FROM main.crew_role_state AS s "
            "JOIN main.geo_ships AS g ON g.id=s.ship_id AND g.owner=s.owner "
            "WHERE s.ship_id=?",
            (ship_id,),
        ).fetchone()

    def current(self, ship_id: str) -> dict[str, str | int] | None:
        """Read present role, not a historical or cached appointment."""
        if not isinstance(ship_id, str) or _IDENTIFIER.fullmatch(ship_id) is None:
            raise ValueError("Invalid crew ship ID")
        with self._connect() as db:
            self._check_world(db)
            if not self._installed(db):
                return None
            state = self._active(db, ship_id)
            return dict(state) if state is not None else None

    def appoint(self, owner_token: str, *, ship_id: str, officer: str,
                action_id: str, now_utc: int) -> dict[str, object]:
        """Appoint one joined officer, authenticated inside BEGIN IMMEDIATE."""
        self._check_request(ship_id, action_id, now_utc)
        if not isinstance(officer, str) or _PLAYER_RE.fullmatch(officer) is None:
            raise ValueError("Invalid officer identifier")
        with self._connect(write=True) as db:
            self._check_world(db)
            self._require_installed(db)
            owner = self._authenticate_owner(db, owner_token, ship_id)
            canonical = db.execute(
                "SELECT id FROM main.players WHERE id=? COLLATE NOCASE", (officer,)
            ).fetchone()
            if canonical is None or canonical[0] == owner:
                raise PermissionError("First officer must be a different joined player")
            return self._commit(
                db, ship_id=ship_id, owner=owner, officer=canonical[0],
                action_id=action_id, now_utc=now_utc, kind="appoint",
            )

    def revoke(self, owner_token: str, *, ship_id: str,
               action_id: str, now_utc: int) -> dict[str, object]:
        """Revoke current role; stale signed display claims cease to qualify."""
        self._check_request(ship_id, action_id, now_utc)
        with self._connect(write=True) as db:
            self._check_world(db)
            self._require_installed(db)
            owner = self._authenticate_owner(db, owner_token, ship_id)
            return self._commit(
                db, ship_id=ship_id, owner=owner, officer=None,
                action_id=action_id, now_utc=now_utc, kind="revoke",
            )

    def _commit(self, db: sqlite3.Connection, *, ship_id: str, owner: str,
                officer: str | None, action_id: str, now_utc: int,
                kind: str) -> dict[str, object]:
        command = kind + (":" + officer if kind == "appoint" else "")
        receipt = db.execute(
            "SELECT command,response_json FROM main.crew_role_receipts "
            "WHERE ship_id=? AND actor=? AND action_id=?",
            (ship_id, owner, action_id),
        ).fetchone()
        if receipt is not None:
            if receipt["command"] != command:
                raise ValueError("Crew action ID already used for a different command")
            prior = json.loads(receipt["response_json"])
            active = self._active(db, ship_id)
            prior["replayed"] = True
            prior["active"] = (
                active is not None
                and prior["kind"] == "appoint"
                and active["officer"] == prior["officer"]
                and active["event_id"] == prior["event_id"]
            )
            return prior

        current = self._active(db, ship_id)
        applied = False
        event_id = None
        result_officer = officer
        if kind == "appoint":
            if officer is None:
                raise RuntimeError("Internal crew appointment missing officer")
            if current is not None:
                if current["officer"] != officer:
                    raise ValueError("Existing officer must be revoked before replacement")
                event_id = current["event_id"]
            else:
                cursor = db.execute(
                    "INSERT INTO main.crew_role_events(occurred_utc,ship_id,owner,officer,kind) "
                    "VALUES(?,?,?,?,'appoint')",
                    (now_utc, ship_id, owner, officer),
                )
                event_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO main.crew_role_state(ship_id,owner,officer,event_id) "
                    "VALUES(?,?,?,?)",
                    (ship_id, owner, officer, event_id),
                )
                applied = True
        else:
            if current is not None:
                result_officer = current["officer"]
                cursor = db.execute(
                    "INSERT INTO main.crew_role_events(occurred_utc,ship_id,owner,officer,kind) "
                    "VALUES(?,?,?,?,'revoke')",
                    (now_utc, ship_id, owner, result_officer),
                )
                event_id = cursor.lastrowid
                db.execute(
                    "DELETE FROM main.crew_role_state WHERE ship_id=? AND owner=?",
                    (ship_id, owner),
                )
                applied = True
        response: dict[str, object] = {
            "kind": kind, "ship_id": ship_id, "owner": owner,
            "officer": result_officer, "event_id": event_id,
            "applied": applied,
        }
        db.execute(
            "INSERT INTO main.crew_role_receipts(ship_id,actor,action_id,command,response_json) "
            "VALUES(?,?,?,?,?)",
            (ship_id, owner, action_id, command,
             json.dumps(response, separators=(",", ":"), sort_keys=True)),
        )
        return {**response, "replayed": False, "active": kind == "appoint"}
