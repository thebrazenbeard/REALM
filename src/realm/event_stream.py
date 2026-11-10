"""Opt-in authenticated, bounded long-poll event feed for a private REALM host.

This does not start or install a service. A host explicitly constructs
FeedWaitServer in place of GatewayServer. Polling delegates to the
existing current-room feed and enforces event-time entry bounds; it creates
no gameplay events or authority.
"""
from __future__ import annotations

import threading
import time
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

from .gateway import Gateway, GatewayHandler, GatewayServer
from .local_presence import LocalPresenceWitness

MAX_WAIT_MS = 5_000
DEFAULT_WAIT_MS = 1_000
POLL_SECONDS = 0.10
MAX_WAITERS = 32
MAX_PLAYER_WAITERS = 2
_MAX_CURSOR = 2**63 - 1
_ALLOWED_QUERY = frozenset({"after", "wait_ms", "player"})


class FeedWaitServer(GatewayServer):
    """Loopback-only opt-in gateway with bounded per-player long-poll reads."""

    def __init__(
        self, gateway: Gateway, allowed_origins: tuple[str, ...], port: int = 8768,
        *, max_waiters: int = 8,
        presence_witness: LocalPresenceWitness | None = None,
    ):
        if type(max_waiters) is not int or not 1 <= max_waiters <= MAX_WAITERS:
            raise ValueError("max_waiters must be an integer from 1 to 32")
        super().__init__(
            gateway, allowed_origins, port, presence_witness=presence_witness,
        )
        # Base GatewayServer binds and does not begin serving automatically.
        self.RequestHandlerClass = FeedWaitHandler
        self.feed_slots = threading.BoundedSemaphore(max_waiters)
        self.player_wait_lock = threading.Lock()
        self.player_wait_counts: dict[str, int] = {}
        self.stop_waits = threading.Event()

    def reserve_wait(self, principal: str) -> bool:
        """Bound each player independently so one cannot exhaust all slots."""
        with self.player_wait_lock:
            count = self.player_wait_counts.get(principal, 0)
            if count >= MAX_PLAYER_WAITERS or not self.feed_slots.acquire(blocking=False):
                return False
            self.player_wait_counts[principal] = count + 1
            return True

    def release_wait(self, principal: str) -> None:
        with self.player_wait_lock:
            count = self.player_wait_counts[principal]
            if count <= 1:
                del self.player_wait_counts[principal]
            else:
                self.player_wait_counts[principal] = count - 1
            self.feed_slots.release()

    def shutdown(self) -> None:
        self.stop_waits.set()
        super().shutdown()

    def server_close(self) -> None:
        self.stop_waits.set()
        super().server_close()


