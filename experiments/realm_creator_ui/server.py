"""Loopback-only, memory-only Creator preview and *unimported* compilation.

This deliberately does not reuse the player Gateway's bearer credentials.
An ephemeral console token gates operator requests, but does not establish
canonical identity or authority to import/publish a world.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from experiments.realm_creator import CreatorDraftError, compile_approved_worldpack, prepare_draft
from experiments.realm_creator_generation import RegionProposalError, propose_region
from experiments.realm_creator_ui.existing_pack import preview_existing_worldpack
from realm.worldpack import MAX_SOURCE_BYTES

MAX_BODY_BYTES = 1536 * 1024
MAX_DRAIN_BYTES = 2 * 1024 * 1024
_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}\.(?:txt|md|markdown)$")
_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}
_FORBIDDEN_UNICODE = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


class _ClientError(ValueError):
    def __init__(self, status: int):
        self.status = status
        super().__init__("Rejected Creator request")


def _unique_body_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _ClientError(400)
        result[key] = value
    return result


def _file_sources(records: object) -> dict[str, bytes]:
    if not isinstance(records, list) or not 1 <= len(records) <= 8:
        raise _ClientError(400)
    result: dict[str, bytes] = {}
    aliases: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"name", "content_base64"}:
            raise _ClientError(400)
        name, encoded = record["name"], record["content_base64"]
        if not isinstance(name, str) or not _BASENAME.fullmatch(name):
            raise _ClientError(400)
        stem = name.rsplit(".", 1)[0].rstrip(" .").upper()
        alias = name.casefold()
        if stem in _RESERVED_STEMS or alias in aliases:
            raise _ClientError(400)
        aliases.add(alias)
        if not isinstance(encoded, str):
            raise _ClientError(400)
        try:
            data = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError, base64.binascii.Error) as exc:
            raise _ClientError(400) from exc
        result[name] = data
    return result


def _existing_pack_response(data: dict) -> dict:
    """Read-only preview of an already-authored, *unapproved* worldpack."""
    if set(data) != {"source_id", "worldpack_base64"}:
        raise _ClientError(400)
    encoded = data["worldpack_base64"]
    if not isinstance(encoded, str):
        raise _ClientError(400)
    if len(encoded) > 4 * ((MAX_SOURCE_BYTES + 2) // 3):
        raise _ClientError(413)
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError, base64.binascii.Error) as exc:
        raise _ClientError(400) from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise _ClientError(413)
    preview = preview_existing_worldpack(raw, {"source_id": data["source_id"]})
    pack = preview["worldpack"]
    return {
        "preview": preview,
        "counts": {
            "rooms": len(pack["rooms"]),
            "items": len(pack["items"]),
            "directed_exits": sum(len(room["exits"]) for room in pack["rooms"]),
        },
    }


def _worldpack_json(compiled: dict) -> str:
    pack = compiled["worldpack"]

    def visit(value: object) -> None:
        if isinstance(value, str) and any(
            char != "\n" and unicodedata.category(char) in _FORBIDDEN_UNICODE
            for char in value
        ):
            raise _ClientError(400)
        if isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(pack)
    text = json.dumps(pack, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":"))
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise _ClientError(413)
    if hashlib.sha256(encoded).hexdigest() != compiled["worldpack_sha256"]:
        raise _ClientError(400)
    return text


class CreatorHandler(BaseHTTPRequestHandler):
    server_version = "REALMCreatorPreview"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, _format, *_args):
        # Never log manuscript excerpts, credentials, metadata or request paths.
        pass

    def _headers(self, status: int, content_type: str, size: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                         "form-action 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _allowed_host(self) -> bool:
        headers = self.headers.get_all("Host", [])
        if len(headers) != 1:
            return False
        port = self.server.server_address[1]
        return headers[0] in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _allowed_origin(self) -> bool:
        origin = self.headers.get_all("Origin", [])
        host = self.headers.get_all("Host", [])
        return len(origin) == 1 and len(host) == 1 and origin[0] == f"http://{host[0]}"

    def _allowed_token(self) -> bool:
        provided = self.headers.get_all("X-REALM-CREATOR-TOKEN", [])
        return (len(provided) == 1 and isinstance(provided[0], str) and
                provided[0].isascii() and
                hmac.compare_digest(provided[0], self.server.creator_token))

    def do_GET(self) -> None:
        if not self._allowed_host():
            self._json(403, {"error": "Forbidden"})
            return
        resource = _STATIC_ASSETS.get(self.path)
        if resource is None:
            self._json(404, {"error": "Not found"})
            return
        file_name, mime = resource
        try:
            body = (_STATIC_ROOT / file_name).read_bytes()
        except OSError:
            self._json(404, {"error": "Not found"})
            return
        self._headers(200, mime, len(body))
        self.wfile.write(body)

    def _body(self) -> dict:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _ClientError(400)
        sizes = self.headers.get_all("Content-Length", [])
        if len(sizes) != 1 or not sizes[0].isdigit() or len(sizes[0]) > 12:
            raise _ClientError(411)
        size = int(sizes[0])
        if not 1 <= size <= MAX_BODY_BYTES:
            if size <= MAX_DRAIN_BYTES:
                self.rfile.read(size)
            raise _ClientError(413)
        if self.headers.get("Content-Type", "").strip().lower() != "application/json":
            self.rfile.read(size)
            raise _ClientError(415)
        self.connection.settimeout(8)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise _ClientError(400)
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_body_keys)
        except _ClientError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise _ClientError(400) from exc
        if not isinstance(data, dict):
            raise _ClientError(400)
        return data

    def _drain_rejected_body(self) -> None:
        """Avoid Windows TCP reset on short rejected POSTs; never drain unbounded input."""
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 12:
            return
        size = int(lengths[0])
        if not 0 <= size <= MAX_DRAIN_BYTES:
            return
        try:
            self.connection.settimeout(2)
            self.rfile.read(size)
        except OSError:
            pass

    def do_POST(self) -> None:
        if not self._allowed_host() or not self._allowed_origin() or not self._allowed_token():
            self._drain_rejected_body()
            self._json(403, {"error": "Forbidden"})
            return
        if self.path not in {"/api/v1/creator/preview", "/api/v1/creator/compile",
                             "/api/v1/creator/existing-preview"}:
            self._json(404, {"error": "Not found"})
            return
        try:
            data = self._body()
            if self.path == "/api/v1/creator/existing-preview":
                self._json(200, _existing_pack_response(data))
                return
            expected = {"world_id", "name", "sources"}
            compile_request = self.path.endswith("/compile")
            if compile_request:
                expected |= {"draft", "approval"}
            if set(data) != expected:
                raise _ClientError(400)
            sources = _file_sources(data["sources"])
            draft = prepare_draft(data["world_id"], data["name"], sources)
            # This verifies the exact source bytes, including source heading
            # provenance, before any suggestion is treated as a candidate.
            region = propose_region(draft, sources)
            if not compile_request:
                self._json(200, {"draft": draft, "region": region})
                return
            if data["draft"] != draft:
                raise _ClientError(400)
            compiled = compile_approved_worldpack(draft, data["approval"])
            pack_json = _worldpack_json(compiled)
            self._json(200, {"compiled": compiled, "worldpack_json": pack_json})
        except _ClientError as exc:
            self._json(exc.status, {"error": "Creator request rejected"})
        except (CreatorDraftError, RegionProposalError, TypeError, ValueError,
                OverflowError, UnicodeError, RecursionError) as _exc:
            self._json(400, {"error": "Creator source or review rejected"})


def make_server(port: int = 8771, token: str | None = None) -> HTTPServer:
    """Construct an ephemeral local operator process; never start a world."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid loopback port")
    if token is None:
        token = secrets.token_urlsafe(32)
    if not isinstance(token, str) or not token:
        raise ValueError("Operator token must not be empty")
    server = HTTPServer(("127.0.0.1", port), CreatorHandler)
    server.creator_token = token
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Loopback-only REALM Creator preview; no world imports")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    server = make_server(port=args.port)
    print(f"REALM Creator local preview: http://127.0.0.1:{server.server_address[1]}/", flush=True)
    print(f"One-session operator token (never share): {server.creator_token}", flush=True)
    print("Preview/compile only. No approval authenticated; no world imported.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
