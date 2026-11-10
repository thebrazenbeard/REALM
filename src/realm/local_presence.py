"""Host-only, fail-closed authenticated session-liveness witness.

This is a reusable primitive, NOT an installed combat authorization route.
Only a trusted HTTP/Telnet handler may admit or refresh a lease *after* it has
authenticated a real socket request. A saved player location, bearer token
alone, client-provided "online" flag, and untrusted model output are not
presence evidence.

The witness is in-process by design. A separate gateway process cannot
implicitly borrow its sessions, and a restarted host begins with none.
Its claim is narrowly "recent active authenticated local session", not a
proof of human attention or of a physically connected remote peer.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .engine import Engine


_PLAYER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
_WORLD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_PROTOCOLS = frozenset({"telnet-player-credential", "http-scoped-request"})
_DENIAL = "Authenticated active-session witness required"


class PresenceDenied(PermissionError):
    """No valid host-authenticated, recent, in-process session witness."""


@dataclass(eq=False)
class _Session:
    principal: str
    protocol: str
    resolve_principal: Callable[[sqlite3.Connection], str | None]
    refreshed_at: float


class PresenceLease:
    """Opaque host lifecycle handle; its existence is never an auth proof."""

    __slots__ = ("_owner", "_session")

    def __init__(self, owner: LocalPresenceWitness, session: _Session):
        self._owner = owner
        self._session = session

    def refresh(self) -> None:
        """Host calls only after new authenticated activity on this socket."""
        self._owner.refresh(self)

    def close(self) -> None:
        """Revoke a lease on socket/request completion; idempotent."""
        self._owner.close(self)


class LocalPresenceWitness:
    """One host process's strictly scoped, nonpersistent session witness.

    A registered identity resolver must return the actual principal for
    the same credential under a world SQLite writer transaction. A
    no-return authorization guard cannot be substituted: that would permit
    accidental principal misbinding. There is intentionally no
    unauthenticated `touch(player)` or client-supplied `is_online` route.

    The target verifier must be called from the combat writer transaction.
    This does not attest to instant remote TCP delivery, network partition
    recovery, or cross-process presence.

    Lock order is always SQLite writer -> _lock when both are required.
    Socket close holds only _lock. NEVER wait for SQLite while holding _lock:
    combat already owns its writer before it enters this witness.
    """

    def __init__(
        self, engine: Engine, world_id: str, *,
        idle_seconds: int = 20, max_sessions: int = 256,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(engine, Engine):
            raise TypeError("An Engine is required")
        if not isinstance(world_id, str) or not _WORLD.fullmatch(world_id):
            raise ValueError("Invalid presence world")
        if type(idle_seconds) is not int or not 1 <= idle_seconds <= 300:
            raise ValueError("Presence idle bound must be 1–300 seconds")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 4096:
            raise ValueError("Presence lease capacity must be 1–4096")
        if not callable(monotonic):
            raise TypeError("A monotonic clock is required")
        self._engine = engine
        self._path = Path(engine.db_path).resolve()
        self._world_id = world_id
        self._idle_seconds = idle_seconds
        self._max_sessions = max_sessions
        self._monotonic = monotonic
        self._sessions: set[_Session] = set()
        self._lock = threading.RLock()

    def _now(self) -> float:
        value = self._monotonic()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise PresenceDenied(_DENIAL)
        return float(value)

    @property
    def idle_seconds(self) -> int:
        """Read-only upper bound for trusted transport heartbeat scheduling."""
        return self._idle_seconds

    @property
    def max_sessions(self) -> int:
        """Read-only cap for the host's corresponding pooled-lease map."""
        return self._max_sessions

    def _world_bound(self, db: sqlite3.Connection) -> None:
        if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
            raise PresenceDenied(_DENIAL)
        if db.execute("PRAGMA query_only").fetchone()[0]:
            raise PresenceDenied(_DENIAL)
        main = [
            Path(row[2]).resolve()
            for row in db.execute("PRAGMA database_list")
            if row[1] == "main" and row[2]
        ]
        if main != [self._path]:
            raise PresenceDenied(_DENIAL)
        # Force reservation of the writer slot without changing any row;
        # revocations/rotations cannot commit across the following guard.
        try:
            db.execute("UPDATE main.world_meta SET value=value WHERE 0")
        except sqlite3.Error as exc:
            raise PresenceDenied(_DENIAL) from exc
        world = db.execute(
            "SELECT value FROM main.world_meta WHERE key='world_id'"
        ).fetchone()
        if world is None or world[0] != self._world_id:
            raise PresenceDenied(_DENIAL)

    def _check_identity(
        self, db: sqlite3.Connection, principal: str,
        resolve_principal: Callable[[sqlite3.Connection], str | None],
    ) -> None:
        try:
            result = resolve_principal(db)
            if not isinstance(result, str) or result.casefold() != principal.casefold():
                raise PresenceDenied(_DENIAL)
        except (PermissionError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise PresenceDenied(_DENIAL) from exc
        row = db.execute(
            "SELECT id FROM main.players WHERE id=? COLLATE NOCASE", (principal,)
        ).fetchone()
        if row is None or str(row[0]).casefold() != principal.casefold():
            raise PresenceDenied(_DENIAL)

    def admit(
        self, principal: str, *, protocol: str,
        resolve_principal: Callable[[sqlite3.Connection], str | None],
    ) -> PresenceLease:
        """Host admits a real socket/request only after credential verification.

        This revalidates credentials inside a *new* real world writer.
        The supplied callback must return the principal currently resolved
        from the authenticated credential in that transaction. Generic
        shared-token Telnet cannot provide such a callback.
        """
        if not isinstance(principal, str) or not _PLAYER.fullmatch(principal):
            raise PresenceDenied(_DENIAL)
        if protocol not in _PROTOCOLS or not callable(resolve_principal):
            raise PresenceDenied(_DENIAL)
        with closing(self._engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._world_bound(db)
            self._check_identity(db, principal, resolve_principal)
        session = _Session(principal.casefold(), protocol, resolve_principal, self._now())
        with self._lock:
            now = self._now()
            # A browser's HTTP requests may intentionally retain a
            # short-lived inactivity lease. Bound memory even if a handler
            # fails to close, pruning expired leases without trying to
            # acquire a DB writer while the in-memory lock is held.
            self._sessions.difference_update(
                old for old in tuple(self._sessions)
                if now < old.refreshed_at
                or now - old.refreshed_at >= self._idle_seconds
            )
            if len(self._sessions) >= self._max_sessions:
                raise PresenceDenied("Active-session witness capacity exceeded")
            session.refreshed_at = now
            self._sessions.add(session)
        return PresenceLease(self, session)

    def refresh(self, lease: PresenceLease) -> None:
        """Refresh only a live, not-expired lease with current credentials."""
        # Combat's require_pair holds the SQLite writer before _lock; using
        # the inverse order here creates an ABBA deadlock when refresh and
        # combat race. Obtain the writer FIRST, then inspect the lease.
        with closing(self._engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._world_bound(db)
            with self._lock:
                session = self._resolve(lease)
                now = self._now()
                if now < session.refreshed_at or now - session.refreshed_at >= self._idle_seconds:
                    self._sessions.discard(session)
                    raise PresenceDenied(_DENIAL)
                try:
                    self._check_identity(db, session.principal, session.resolve_principal)
                except PresenceDenied:
                    # A revoked/rotated credential must not occupy a pooled
                    # HTTP lease slot until its idle deadline.
                    self._sessions.discard(session)
                    raise
                session.refreshed_at = now

    def close(self, lease: PresenceLease) -> None:
        if not isinstance(lease, PresenceLease) or lease._owner is not self:
            raise PresenceDenied(_DENIAL)
        with self._lock:
            self._sessions.discard(lease._session)

    def _resolve(self, lease: PresenceLease) -> _Session:
        if (not isinstance(lease, PresenceLease) or lease._owner is not self
                or lease._session not in self._sessions):
            raise PresenceDenied(_DENIAL)
        return lease._session

    def require_pair_in_writer(
        self, db: sqlite3.Connection, attacker: str, target: str,
    ) -> None:
        """Fail closed unless BOTH names have fresh authoritatively bound leases.

        Call inside the existing combat transaction. Locks are held only
        during this check; a later socket disconnect is not a rollback
        directive after combat has been accepted.
        """
        if (not isinstance(attacker, str) or not _PLAYER.fullmatch(attacker)
                or not isinstance(target, str) or not _PLAYER.fullmatch(target)):
            raise PresenceDenied(_DENIAL)
        # A caller may have started with deferred BEGIN rather than
        # BEGIN IMMEDIATE. Reserve the SQLite writer BEFORE taking _lock,
        # or refresh(writer -> lock) and this upgrade(lock -> writer) form
        # a second ABBA deadlock despite the refresh fix.
        self._world_bound(db)
        with self._lock:
            now = self._now()
            for principal in (attacker.casefold(), target.casefold()):
                live = False
                for session in tuple(self._sessions):
                    if session.principal != principal:
                        continue
                    if now < session.refreshed_at or now - session.refreshed_at >= self._idle_seconds:
                        self._sessions.discard(session)
                        continue
                    try:
                        self._check_identity(db, principal, session.resolve_principal)
                    except PresenceDenied:
                        self._sessions.discard(session)
                        continue
                    live = True
                    break
                if not live:
                    raise PresenceDenied(_DENIAL)
