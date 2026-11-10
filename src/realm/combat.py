"""Explicitly installed, host-governed, deterministic one-world PvP.

This is an optional local prototype, not a remote identity service. Its policy
is installed by trusted host code, never by an incoming gameplay command.
Player positions persist across disconnects and DO NOT prove online presence.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine


_PLAYER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
_TEMPLATES = {
    # Original REALM text, not attributed to or copied from another MUD.
    "herald": ("realm-original-herald-v1",
               "Hear the iron bell: {victim} has fallen before {killer}. The realm bears witness."),
    "stark": ("realm-original-stark-v1",
              "The record stands: {killer} defeated {victim}."),
}

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS combat_policy ("
    "id INTEGER PRIMARY KEY CHECK(id=1), max_hp INTEGER NOT NULL CHECK(max_hp BETWEEN 1 AND 1000), "
    "damage INTEGER NOT NULL CHECK(damage BETWEEN 1 AND 1000), "
    "safe_rooms_json TEXT NOT NULL, allow_offline_targets INTEGER NOT NULL CHECK(allow_offline_targets IN (0,1)), "
    "announcement_style TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS combatants ("
    "player TEXT PRIMARY KEY COLLATE NOCASE REFERENCES players(id), "
    "pvp_enabled INTEGER NOT NULL CHECK(pvp_enabled IN (0,1)), "
    "hp INTEGER NOT NULL CHECK(hp BETWEEN 0 AND 1000))",
    "CREATE TABLE IF NOT EXISTS combat_announcements ("
    "event_id INTEGER PRIMARY KEY REFERENCES events(id), "
    "template_id TEXT NOT NULL, message TEXT NOT NULL)",
)


class Combat:
    @staticmethod
    def require_living_for_mutation(db: sqlite3.Connection, player: str) -> None:
        """Enforce the optional world's ghost/spectator state in the SAME writer.

        A dead character may observe, chat or sign out, but cannot move,
        transfer items or produce resources until trusted host revival.
        This is not a corpse, limbo, or automatic-respawn implementation.
        Legacy worlds without installed combat policy remain unchanged.
        Check before action-receipt replay, so a formerly valid command cannot
        masquerade as permitted current gameplay after death.
        """
        installed = db.execute(
            "SELECT 1 FROM main.sqlite_master "
            "WHERE type='table' AND name='combat_policy'"
        ).fetchone()
        if installed is None:
            return
        existing = db.execute(
            "SELECT hp FROM main.combatants WHERE player=? COLLATE NOCASE", (player,)
        ).fetchone()
        if existing is not None and existing["hp"] == 0:
            raise ValueError("Defeated characters cannot modify the world until host revival")

    @staticmethod
    def configure(
        engine: Engine, *, max_hp: int = 12, damage: int = 4,
        safe_rooms: tuple[str, ...] = (), announcement_style: str = "herald",
        allow_offline_targets: bool = False,
    ) -> None:
        """Host-only, one-time policy installation; never an HTTP/Telnet verb.

        Offline attacks are denied unless the HOST deliberately enables them.
        This engine has no authoritative live-session/online-presence witness.
        """
        if (type(max_hp) is not int or not 1 <= max_hp <= 1000
                or type(damage) is not int or not 1 <= damage <= max_hp):
            raise ValueError("Combat HP/damage must be bounded positive integers")
        if type(allow_offline_targets) is not bool:
            raise ValueError("Offline-target policy requires an explicit boolean")
        if announcement_style not in _TEMPLATES:
            raise ValueError("Unknown original REALM announcement style")
        if (not isinstance(safe_rooms, tuple) or len(safe_rooms) > 256
                or any(not isinstance(name, str) or not name for name in safe_rooms)
                or len(set(safe_rooms)) != len(safe_rooms)):
            raise ValueError("Safe rooms must be a bounded tuple of distinct room IDs")
        with closing(engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for room in safe_rooms:
                if db.execute("SELECT 1 FROM rooms WHERE id=?", (room,)).fetchone() is None:
                    raise ValueError("Safe room must already exist in this world")
            for sql in _SCHEMA:
                db.execute(sql)
            if db.execute("SELECT 1 FROM combat_policy WHERE id=1").fetchone() is not None:
                raise ValueError("Combat policy already installed; no implicit replacement")
            db.execute(
                "INSERT INTO combat_policy VALUES (1,?,?,?,?,?)",
                (max_hp, damage, json.dumps(safe_rooms), int(allow_offline_targets), announcement_style),
            )

    @staticmethod
    def _policy(db: sqlite3.Connection) -> sqlite3.Row:
        if db.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='combat_policy'"
        ).fetchone() is None:
            raise ValueError("PvP is not enabled in this world")
        policy = db.execute("SELECT * FROM combat_policy WHERE id=1").fetchone()
        if policy is None:
            raise ValueError("PvP is not enabled in this world")
        return policy

    @staticmethod
    def _status(db: sqlite3.Connection, player: str, max_hp: int) -> tuple[int, bool]:
        row = db.execute(
            "SELECT hp,pvp_enabled FROM combatants WHERE player=? COLLATE NOCASE", (player,)
        ).fetchone()
        return (max_hp, False) if row is None else (row["hp"], bool(row["pvp_enabled"]))

    @classmethod
    def perform(
        cls, db: sqlite3.Connection, *, engine: Engine, actor: str,
        room: str, verb: str, arg: str, action_id: str | None,
    ) -> str:
        policy = cls._policy(db)
        max_hp = int(policy["max_hp"])
        if verb == "pvp":
            setting = arg.casefold()
            if setting == "status":
                hp, enabled = cls._status(db, actor, max_hp)
                online = "offline targeting allowed" if policy["allow_offline_targets"] else "online presence unverified; attacks blocked"
                return f"PvP {'ON' if enabled else 'OFF'} | HP {hp}/{max_hp} | {online}."
            if setting == "news":
                announcements = db.execute(
                    "SELECT event_id,message FROM combat_announcements "
                    "ORDER BY event_id DESC LIMIT 5"
                ).fetchall()
                return "\n".join(
                    f"[event {row['event_id']}] {row['message']}" for row in reversed(announcements)
                ) or "No PvP deaths have been announced."
            if setting not in {"on", "off"}:
                raise ValueError("Use pvp on, pvp off, pvp status or pvp news")
            hp, enabled = cls._status(db, actor, max_hp)
            if setting == "on" and hp == 0:
                raise ValueError("Defeated characters require host revival")
            desired = setting == "on"
            if enabled != desired:
                db.execute(
                    "INSERT INTO combatants(player,pvp_enabled,hp) VALUES (?,?,?) "
                    "ON CONFLICT(player) DO UPDATE SET pvp_enabled=excluded.pvp_enabled",
                    (actor, int(desired), hp),
                )
            return ("PvP is now ON; host policy permits offline attacks."
                    if desired and policy["allow_offline_targets"] else
                    "PvP is now ON; attacks require verified live presence."
                    if desired else "PvP is now OFF.")

        if verb != "attack":
            raise ValueError("Unknown combat command")
        if not _PLAYER.fullmatch(arg):
            raise ValueError("Invalid PvP target")
        if arg.casefold() == actor.casefold():
            raise ValueError("You cannot attack yourself")
        target = db.execute("SELECT id,room_id FROM players WHERE id=? COLLATE NOCASE", (arg,)).fetchone()
        if target is None:
            raise ValueError("PvP target does not exist in this world")
        if target["room_id"] != room:
            raise ValueError("PvP combat requires both characters in the same room")
        own_hp, own_enabled = cls._status(db, actor, max_hp)
        their_hp, their_enabled = cls._status(db, target["id"], max_hp)
        if not own_enabled or not their_enabled:
            raise ValueError("PvP target or attacker is not opted in")
        if own_hp == 0 or their_hp == 0:
            raise ValueError("defeated characters cannot fight or be attacked")
        if room in json.loads(policy["safe_rooms_json"]):
            raise ValueError("PvP combat is forbidden in this safe zone")
        if not policy["allow_offline_targets"]:
            raise ValueError("Target presence cannot be verified; host policy denies offline attacks")
        amount = min(int(policy["damage"]), their_hp)
        remaining = their_hp - amount
        changed = db.execute(
            "UPDATE combatants SET hp=? WHERE player=? COLLATE NOCASE AND hp=? AND pvp_enabled=1",
            (remaining, target["id"], their_hp),
        )
        if changed.rowcount != 1:
            raise ValueError("Combat target state changed during attack")
        engine._event(db, actor, room, "pvp_hit", {
            "victim": target["id"], "damage": amount, "remaining_hp": remaining,
            "action_id": action_id,
        })
        if remaining > 0:
            return f"{actor} strikes {target['id']} for {amount} damage. HP {remaining}/{max_hp}."
        template_id, template = _TEMPLATES[policy["announcement_style"]]
        message = template.format(killer=actor, victim=target["id"])
        identity: list[tuple[int, str]] = []
        engine._event(db, actor, room, "pvp_death", {
            "killer": actor, "victim": target["id"], "damage": amount,
            "remaining_hp": 0, "action_id": action_id,
            "template_id": template_id, "message": message,
        }, emissions=identity)
        db.execute(
            "INSERT INTO combat_announcements(event_id,template_id,message) VALUES (?,?,?)",
            (identity[0][0], template_id, message),
        )
        return f"{actor} strikes {target['id']} for {amount} damage. {target['id']} has fallen."

    @classmethod
    def revive(cls, engine: Engine, player: str) -> None:
        """Host-only recovery; not exposed by the gameplay command router."""
        if not _PLAYER.fullmatch(player):
            raise ValueError("Invalid recovery target")
        with closing(engine._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            policy = cls._policy(db)
            target = db.execute("SELECT player,hp FROM combatants WHERE player=? COLLATE NOCASE", (player,)).fetchone()
            if target is None or target["hp"] != 0:
                raise ValueError("Only a defeated combatant may be revived")
            db.execute("UPDATE combatants SET hp=?,pvp_enabled=0 WHERE player=? COLLATE NOCASE",
                       (policy["max_hp"], target["player"]))
