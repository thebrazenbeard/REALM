"""Host-curated character display names, separate from login principals.

Profiles belong to the SQLite world instance, not to global credentials.
No network route is authorized to set profiles by importing this module.
"""

from __future__ import annotations

import unicodedata
from contextlib import closing
from datetime import datetime, timezone

from .engine import Engine, _PLAYER_RE


_PROFILE_SQL = """
CREATE TABLE IF NOT EXISTS player_profiles (
    player_name TEXT PRIMARY KEY COLLATE NOCASE REFERENCES players(id),
    display_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_ALLOWED_PUNCTUATION = frozenset("'’“”.-")


def _display_name(value: str) -> str:
    """Normalize a visible name while excluding markup, controls, and bidi spoofing."""
    if not isinstance(value, str):
        raise ValueError("Display name must be text")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Display names cannot contain controls or format characters")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(normalized) <= 64:
        raise ValueError("Display name must be between 1 and 64 characters")
    if not all(
        char == " "
        or char in _ALLOWED_PUNCTUATION
        or unicodedata.category(char)[0] in {"L", "M", "N"}
        for char in normalized
    ):
        raise ValueError("Display name contains unsupported characters")
    return normalized


class PlayerProfileStore:
    """Explicit host-only metadata; names never confer authentication authority."""

    def __init__(self, engine: Engine):
        self.engine = engine
        with closing(engine._connect()) as db, db:
            db.execute(_PROFILE_SQL)

    @staticmethod
    def _actor(player_name: str) -> str:
        if not isinstance(player_name, str) or not _PLAYER_RE.fullmatch(player_name):
            raise ValueError("Invalid player principal")
        return player_name

    def get(self, player_name: str) -> str | None:
        """Return display name, fallback joined actor ID, or None if not joined."""
        name = self._actor(player_name)
        with closing(self.engine._connect()) as db:
            result = db.execute(
                "SELECT p.id, r.display_name FROM players AS p "
                "LEFT JOIN player_profiles AS r ON r.player_name=p.id WHERE p.id=?",
                (name,),
            ).fetchone()
        return str(result["display_name"] or result["id"]) if result else None

    def set(self, player_name: str, display_name: str) -> str:
        """Set joined player's visible label; host operator chooses authority."""
        name = self._actor(player_name)
        label = _display_name(display_name)
        with closing(self.engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            player = db.execute("SELECT id FROM players WHERE id=?", (name,)).fetchone()
            if player is None:
                raise ValueError("Player must join this world before setting a profile")
            db.execute(
                "INSERT INTO player_profiles (player_name,display_name,updated_at) "
                "VALUES (?,?,?) ON CONFLICT(player_name) DO UPDATE SET "
                "display_name=excluded.display_name,updated_at=excluded.updated_at",
                (player["id"], label, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        return label
