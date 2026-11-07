"""Opt-in host-managed, bounded, per-world bearer credential lifecycle.

This does not alter the legacy ``player_credentials`` table or grant host/admin
rights. The outside-DB witness rejects rollback of the SQLite file *alone*.
It is not device binding, TLS, protection against a stolen current bearer, or
protection if an attacker can also restore/replace the witness itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .engine import Engine, _PLAYER_RE


_WORLD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DEFAULT_GATEWAY_CAPABILITIES = (
    "world:join", "command:write", "room:read", "map:read", "feed:read",
)
_ALLOWED_CAPABILITIES = frozenset((*_DEFAULT_GATEWAY_CAPABILITIES, "ship:depart"))
_PROCESS_CLOCK_LOCK = threading.Lock()
_PROCESS_HIGH_WATER: dict[tuple[str, str], int] = {}
_TABLES = (
    "CREATE TABLE IF NOT EXISTS main.scoped_auth_guard ("
    "world_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
    "nonce TEXT NOT NULL, max_seen_us INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS main.scoped_player_credentials ("
    "world_id TEXT NOT NULL, player_name TEXT NOT NULL COLLATE NOCASE, "
    "token_digest TEXT NOT NULL UNIQUE, issued_utc TEXT NOT NULL, "
    "expires_utc TEXT NOT NULL, issued_us INTEGER NOT NULL, "
    "expires_us INTEGER NOT NULL, generation INTEGER NOT NULL, "
    "capabilities_json TEXT NOT NULL, "
    "revoked_us INTEGER, PRIMARY KEY (world_id, player_name))",
)


class CredentialStateError(RuntimeError):
    """A clock, world, or independent storage witness check failed closed."""


class ScopedCredentialStore:
    """Host-only API; never expose provisioning/rotation directly as HTTP routes.

    Every auth transaction checks a separate atomic-file witness, including
    high-water UTC. The witness moves before SQLite commit: an interrupted
    operation can lock out credentials, requiring explicit host recovery, but
    cannot silently restore an old credential. Keep it outside DB backups.
    """

    def __init__(
        self, engine: Engine, world_id: str, *, guard_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(world_id, str) or not _WORLD_ID.fullmatch(world_id):
            raise ValueError("Invalid scoped credential world ID")
        self.engine = engine
        self.world_id = world_id
        self.guard_path = Path(guard_path) if guard_path is not None else Path(
            str(engine.db_path) + ".scoped-auth-witness.json"
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._observation_key = (str(Path(engine.db_path).resolve()), world_id)
        with closing(engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._check_world(db)
            for statement in _TABLES:
                db.execute(statement)
            row = db.execute(
                "SELECT revision,nonce,max_seen_us FROM main.scoped_auth_guard WHERE world_id=?",
                (world_id,),
            ).fetchone()
            if row is None:
                if self.guard_path.exists():
                    raise CredentialStateError("Witness exists but SQLite state is missing")
                nonce = secrets.token_hex(32)
                now_us = self._now_us()
                self._write_witness(0, nonce, now_us)
                db.execute(
                    "INSERT INTO main.scoped_auth_guard VALUES (?,?,?,?)",
                    (world_id, 0, nonce, now_us),
                )
            else:
                self._check_witness(row)

    def _check_world(self, db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT value FROM main.world_meta WHERE key='world_id'"
        ).fetchone()
        if row is None or row[0] != self.world_id:
            raise ValueError("Scoped credentials require exact bound world identity")

    def _now_us(self) -> int:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Credential clock must return aware UTC datetime")
        delta = value.astimezone(timezone.utc) - _EPOCH
        return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds

    @staticmethod
    def _utc(value: int) -> str:
        return (_EPOCH + timedelta(microseconds=value)).isoformat(timespec="seconds")

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _valid_token(token: object) -> bool:
        return isinstance(token, str) and 16 <= len(token) <= 255 and token.isascii()

    @staticmethod
    def _ttl(value: int) -> int:
        if type(value) is not int or not 1 <= value <= 3600:
            raise ValueError("Credential TTL must be an integer from 1 to 3600 seconds")
        return value

    @staticmethod
    def _capabilities(values: tuple[str, ...] | None) -> str:
        capabilities = _DEFAULT_GATEWAY_CAPABILITIES if values is None else values
        if (not isinstance(capabilities, tuple) or not capabilities or
                any(type(value) is not str or value not in _ALLOWED_CAPABILITIES
                    for value in capabilities) or len(set(capabilities)) != len(capabilities)):
            raise ValueError("Invalid explicit credential capabilities")
        return json.dumps(sorted(capabilities), separators=(",", ":"))

    @staticmethod
    def _player(value: str) -> str:
        if not isinstance(value, str) or not _PLAYER_RE.fullmatch(value):
            raise ValueError("Invalid player name")
        return value

    def _witness_payload(self, revision: int, nonce: str, seen: int) -> dict:
        return {"world_id": self.world_id, "revision": revision,
                "nonce": nonce, "max_seen_us": seen}

    def _check_witness(self, row: tuple) -> None:
        try:
            data = json.loads(self.guard_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialStateError("Missing or damaged independent credential witness") from exc
        if data != self._witness_payload(int(row[0]), str(row[1]), int(row[2])):
            raise CredentialStateError("Credential witness diverged from SQLite; fail closed")

    def _write_witness(self, revision: int, nonce: str, seen: int) -> None:
        self.guard_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._witness_payload(revision, nonce, seen), sort_keys=True)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".scoped-auth-", dir=self.guard_path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.guard_path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _begin(self, db: sqlite3.Connection) -> tuple[int, tuple]:
        db.execute("BEGIN IMMEDIATE")
        self._check_world(db)
        state = db.execute(
            "SELECT revision,nonce,max_seen_us FROM main.scoped_auth_guard WHERE world_id=?",
            (self.world_id,),
        ).fetchone()
        if state is None:
            raise CredentialStateError("Missing scoped credential guard")
        self._check_witness(state)
        now = self._now_us()
        if now < int(state[2]):
            raise CredentialStateError("UTC clock moved backwards; credentials denied")
        self._observe_in_process(now)
        return now, state

    def _observe_in_process(self, now: int) -> None:
        # One process-wide high-water for all store instances bound to same DB.
        # Cross-process rollback after reads alone is not durable evidence.
        with _PROCESS_CLOCK_LOCK:
            prior = _PROCESS_HIGH_WATER.get(self._observation_key)
            if prior is not None and now < prior:
                raise CredentialStateError("UTC clock moved backwards in this process")
            _PROCESS_HIGH_WATER[self._observation_key] = now

    def _advance(self, db: sqlite3.Connection, state: tuple, now: int) -> None:
        revision = int(state[0]) + 1
        nonce = secrets.token_hex(32)
        seen = max(now, int(state[2]))
        # Move witness FIRST. Crash-before-DB-commit fails closed on restart.
        self._write_witness(revision, nonce, seen)
        db.execute(
            "UPDATE main.scoped_auth_guard SET revision=?,nonce=?,max_seen_us=? "
            "WHERE world_id=?",
            (revision, nonce, seen, self.world_id),
        )

    def provision(self, player_name: str, *, ttl_seconds: int = 900,
                  capabilities: tuple[str, ...] | None = None) -> str:
        """Host issues one bounded credential. Duplicate/revoked identity needs separate policy."""
        player_name = self._player(player_name)
        ttl = self._ttl(ttl_seconds)
        capabilities_json = self._capabilities(capabilities)
        token = secrets.token_urlsafe(32)
        with closing(self.engine._connect()) as db, db:
            now, state = self._begin(db)
            if db.execute(
                "SELECT 1 FROM main.scoped_player_credentials WHERE world_id=? AND player_name=?",
                (self.world_id, player_name),
            ).fetchone():
                raise ValueError("Already provisioned; use explicit rotation")
            expires = now + ttl * 1_000_000
            db.execute(
                "INSERT INTO main.scoped_player_credentials VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                (self.world_id, player_name, self._digest(token), self._utc(now),
                 self._utc(expires), now, expires, 1, capabilities_json),
            )
            self._advance(db, state, now)
        return token

    def authenticate(self, token: str, *, required_capability: str | None = None) -> str | None:
        if not self._valid_token(token):
            return None
        if required_capability is not None and required_capability not in _ALLOWED_CAPABILITIES:
            return None
        with closing(self.engine._connect()) as db:
            db.execute("BEGIN")
            self._check_world(db)
            state = db.execute(
                "SELECT revision,nonce,max_seen_us FROM main.scoped_auth_guard WHERE world_id=?",
                (self.world_id,),
            ).fetchone()
            if state is None:
                raise CredentialStateError("Missing credential guard")
            self._check_witness(state)
            now = self._now_us()
            if now < int(state[2]):
                raise CredentialStateError("UTC clock moved backwards; credentials denied")
            self._observe_in_process(now)
            row = db.execute(
                "SELECT player_name,capabilities_json,issued_us,expires_us,revoked_us "
                "FROM main.scoped_player_credentials WHERE world_id=? AND token_digest=?",
                (self.world_id, self._digest(token)),
            ).fetchone()
        if row is None or row[4] is not None or now < row[2]:
            return None
        if now >= row[3]:
            # An actual matching expired token is tombstoned ONCE. Forged and
            # normally active bearer checks never enter a writer transaction.
            self._expire_once(token)
            return None
        if required_capability is not None and required_capability not in json.loads(row[1]):
            return None
        return str(row[0])

    def _expire_once(self, token: str) -> None:
        with closing(self.engine._connect()) as db, db:
            now, state = self._begin(db)
            updated = db.execute(
                "UPDATE main.scoped_player_credentials SET revoked_us=? "
                "WHERE world_id=? AND token_digest=? AND revoked_us IS NULL "
                "AND expires_us<=?",
                (now, self.world_id, self._digest(token), now),
            )
            if updated.rowcount == 1:
                self._advance(db, state, now)

    def rotate(self, current_token: str, *, ttl_seconds: int = 900) -> str:
        """Atomically consume current bearer; concurrent/replayed rotation loses."""
        ttl = self._ttl(ttl_seconds)
        if not self._valid_token(current_token):
            raise ValueError("Current active token required")
        next_token = secrets.token_urlsafe(32)
        with closing(self.engine._connect()) as db, db:
            now, state = self._begin(db)
            updated = db.execute(
                "UPDATE main.scoped_player_credentials SET token_digest=?,issued_utc=?,"
                "expires_utc=?,issued_us=?,expires_us=?,generation=generation+1 "
                "WHERE world_id=? AND token_digest=? AND revoked_us IS NULL "
                "AND issued_us<=? AND expires_us>?",
                (self._digest(next_token), self._utc(now), self._utc(now + ttl * 1_000_000),
                 now, now + ttl * 1_000_000, self.world_id, self._digest(current_token),
                 now, now),
            )
            if updated.rowcount != 1:
                raise ValueError("Current active token required")
            self._advance(db, state, now)
        return next_token

    def revoke(self, player_name: str) -> bool:
        """Host-only principal revoke, regardless of concurrent bearer rotations."""
        player_name = self._player(player_name)
        with closing(self.engine._connect()) as db, db:
            now, state = self._begin(db)
            updated = db.execute(
                "UPDATE main.scoped_player_credentials SET revoked_us=? "
                "WHERE world_id=? AND player_name=? AND revoked_us IS NULL",
                (now, self.world_id, player_name),
            )
            if updated.rowcount != 1:
                return False
            self._advance(db, state, now)
            return True
