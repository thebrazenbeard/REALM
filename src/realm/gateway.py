"""Loopback-only, player-bound bearer HTTP bridge for the REALM prototype.

The v1 wire format is provisional. This is NOT a public multiplayer/auth
service, not an HTTPS endpoint, and not a replacement for Telnet/GMCP support.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .auth import CredentialStore
from .engine import Engine
from .geography import Geography, MapUnavailable
from .router import CommandRouter


MAX_BODY_BYTES = 4096
MAX_FEED_EVENTS = 100


class Gateway:
    """Boundary between untrusted HTTP requests and deterministic engine actions.

    Remote player access requires a host-provisioned credential bound to a
    specific name. Local Engine methods are still host-trusted administrative
    APIs; this is not public account registration or session management.
    """

    def __init__(self, db_path: str | Path, *, world_id: str = "demo"):
        self.engine = Engine(db_path)
        self.geography = Geography(db_path)
        self.router = CommandRouter(self.engine)
        self.credentials = CredentialStore(self.engine)
        self.world_id = world_id

    def _read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.engine.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def cursor(self) -> int:
        db = self._read()
        try:
            return int(db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
        finally:
            db.close()

    def join(self, player: str) -> dict:
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        # The underlying join() validates syntax and preserves the current world.
        text = self.engine.join(player)
        return {"ok": True, "text": text, "cursor": self.cursor()}

    def command(self, player: str, command: str, action_id: str | None) -> dict:
        if not isinstance(player, str) or not isinstance(command, str):
            raise ValueError("Player and command must be strings")
        if len(command) > 512:
            raise ValueError("Command is too long")
        text = self.router.act(player, command, action_id=action_id)
        return {"ok": True, "text": text, "cursor": self.cursor()}

    def room(self, player: str) -> dict:
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        return {"ok": True, "text": self.engine.act(player, "look"), "cursor": self.cursor()}

    def map(self, player: str) -> dict:
        """Map for the joined character's persisted room, if one is authored.

        The HTTP handler must resolve the player from their credential before
        invoking this host-trusted method. Fog-of-war authorization remains
        unimplemented, so this route is not ready for public hosting.
        """
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        db = self._read()
        try:
            row = db.execute("SELECT id,room_id FROM players WHERE id=?", (player,)).fetchone()
            if row is None:
                raise ValueError("Join this REALM before reading the map")
            room_id = row["room_id"]
        finally:
            db.close()
        return {"ok": True, **self.geography.snapshot_for_room(room_id)}

    def feed(self, player: str, after: int) -> dict:
        if not isinstance(player, str) or not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid player or cursor")
        db = self._read()
        try:
            subject = db.execute("SELECT id,room_id FROM players WHERE id=?", (player,)).fetchone()
            if subject is None:
                raise ValueError("Join this REALM before reading the feed")
            # Only events occurring in the player's CURRENT room are returned.
            # Host-trusted player name: the HTTP handler authenticates the
            # principal before reaching this method.
            records = db.execute(
                "SELECT id,occurred_at,actor,room_id,kind,payload_json FROM events "
                "WHERE room_id=? AND id>? ORDER BY id LIMIT ?",
                (subject["room_id"], after, MAX_FEED_EVENTS),
            ).fetchall()
            events = []
            for row in records:
                payload = json.loads(row["payload_json"])
                kind = row["kind"]
                if kind == "say":
                    content = f"{row['actor']}: {payload.get('text','')}"
                elif kind == "join":
                    content = f"{row['actor']} entered the world."
                elif kind == "move":
                    content = f"{row['actor']} arrived."
                elif kind == "leave":
                    content = f"{row['actor']} departed."
                elif kind == "take":
                    content = f"{row['actor']} took {payload.get('item','an item')}."
                elif kind == "drop":
                    content = f"{row['actor']} dropped {payload.get('item','an item')}."
                elif kind == "gather":
                    content = f"{row['actor']} gathered {payload.get('material','resources')} x{payload.get('amount',0)}."
                elif kind in {"craft", "build"}:
                    past = "crafted" if kind == "craft" else "built"
                    content = f"{row['actor']} {past} {payload.get('output','something')}."
                else:
                    content = f"{row['actor']} acted."
                events.append(
                    {"id": row["id"], "kind": kind, "actor": row["actor"],
                     "text": content, "room": row["room_id"], "at": row["occurred_at"]}
                )
            return {"ok": True, "events": events, "cursor": events[-1]["id"] if events else after}
        finally:
            db.close()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gateway: Gateway, allowed_origins: tuple[str, ...], port: int = 8768):
        # No process-wide token or 0.0.0.0 bind option: gameplay uses
        # independent per-player credentials provisioned by the host.
        self.gateway = gateway
        self.allowed_origins = frozenset(allowed_origins)
        super().__init__(("127.0.0.1", port), GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Avoid dumping authorization headers, player messages, and request URLs.
        return

    def _origin(self) -> str | None:
        return self.headers.get("Origin")

    def _origin_ok(self) -> bool:
        origin = self._origin()
        return origin is None or origin in self.server.allowed_origins

    def _headers(self, status: int, payload_length: int) -> None:
        # Some rejected POST requests are denied before their body is read.
        # Never reuse that HTTP/1.1 socket: unread bytes could be parsed as
        # another request, producing an HTML 400 or request desynchronization.
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(payload_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if status >= 400:
            self.send_header("Connection", "close")
        origin = self._origin()
        if origin is not None and origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers(status, len(data))
        self.wfile.write(data)

    def _principal(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        scheme, separator, presented = auth.partition(" ")
        if separator != " " or scheme.casefold() != "bearer":
            return None
        return self.server.gateway.credentials.authenticate(presented)

    def _gate(self, *, public_health: bool = False) -> str | None:
        if not self._origin_ok():
            self._send(403, {"ok": False, "error": "Origin not allowed"})
            return None
        if public_health:
            return ""  # distinguished from no principal (None)
        principal = self._principal()
        if principal is None:
            self._send(401, {"ok": False, "error": "Valid player bearer token required"})
        return principal

    def _player_matches(self, claimed: object, principal: str) -> bool:
        # A caller's player name is merely a backwards-compatible assertion.
        # The verified token, never JSON/query parameters, supplies the actor.
        if claimed is None or (isinstance(claimed, str) and claimed.casefold() == principal.casefold()):
            return True
        self._send(403, {"ok": False, "error": "Player does not match token"})
        return False

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin() or not self._origin_ok():
            self._send(403, {"ok": False, "error": "Origin not allowed"})
            return
        self._headers(204, 0)

    def _params(self) -> tuple[str, dict[str, list[str]]]:
        url = urlsplit(self.path)
        return url.path, parse_qs(url.query)

    def do_GET(self) -> None:  # noqa: N802
        path, params = self._params()
        if path == "/api/v1/health":
            if self._gate(public_health=True) is not None:
                self._send(200, {"ok": True, "world_id": self.server.gateway.world_id})
            return
        principal = self._gate()
        if principal is None:
            return
        try:
            if path == "/api/v1/feed":
                if not self._player_matches(params.get("player", [None])[0], principal):
                    return
                raw_after = params.get("after", ["0"])[0]
                if not raw_after.isdecimal():
                    raise ValueError("Cursor must be a nonnegative integer")
                result = self.server.gateway.feed(principal, int(raw_after))
            elif path == "/api/v1/room":
                if not self._player_matches(params.get("player", [None])[0], principal):
                    return
                result = self.server.gateway.room(principal)
            elif path == "/api/v1/map":
                if not self._player_matches(params.get("player", [None])[0], principal):
                    return
                result = self.server.gateway.map(principal)
            else:
                self._send(404, {"ok": False, "error": "Unknown endpoint"})
                return
        except MapUnavailable as error:
            self._send(404, {"ok": False, "error": str(error)})
            return
        except ValueError as error:
            self._send(400, {"ok": False, "error": str(error)})
            return
        self._send(200, result)

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._params()
        principal = self._gate()
        if principal is None:
            return
        if path not in {"/api/v1/join", "/api/v1/command"}:
            self._send(404, {"ok": False, "error": "Unknown endpoint"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(415, {"ok": False, "error": "Content-Type must be application/json"})
            return
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdecimal() or not 0 <= int(raw_length) <= MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": "Invalid request length"})
            return
        try:
            content = self.rfile.read(int(raw_length))
            obj = json.loads(content)
            if not isinstance(obj, dict):
                raise ValueError("Expected a JSON object")
            if not self._player_matches(obj.get("player"), principal):
                return
            if path == "/api/v1/join":
                result = self.server.gateway.join(principal)
            else:
                result = self.server.gateway.command(
                    principal, obj.get("command"), obj.get("action_id")
                )
        except (ValueError, json.JSONDecodeError) as error:
            self._send(400, {"ok": False, "error": str(error)})
            return
        self._send(200, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REALM local-only JSON gateway (prototype)")
    parser.add_argument("--db", type=Path, default=Path("realm.sqlite3"))
    parser.add_argument("--world-id", default="demo")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--demo-crafting", action="store_true", help="Install optional noncanonical gathering and recipe fixtures")
    parser.add_argument(
        "--allow-origin", action="append", default=[], metavar="ORIGIN",
        help="Exact browser origin permitted by CORS, e.g. http://127.0.0.1:8000",
    )
    operator = parser.add_mutually_exclusive_group()
    operator.add_argument("--provision-player", metavar="PLAYER", help="Provision a player token locally, then exit")
    operator.add_argument("--rotate-player", metavar="PLAYER", help="Rotate a player token locally, then exit")
    operator.add_argument("--revoke-player", metavar="PLAYER", help="Revoke a player token locally, then exit")
    args = parser.parse_args(argv)
    gateway = Gateway(args.db, world_id=args.world_id)
    if args.provision_player:
        print(f"New token for {args.provision_player}: {gateway.credentials.provision(args.provision_player)}")
        return 0
    if args.rotate_player:
        print(f"Rotated token for {args.rotate_player}: {gateway.credentials.rotate(args.rotate_player)}")
        return 0
    if args.revoke_player:
        print("Revoked" if gateway.credentials.revoke(args.revoke_player) else "Not provisioned")
        return 0
    if args.demo_crafting:
        gateway.router.seed_demo_crafting()
    server = GatewayServer(gateway, tuple(args.allow_origin), args.port)
    print(f"REALM gateway listening on http://127.0.0.1:{server.server_port}", flush=True)
    print("Private-host player credentials required. Not an internet-facing game server.", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

