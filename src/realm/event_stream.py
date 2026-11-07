"""Opt-in authenticated, bounded long-poll event feed for a private REALM host.

This does not start or install a service. A host explicitly constructs
FeedWaitServer in place of GatewayServer. Polling delegates to the
existing current-room feed; it creates no gameplay events or authority.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import parse_qs, urlsplit

from .gateway import Gateway, GatewayHandler, GatewayServer

MAX_WAIT_MS = 5_000
DEFAULT_WAIT_MS = 1_000
POLL_SECONDS = 0.10
MAX_WAITERS = 32
_MAX_CURSOR = 2**63 - 1
_ALLOWED_QUERY = frozenset({"after", "wait_ms", "player"})


class FeedWaitServer(GatewayServer):
    """Loopback-only opt-in gateway with bounded per-player long-poll reads."""

    def __init__(
        self, gateway: Gateway, allowed_origins: tuple[str, ...], port: int = 8768,
        *, max_waiters: int = 8,
    ):
        if type(max_waiters) is not int or not 1 <= max_waiters <= MAX_WAITERS:
            raise ValueError("max_waiters must be an integer from 1 to 32")
        super().__init__(gateway, allowed_origins, port)
        # Base GatewayServer binds and does not begin serving automatically.
        self.RequestHandlerClass = FeedWaitHandler
        self.feed_slots = threading.BoundedSemaphore(max_waiters)
        self.stop_waits = threading.Event()

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

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/v1/feed/wait":
            super().do_GET()
            return
        principal = self._gate()
        if principal is None:
            return
        try:
            after, wait_ms, claimed = self._query()
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
        if not self.server.feed_slots.acquire(blocking=False):
            self._send(429, {"ok": False, "error": "Feed waiter limit reached"})
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
                    result = self.server.gateway.feed(principal, after)
                except ValueError as exc:
                    self._send(400, {"ok": False, "error": str(exc)})
                    return
                if result["events"] or time.monotonic() >= deadline:
                    if self._principal() != principal:
                        self._send(401, {"ok": False, "error": "Player token revoked or rotated"})
                        return
                    self._send(200, result)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    continue
                self.server.stop_waits.wait(min(POLL_SECONDS, remaining))
        finally:
            self.server.feed_slots.release()
