"""Host-provisioned, player-bound REALM bearer credentials.

Only the host operator can provision/revoke/rotate credentials through local
Python/CLI access. Network callers never select the player they can act as.
This is an initial *private-host* authorization boundary, not a public
registration, password-reset, rate-limiting, or account-management service.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from .engine import Engine, _PLAYER_RE
from .principal_roles import is_reserved_npc


_CREDENTIAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS player_credentials (
    player_name TEXT PRIMARY KEY COLLATE NOCASE,
    token_digest TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
)
"""


class CredentialStore:
    def __init__(self, engine: Engine):
        self.engine = engine
        with closing(engine._connect()) as db, db:
            db.execute(_CREDENTIAL_SCHEMA_SQL)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(32)

    def provision(self, player_name: str) -> str:
        """Issue one random credential without joining/spawning the character.

        An existing name must be explicitly rotated; no silent takeover.
        Caller must be the host operator, never a public route.
        """
        if not isinstance(player_name, str) or not _PLAYER_RE.fullmatch(player_name):
            raise ValueError("Invalid player name")
        token = self._new_token()
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if is_reserved_npc(db, player_name):
                raise ValueError("Reserved NPC cannot receive player credentials")
            try:
                db.execute(
                    "INSERT INTO player_credentials (player_name,token_digest,created_at) VALUES (?,?,?)",
                    (player_name, self._digest(token), datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Player already provisioned; use explicit rotation") from exc
        return token

    def rotate(self, player_name: str) -> str:
        """Atomically invalidate a player's old token and issue a new one."""
        if not isinstance(player_name, str) or not _PLAYER_RE.fullmatch(player_name):
            raise ValueError("Invalid player name")
        token = self._new_token()
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if is_reserved_npc(db, player_name):
                raise ValueError("Reserved NPC cannot rotate player credentials")
            updated = db.execute(
                "UPDATE player_credentials SET token_digest=?,created_at=? WHERE player_name=?",
                (self._digest(token), datetime.now(timezone.utc).isoformat(timespec="seconds"), player_name),
            )
            if updated.rowcount != 1:
                raise ValueError("No provisioned player by that name")
        return token

    def revoke(self, player_name: str) -> bool:
        """Revoke the remote credential; does not destroy the player's game data."""
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute("DELETE FROM player_credentials WHERE player_name=?", (player_name,))
            return removed.rowcount == 1

    def authenticate(self, token: str) -> str | None:
        """Return the canonical provisioned name, or None without mutating state."""
        if not isinstance(token, str) or not 16 <= len(token) <= 255 or not token.isascii():
            return None
        with closing(self.engine._connect()) as db:
            db.execute("BEGIN")
            result = db.execute(
                "SELECT player_name FROM main.player_credentials WHERE token_digest=?",
                (self._digest(token),),
            ).fetchone()
            if result is not None and is_reserved_npc(db, result[0]):
                return None
        return str(result[0]) if result else None
