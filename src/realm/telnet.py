"""Loopback-only Telnet/text transport for REALM's deterministic command core.

This is an experimental LAN-host building block, NOT a public login service.
Telnet is plaintext. The legacy shared access token does not authenticate a
player; hosts may opt into per-player credentials for local identity.
The server intentionally binds 127.0.0.1 only, regardless of CLI arguments.
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import socket
import socketserver
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Callable

from .auth import CredentialStore
from .engine import Engine, _pristine_schema, _schema_snapshot
from .local_presence import LocalPresenceWitness, PresenceDenied, PresenceLease, _WORLD
from .presence_host_binding import bind_host_engine, unbind_host_engine
from .router import CommandRouter


IAC = 255
SB = 250
SE = 240
WILL = 251
WONT = 252
DO = 253
DONT = 254
MAX_LINE_BYTES = 512
MAX_CONNECTIONS = 8
SOCKET_TIMEOUT = 90


class _TelnetFilter:
    """Drop Telnet control bytes; explicitly refuse negotiated options."""

    def __init__(self) -> None:
        self.state = "data"
        self.negotiation = 0

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        body = bytearray()
        reply = bytearray()
        for byte in data:
            if self.state == "data":
                if byte == IAC:
                    self.state = "iac"
                elif byte != 0:
                    body.append(byte)
            elif self.state == "iac":
                if byte == IAC:
                    # Escaped 255 is not part of a supported UTF-8 command.
                    self.state = "data"
                elif byte in (WILL, WONT, DO, DONT):
                    self.negotiation = byte
                    self.state = "option"
                elif byte == SB:
                    self.state = "subneg"
                else:
                    self.state = "data"
            elif self.state == "option":
                if self.negotiation in (WILL, WONT):
                    reply.extend((IAC, DONT, byte))
                else:
                    reply.extend((IAC, WONT, byte))
                self.state = "data"
            elif self.state == "subneg":
                if byte == IAC:
                    self.state = "subneg_iac"
            elif self.state == "subneg_iac":
                if byte == SE:
                    self.state = "data"
                elif byte != IAC:
                    self.state = "subneg"
        return bytes(body), bytes(reply)


class LocalTelnetServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """An authenticated connection gate for one local REALM SQLite instance.

    ``token`` only gates access to the local demonstration server. Players can
    still impersonate one another by entering a known name. Never expose this
    over the internet; public multiplayer needs real identity and TLS/SSH.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, engine: Engine, token: str | None, port: int = 7777,
                 *, player_credentials: bool = False,
                 presence_witness: LocalPresenceWitness | None = None):
        if type(player_credentials) is not bool:
            raise ValueError("player_credentials must be a bool")
        if not player_credentials and (
                not isinstance(token, str) or not token or len(token) > 256):
            raise ValueError("A nonempty server access token is required (max 256 characters)")
        if presence_witness is not None:
            if not isinstance(presence_witness, LocalPresenceWitness) or not player_credentials:
                raise ValueError("Presence requires a real player-bound Telnet credential")
        self.engine = engine
        self.presence_witness = presence_witness
        self._presence_bound = False
        self._presence_lock = threading.Lock()
        self._presence_leases: set[PresenceLease] = set()
        self._presence_closed = False
        self.player_credentials = player_credentials
        self.token = None if player_credentials else token
        self.credentials = CredentialStore(engine) if player_credentials else None
        # Lazy shared router; merely starting a Telnet listener must not
        # implicitly install optional crafting tables.
        self._router: CommandRouter | None = None
        self._router_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._join_lock = threading.Lock()
        self._active_names: set[str] = set()
        super().__init__(("127.0.0.1", port), LocalTelnetHandler)
        try:
            if presence_witness is not None:
                bind_host_engine(engine, presence_witness)
                self._presence_bound = True
        except BaseException:
            super().server_close()
            raise

    @property
    def router(self) -> CommandRouter:
        if self._router is None:
            with self._router_lock:
                if self._router is None:
                    self._router = CommandRouter(self.engine)
        return self._router

    def join_exclusive(self, name: str, *,
                       writer_guard: Callable[[sqlite3.Connection], None] | None = None
                       ) -> str | None:
        """Reserve one *live local socket session* per casefolded player name.

        In legacy mode the shared token permits any valid name. In opt-in
        credential mode the caller's name is fixed by the host-provisioned
        credential, and joining is guarded inside the SQLite writer transaction.
        """
        key = name.casefold()
        with self._join_lock:
            if key in self._active_names:
                return None
            greeting = self.engine.join(name, writer_guard=writer_guard)
            self._active_names.add(key)
            return greeting

    def principal_guard(self, name: str, token: str) -> Callable[[sqlite3.Connection], None]:
        """Validate the credential inside each Engine/Crafting writer transaction.

        The socket precheck alone is insufficient: rotation or revocation
        can commit between it and the world action.
        """
        if not self.player_credentials:
            raise ValueError("Player writer guard needs player_credentials mode")
        digest = CredentialStore._digest(token)
        canonical = name.casefold()

        def guard(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT player_name FROM player_credentials WHERE token_digest=?",
                (digest,),
            ).fetchone()
            if row is None or str(row[0]).casefold() != canonical:
                raise PermissionError("Player credential expired or revoked")

        return guard

    def admit_bound_presence(self, name: str, token: str) -> PresenceLease | None:
        witness = self.presence_witness
        if witness is None:
            return None
        digest = CredentialStore._digest(token)
        def resolver(db: sqlite3.Connection) -> str | None:
            row = db.execute(
                "SELECT player_name FROM main.player_credentials WHERE token_digest=?",
                (digest,),
            ).fetchone()
            return str(row[0]) if row is not None else None
        lease = witness.admit(
            name, protocol="telnet-player-credential", resolve_principal=resolver,
        )
        with self._presence_lock:
            if self._presence_closed:
                lease.close()
                raise PresenceDenied("Telnet host is closing")
            self._presence_leases.add(lease)
        return lease

    def release_presence(self, lease: PresenceLease) -> None:
        with self._presence_lock:
            self._presence_leases.discard(lease)
        lease.close()

    def server_close(self) -> None:
        with self._presence_lock:
            self._presence_closed = True
            leases = tuple(self._presence_leases)
            self._presence_leases.clear()
        for lease in leases:
            lease.close()
        if self._presence_bound:
            unbind_host_engine(self.engine, self.presence_witness)
            self._presence_bound = False
        super().server_close()

    def channel_access_revision(self, player: str) -> int:
        """Read-only response-time proof for host-granted group content."""
        from .channel_roster import channel_access_revision
        with closing(sqlite3.connect(str(self.engine.db_path), timeout=10)) as db:
            return channel_access_revision(db, player)

    def release_name(self, name: str) -> None:
        with self._join_lock:
            self._active_names.discard(name.casefold())

    def is_name_active(self, name: str) -> bool:
        with self._join_lock:
            return name.casefold() in self._active_names

    def verify_request(self, request: socket.socket, client_address: tuple) -> bool:
        if client_address[0] != "127.0.0.1":
            return False
        if self._slots.acquire(blocking=False):
            return True
        try:
            request.sendall(b"REALM is full. Try again later.\r\n")
        except OSError:
            pass
        return False

    def process_request_thread(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class LocalTelnetHandler(socketserver.BaseRequestHandler):
    server: LocalTelnetServer

    def setup(self) -> None:
        self.request.settimeout(SOCKET_TIMEOUT)
        self._filter = _TelnetFilter()
        self._pending = bytearray()
        self._skip_lf = False

    def _send(self, message: str) -> None:
        # ASCII Telnet clients conventionally expect CRLF; errors are replaced
        # instead of allowing a malformed UTF-8 input to terminate the server.
        wire = message.replace("\r\n", "\n").replace("\r", "\n")
        self.request.sendall(wire.replace("\n", "\r\n").encode("utf-8", errors="replace"))

    def _line(self) -> str | None:
        while True:
            for i, byte in enumerate(self._pending):
                if byte in (10, 13):
                    if i > MAX_LINE_BYTES:
                        raise ValueError("Input line exceeds the 512-byte limit")
                    line = bytes(self._pending[:i])
                    delimiter = byte
                    del self._pending[:i + 1]
                    # Telnet CRLF and CR-NUL count as one newline.
                    if delimiter == 13:
                        if self._pending:
                            if self._pending[:1] in (b"\n", b"\x00"):
                                del self._pending[:1]
                            self._skip_lf = False
                        else:
                            self._skip_lf = True
                    return line.decode("utf-8", errors="replace")
            if len(self._pending) > MAX_LINE_BYTES:
                raise ValueError("Input line exceeds the 512-byte limit")
            try:
                chunk = self.request.recv(1024)
            except socket.timeout:
                lease = getattr(self, "_presence_lease", None)
                if lease is None:
                    return None
                try:
                    # A verified, still-owned server socket is sufficient to
                    # renew Telnet presence without requiring users to type.
                    # The lease independently rechecks the stored credential.
                    lease.refresh()
                except PresenceDenied:
                    return None
                continue
            if not chunk:
                return None
            body, reply = self._filter.feed(chunk)
            if reply:
                self.request.sendall(reply)
            if self._skip_lf and body[:1] in (b"\n", b"\x00"):
                body = body[1:]
            self._skip_lf = False
            self._pending.extend(body)

    def handle(self) -> None:
        self._claimed_name: str | None = None
        self._presence_lease: PresenceLease | None = None
        try:
            self._run()
        except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
            return
        except ValueError as error:
            try:
                self._send(f"\nInput rejected: {error}\n")
            except OSError:
                pass
        finally:
            if self._presence_lease is not None:
                self.server.release_presence(self._presence_lease)
            if self._claimed_name is not None:
                self.server.release_name(self._claimed_name)

    def _run(self) -> None:
        if self.server.player_credentials:
            self._send("REALM — player-bound local terminal prototype\nPlayer token: ")
            presented = self._line()
            name = self.server.credentials.authenticate(presented) if presented is not None else None
            if name is None:
                self._send("\nAccess denied.\n")
                return
        else:
            self._send("REALM — local terminal prototype\nToken: ")
            presented = self._line()
            if presented is None or not hmac.compare_digest(presented, self.server.token):
                self._send("\nAccess denied.\n")
                return
            self._send("Name: ")
            name = self._line()
            if name is None:
                return
        if (self.server.player_credentials and
                self.server.credentials.authenticate(presented) != name):
            self._send("Access revoked.\n")
            return
        guard = self.server.principal_guard(name, presented) if self.server.player_credentials else None
        try:
            greeting = self.server.join_exclusive(name, writer_guard=guard)
        except PermissionError:
            self._send("Access revoked.\n")
            return
        except ValueError as error:
            self._send(f"Invalid player name: {error}\n")
            return
        if greeting is None:
            self._send("That player name is already connected to this local server.\n")
            return
        self._claimed_name = name
        if self.server.player_credentials and self.server.presence_witness is not None:
            try:
                self._presence_lease = self.server.admit_bound_presence(name, presented)
            except PresenceDenied:
                self._send("Access revoked or authenticated session witness unavailable.\n")
                return
            # The old 90-second socket idle timeout is not a credential-bound
            # liveness heartbeat. A small server-owned recv timeout periodically
            # checks the actual still-open socket and current credential.
            interval = min(2.0, max(0.2, self.server.presence_witness.idle_seconds / 3))
            self.request.settimeout(interval)
        self._send(f"\n{greeting}\nREALM> ")
        while True:
            command = self._line()
            if command is None:
                return
            if self._presence_lease is not None:
                try:
                    self._presence_lease.refresh()
                except PresenceDenied:
                    self._send("Access revoked.\n")
                    return
            command = command.strip()
            if (self.server.player_credentials and
                    self.server.credentials.authenticate(presented) != name):
                self._send("Access revoked.\n")
                return
            if not command:
                self._send("REALM> ")
                continue
            channel_revision = (
                self.server.channel_access_revision(name)
                if self.server.player_credentials and
                command.partition(" ")[0].casefold() in {"history", "listen", "crewchat"}
                else None
            )
            try:
                if self.server.player_credentials:
                    result = self.server.router.act(name, command, writer_guard=guard)
                else:
                    result = self.server.router.act(
                        name, command, writer_guard=guard, private_channels=False,
                    )
            except PermissionError:
                self._send("Access revoked.\n")
                return
            except ValueError as error:
                result = f"Command rejected: {error}"
            if channel_revision is not None:
                if self.server.credentials.authenticate(presented) != name:
                    self._send("Access revoked.\n")
                    return
                if channel_revision != self.server.channel_access_revision(name):
                    result = "Channel membership changed; retry command."
            self._send(f"{result}\n")
            if command.casefold() in {"quit", "exit"}:
                return
            self._send("REALM> ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REALM loopback-only Telnet prototype")
    parser.add_argument("--db", type=Path, default=Path("realm.sqlite3"))
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--player-credentials", action="store_true",
                        help="Require host-provisioned player tokens, with no client-chosen name")
    parser.add_argument("--enable-local-presence", action="store_true",
                        help="Host-enable credential-bound live-socket PvP presence (no shared token)")
    args = parser.parse_args(argv)
    if args.enable_local_presence and not args.player_credentials:
        parser.error("--enable-local-presence requires --player-credentials")
    token = None if args.player_credentials else (
        os.environ.get("REALM_TELNET_TOKEN") or secrets.token_urlsafe(24)
    )
    world_id = None
    if args.enable_local_presence:
        # Engine() initializes an absent database and seeds rooms. A rejected
        # presence-only CLI command must never create a phantom world merely
        # while checking whether a trusted host bound its identity.
        try:
            with closing(sqlite3.connect(
                    args.db.resolve().as_uri() + "?mode=ro", uri=True,
            )) as db:
                row = db.execute(
                    "SELECT value FROM world_meta WHERE key='world_id'"
                ).fetchone()
                actual_schema = _schema_snapshot(db)
        except sqlite3.Error:
            row = None
            actual_schema = {}
        # A world_id marker and table *names* alone are trivially forgeable:
        # Engine() would then initialize or mutate an incompatible database.
        # Match the existing canonical core constraints/indices read-only,
        # while allowing additional optional worldpack schema objects.
        expected_core = _pristine_schema()
        if (row is None or not isinstance(row[0], str)
                or not _WORLD.fullmatch(row[0])
                or any(actual_schema.get(name) != spec
                       for name, spec in expected_core.items())):
            parser.error("Local presence requires a world already bound by the trusted host")
        world_id = row[0]
    engine = Engine(args.db)
    witness = (
        LocalPresenceWitness(engine, world_id, idle_seconds=10)
        if world_id is not None else None
    )
    server = LocalTelnetServer(
        engine, token, args.port, player_credentials=args.player_credentials,
        presence_witness=witness,
    )
    print(f"REALM local Telnet listening on 127.0.0.1:{server.server_address[1]}", flush=True)
    print("Experimental plaintext LOCAL transport. Do NOT port-forward or internet-expose.", flush=True)
    if args.player_credentials:
        print("Host-provisioned player tokens required (no network registration).", flush=True)
    else:
        print("Legacy shared token is NOT a player identity.", flush=True)
        print(f"Connection token: {token}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
