"""Loopback-only Telnet/text transport for REALM's deterministic command core.

This is an experimental LAN-host building block, NOT a public login service.
Telnet is plaintext and a shared access token does not authenticate a player.
The server intentionally binds 127.0.0.1 only, regardless of CLI arguments.
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import socket
import socketserver
import threading
from pathlib import Path

from .engine import Engine


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

    def __init__(self, engine: Engine, token: str, port: int = 7777):
        if not token or len(token) > 256:
            raise ValueError("A nonempty server access token is required (max 256 characters)")
        self.engine = engine
        self.token = token
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._join_lock = threading.Lock()
        self._active_names: set[str] = set()
        super().__init__(("127.0.0.1", port), LocalTelnetHandler)

    def join_exclusive(self, name: str) -> str | None:
        """Reserve one *live local socket session* per casefolded player name.

        This is not durable identity/ownership: someone with the shared local
        access token can still claim the name after its socket disconnects.
        """
        key = name.casefold()
        with self._join_lock:
            if key in self._active_names:
                return None
            greeting = self.engine.join(name)
            self._active_names.add(key)
            return greeting

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
                return None
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
            if self._claimed_name is not None:
                self.server.release_name(self._claimed_name)

    def _run(self) -> None:
        self._send("REALM — local terminal prototype\nToken: ")
        presented = self._line()
        if presented is None or not hmac.compare_digest(presented, self.server.token):
            self._send("\nAccess denied.\n")
            return
        self._send("Name: ")
        name = self._line()
        if name is None:
            return
        try:
            greeting = self.server.join_exclusive(name)
        except ValueError as error:
            self._send(f"Invalid player name: {error}\n")
            return
        if greeting is None:
            self._send("That player name is already connected to this local server.\n")
            return
        self._claimed_name = name
        self._send(f"\n{greeting}\nREALM> ")
        while True:
            command = self._line()
            if command is None:
                return
            command = command.strip()
            if not command:
                self._send("REALM> ")
                continue
            try:
                result = self.server.engine.act(name, command)
            except ValueError as error:
                result = f"Command rejected: {error}"
            self._send(f"{result}\n")
            if command.casefold() in {"quit", "exit"}:
                return
            self._send("REALM> ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REALM loopback-only Telnet prototype")
    parser.add_argument("--db", type=Path, default=Path("realm.sqlite3"))
    parser.add_argument("--port", type=int, default=7777)
    args = parser.parse_args(argv)
    token = os.environ.get("REALM_TELNET_TOKEN") or secrets.token_urlsafe(24)
    server = LocalTelnetServer(Engine(args.db), token, args.port)
    print(f"REALM local Telnet listening on 127.0.0.1:{server.server_address[1]}", flush=True)
    print("Experimental plaintext LOCAL transport. Do NOT port-forward or internet-expose.", flush=True)
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
