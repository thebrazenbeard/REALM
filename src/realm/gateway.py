"""Loopback-only, bearer-protected HTTP bridge for the local REALM prototype.

The v1 wire format is provisional. This is NOT a public multiplayer/auth
service, not an HTTPS endpoint, and not a replacement for Telnet/GMCP support.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .engine import Engine


MAX_BODY_BYTES = 4096
MAX_FEED_EVENTS = 100


class Gateway:
    """Boundary between untrusted HTTP requests and deterministic engine actions.

    A single gateway token protects the entire local instance. It does NOT
    authenticate individual players; never expose this prototype to the public
    internet or use it as an identity system.
    """

    def __init__(self, db_path: str | Path, *, world_id: str = "demo"):
        self.engine = Engine(db_path)
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
        text = self.engine.act(player, command, action_id=action_id)
        return {"ok": True, "text": text, "cursor": self.cursor()}

    def room(self, player: str) -> dict:
        if not isinstance(player, str):
            raise ValueError("Player must be a string")
        return {"ok": True, "text": self.engine.act(player, "look"), "cursor": self.cursor()}

    def feed(self, player: str, after: int) -> dict:
        if not isinstance(player, str) or not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid player or cursor")
        db = self._read()
        try:
            subject = db.execute("SELECT id,room_id FROM players WHERE id=?", (player,)).fetchone()
            if subject is None:
                raise ValueError("Join this REALM before reading the feed")
            # Only events occurring in the player's CURRENT room are returned.
            # A shared local gateway token is not player-level authentication.
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
                elif kind == "take":
                    content = f"{row['actor']} took {payload.get('item','an item')}."
                elif kind == "drop":
                    content = f"{row['actor']} dropped {payload.get('item','an item')}."
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

    def __init__(self, gateway: Gateway, token: str, allowed_origins: tuple[str, ...], port: int = 8768):
        # Intentionally no 0.0.0.0 bind option in this initial gateway.
        self.gateway = gateway
        self.token = token
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(payload_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
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

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        scheme, separator, presented = auth.partition(" ")
        return (
            separator == " "
            and scheme.casefold() == "bearer"
            and hmac.compare_digest(presented, self.server.token)
        )

    def _gate(self, *, public_health: bool = False) -> bool:
        if not self._origin_ok():
            self._send(403, {"ok": False, "error": "Origin not allowed"})
            return False
        if not public_health and not self._authorized():
            self._send(401, {"ok": False, "error": "Bearer token required"})
            return False
        return True

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
            if self._gate(public_health=True):
                self._send(200, {"ok": True, "world_id": self.server.gateway.world_id})
            return
        if not self._gate():
            return
        try:
            if path == "/api/v1/feed":
                player = params.get("player", [""])[0]
                raw_after = params.get("after", ["0"])[0]
                if not raw_after.isdecimal():
                    raise ValueError("Cursor must be a nonnegative integer")
                result = self.server.gateway.feed(player, int(raw_after))
            elif path == "/api/v1/room":
                result = self.server.gateway.room(params.get("player", [""])[0])
            else:
                self._send(404, {"ok": False, "error": "Unknown endpoint"})
                return
        except ValueError as error:
            self._send(400, {"ok": False, "error": str(error)})
            return
        self._send(200, result)

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._params()
        if not self._gate():
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
            if path == "/api/v1/join":
                result = self.server.gateway.join(obj.get("player"))
            else:
                result = self.server.gateway.command(
                    obj.get("player"), obj.get("command"), obj.get("action_id")
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
    parser.add_argument(
        "--allow-origin", action="append", default=[], metavar="ORIGIN",
        help="Exact browser origin permitted by CORS, e.g. http://127.0.0.1:8000",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("REALM_GATEWAY_TOKEN") or secrets.token_urlsafe(32)
    gateway = Gateway(args.db, world_id=args.world_id)
    server = GatewayServer(gateway, token, tuple(args.allow_origin), args.port)
    print(f"REALM gateway listening on http://127.0.0.1:{server.server_port}", flush=True)
    print("This is a local prototype, not an internet-facing authenticated game server.", flush=True)
    print(f"Bearer token for this process: {token}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
