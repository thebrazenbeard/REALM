"""Opt-in, bounded foreground composition for REALM's two local transports.

No listeners, clock ticks, actor handlers, or credentials are created at import.
An authorized host supplies already-bound loopback servers, an enabled clock,
and an explicit trusted kind-to-handler registry for one SQLite world.
"""
from __future__ import annotations

import math
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .clock_runner import PumpReport, run_foreground
from .gateway import GatewayServer
from .telnet import LocalTelnetServer
from .world_clock import WorldClock


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _AcceptedSessions:
    """Per-run custody of accepted HTTP/Telnet sockets and handler threads.

    ThreadingMixIn's daemon handler threads outlive server_close(), including
    HTTP keep-alive and authenticated Telnet sessions. Track accepted sockets
    before worker creation and release each only after its handler exits.
    """

    def __init__(self, server: GatewayServer | LocalTelnetServer):
        self._condition = threading.Condition()
        self._active: set[socket.socket] = set()
        process_request = server.process_request
        process_request_thread = server.process_request_thread

        def tracked_request(request: socket.socket, client_address: tuple) -> None:
            with self._condition:
                self._active.add(request)
            try:
                process_request(request, client_address)
            except BaseException:
                with self._condition:
                    self._active.discard(request)
                    self._condition.notify_all()
                raise

        def tracked_worker(request: socket.socket, client_address: tuple) -> None:
            try:
                process_request_thread(request, client_address)
            finally:
                with self._condition:
                    self._active.discard(request)
                    self._condition.notify_all()

        # Instance-scoped only. These prebound servers cannot be reused after
        # the foreground call and are closed in its finally block.
        server.process_request = tracked_request
        server.process_request_thread = tracked_worker

    def quiesce(self) -> None:
        with self._condition:
            accepted = tuple(self._active)
        for request in accepted:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        with self._condition:
            while self._active:
                # An HTTP handler may already be inside trusted Python/SQLite
                # code which cannot be safely cancelled by closing its socket.
                # Returning with a timeout error would leave a live world
                # writer that can commit AFTER the host claims to have stopped.
                # This deliberately waits for truthful quiescence; hosts need
                # process isolation for a hard shutdown deadline.
                self._condition.wait()


def run_local_host(
    http: GatewayServer,
    telnet: LocalTelnetServer,
    clock: WorldClock,
    handlers: Mapping[str, Callable],
    *,
    pulses: int,
    interval_seconds: float = 1.0,
    batch_limit: int = 100,
    now: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    stop_event: threading.Event | None = None,
    acknowledge_forward_jump: bool = False,
) -> PumpReport:
    """Serve HTTP+Telnet and pump host UTC until bounded pulses or a stop signal.

    This function blocks in its calling thread; it is not a daemon, persistent
    service, installer, public network listener, or NPC policy implementation.
    Each transport has a short-lived, explicitly joined request-loop thread.
    HTTP/Telnet must already be bound to loopback and the clock's one world DB.

    Handler exceptions propagate after both listeners have shut down. The
    underlying SQLite clock transaction protects event effects and receipts;
    the caller may retry with fresh servers against the same pending event.
    """
    # The caller hands over two *already listening* sockets. Validation is
    # therefore inside an ownership boundary: even a rejected start must close
    # both ports, leaving no stray listener or unusable retry address.
    try:
        if not isinstance(http, GatewayServer) or not isinstance(telnet, LocalTelnetServer):
            raise TypeError("Bound REALM HTTP and Telnet server instances are required")
        if not isinstance(clock, WorldClock):
            raise TypeError("A bound WorldClock instance is required")
        if (http.server_address[0] != "127.0.0.1"
                or telnet.server_address[0] != "127.0.0.1"):
            raise ValueError("Both local listeners must bind 127.0.0.1")
        paths = (
            Path(http.gateway.engine.db_path).resolve(),
            Path(telnet.engine.db_path).resolve(),
            Path(clock.db_path).resolve(),
        )
        if paths[0] != paths[1] or paths[0] != paths[2]:
            raise ValueError("HTTP, Telnet and clock must share the same world database")
        if type(pulses) is not int or not 1 <= pulses <= 1000:
            raise ValueError("pulses must be an integer in 1..1000")
        if (type(interval_seconds) not in (int, float)
                or not math.isfinite(interval_seconds)
                or not 0.01 <= interval_seconds <= 3600):
            raise ValueError("interval_seconds must be finite in 0.01..3600")
        if type(batch_limit) is not int or not 1 <= batch_limit <= 1000:
            raise ValueError("batch_limit must be an integer in 1..1000")
        if pulses * batch_limit > 100_000:
            raise ValueError("One foreground run may process at most 100000 events")
        if not isinstance(handlers, Mapping) or not handlers or any(
                not isinstance(kind, str) or not kind or not callable(handler)
                for kind, handler in handlers.items()):
            raise ValueError("A nonempty trusted event-kind handler registry is required")
        if (not callable(now) or not callable(monotonic)
                or type(acknowledge_forward_jump) is not bool):
            raise ValueError("Host UTC, pacing and forward-jump controls must be valid")
        if stop_event is not None and not isinstance(stop_event, type(threading.Event())):
            raise TypeError("stop_event must be a threading.Event")
    except BaseException:
        for server in (http, telnet):
            if isinstance(server, (GatewayServer, LocalTelnetServer)):
                server.server_close()
        raise

    stopping = threading.Event()
    failures: list[BaseException] = []

    def interrupted() -> bool:
        if stop_event is not None and stop_event.is_set():
            stopping.set()
        return stopping.is_set()

    def interruptible_wait(seconds: float) -> None:
        # Wake on a caller stop rather than waiting out a potentially long
        # bounded cadence. The internal signal also wakes on listener failure.
        deadline = time.monotonic() + seconds
        while not interrupted():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            stopping.wait(min(remaining, 0.05))

    def serve_requests(server: GatewayServer | LocalTelnetServer) -> None:
        # Unlike serve_forever/shutdown, a bounded handle_request loop cannot
        # race shutdown against a serve_forever thread that has not entered yet.
        server.timeout = 0.05
        try:
            while not interrupted():
                server.handle_request()
        except BaseException as exc:
            failures.append(exc)
            stopping.set()

    threads: list[threading.Thread] = []
    sessions: list[_AcceptedSessions] = []
    try:
        if interrupted():
            return PumpReport(0, (), "stopped")
        sessions = [_AcceptedSessions(http), _AcceptedSessions(telnet)]
        for label, server in (("http", http), ("telnet", telnet)):
            thread = threading.Thread(target=serve_requests, args=(server,),
                                      name=f"REALM-local-{label}")
            thread.start()
            threads.append(thread)
        result = run_foreground(
            clock, handlers, pulses=pulses, interval_seconds=interval_seconds,
            batch_limit=batch_limit, now=now, monotonic=monotonic,
            sleep=interruptible_wait, stop=interrupted,
            acknowledge_forward_jump=acknowledge_forward_jump,
        )
        if failures:
            raise RuntimeError("Local listener stopped unexpectedly") from failures[0]
        return result
    finally:
        stopping.set()
        for thread in threads:
            thread.join(timeout=2)
        for server in (http, telnet):
            server.server_close()
        for custody in sessions:
            custody.quiesce()
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("Local listener did not stop within two seconds")