class FeedWaitHandler(GatewayHandler):
    server: FeedWaitServer

    @staticmethod
    def _number(value: str, *, name: str, maximum: int) -> int:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError(f"{name} must be a nonnegative integer")
        if len(value) > len(str(maximum)):
            raise ValueError(f"{name} exceeds the allowed bound")
        number = int(value)
        if not 0 <= number <= maximum:
            raise ValueError(f"{name} exceeds the allowed bound")
        return number

    def _query(self) -> tuple[int, int, str | None]:
        raw = urlsplit(self.path).query
        query = parse_qs(raw, keep_blank_values=True)
        if set(query) - _ALLOWED_QUERY or any(len(values) != 1 for values in query.values()):
            raise ValueError("Unexpected or repeated feed query parameter")
        after = self._number(query.get("after", ["0"])[0], name="after", maximum=_MAX_CURSOR)
        wait_ms = self._number(
            query.get("wait_ms", [str(DEFAULT_WAIT_MS)])[0],
            name="wait_ms", maximum=MAX_WAIT_MS,
        )
        player = query.get("player", [None])[0]
        return after, wait_ms, player

    def _room_entry(self, principal: str) -> tuple[str, int]:
        """Fail closed unless an engine event proves current-room arrival.

        A current-room match is not historical permission: prior occupants'
        messages must remain invisible before first arrival or after re-entry.
        """
        with closing(self.server.gateway._read()) as db:
            player = db.execute(
                "SELECT room_id FROM players WHERE id=? COLLATE NOCASE", (principal,)
            ).fetchone()
            if player is None:
                raise ValueError("Join this REALM before reading the feed")
            room = str(player["room_id"])
            entry = db.execute(
                "SELECT MAX(id) FROM events WHERE actor=? COLLATE NOCASE "
                "AND room_id=? AND kind IN ('join','move')", (principal, room),
            ).fetchone()[0]
            if entry is None:
                raise ValueError("No provable current-room arrival; feed denied")
            return room, int(entry)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path not in {"/api/v1/feed/wait", "/api/v1/feed"}:
            super().do_GET()
            return
        # The legacy endpoint must share event-time audience enforcement.
        # Falling through to GatewayHandler would disclose pre-entry history.
        principal = self._gate()
        if principal is None:
            return
        try:
            after, wait_ms, claimed = self._query()
            if path == "/api/v1/feed":
                wait_ms = 0  # Preserve immediate legacy polling semantics.
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
            return
        if not self._player_matches(claimed, principal):
            return
        # A cursor from another world (or an invented far-future ID) must not
        # silently starve this session forever; each world has its own ID space.
        if after > self.server.gateway.cursor():
            self._send(400, {"ok": False, "error": "Cursor exceeds this world's event head"})
            return
        # Immediate reads never occupy long-wait slots. Waiting readers use
        # both per-principal fairness and global memory/thread bounds.
        reserved = wait_ms > 0
        if reserved and not self.server.reserve_wait(principal):
            self._send(429, {"ok": False, "error": "Player or host waiter limit reached"})
            return
        try:
            deadline = time.monotonic() + wait_ms / 1000
            while True:
                if self.server.stop_waits.is_set():
                    self._send(503, {"ok": False, "error": "Feed host shutting down"})
                    return
                # Re-validate every cycle: a bearer can be revoked or rotated
                # while a long poll is waiting. Never use a cached principal.
                if self._principal() != principal:
                    self._send(401, {"ok": False, "error": "Player token revoked or rotated"})
                    return
                try:
                    channel_revision = self.server.gateway.channel_access_revision(principal)
                    room, entry = self._room_entry(principal)
                    # Gateway enforces the private room's arrival floor. A
                    # global PK herald uses the first world-join floor and
                    # survives subsequent moves, so never clamp its cursor
                    # to the latest local arrival.
                    result = self.server.gateway.feed(principal, after)
                    # A concurrent move could change current-room scope between
                    # two database reads; never deliver from a stale audience.
                    if self._room_entry(principal) != (room, entry):
                        continue
                    if channel_revision != self.server.gateway.channel_access_revision(principal):
                        self._send(409, {"ok": False, "error": "Channel membership changed; retry"})
                        return
                    events = [
                        e for e in result["events"]
                        if (e["room"] == room and e["id"] >= entry)
                        or (e["room"] == "global" and e["kind"] == "pvp_death")
                        or (e["room"] is None and e["kind"] in {"ooc", "crewchat"})
                    ]
                    result["events"] = events
                    result["cursor"] = events[-1]["id"] if events else after
                except ValueError as exc:
                    self._send(400, {"ok": False, "error": str(exc)})
                    return
                if result["events"] or time.monotonic() >= deadline:
                    if self._principal() != principal:
                        self._send(401, {"ok": False, "error": "Player token revoked or rotated"})
                        return
                    if channel_revision != self.server.gateway.channel_access_revision(principal):
                        self._send(409, {"ok": False, "error": "Channel membership changed; retry"})
                        return
                    _, _, presented = self.headers.get("Authorization", "").partition(" ")
                    self.server.refresh_scoped_presence(principal, presented, "feed:read")
                    if self._principal() != principal:
                        self._send(401, {"ok": False, "error": "Player token revoked or rotated"})
                        return
                    if channel_revision != self.server.gateway.channel_access_revision(principal):
                        self._send(409, {"ok": False, "error": "Channel membership changed; retry"})
                        return
                    self._send(200, result)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    continue
                self.server.stop_waits.wait(min(POLL_SECONDS, remaining))
        finally:
            if reserved:
                self.server.release_wait(principal)
