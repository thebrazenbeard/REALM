"""Offline HMAC-bound private-edge experiment, not a TLS or network service.

This is an independently checked, host/world/player-bound *in-process* adapter
around REALM's existing Gateway. It opens NO socket, issues NO real player
credentials and does NOT establish that an HTTP reverse proxy is trusted.

Signer and backend must eventually run in independently authenticated operator
components with protected short-lived keys. Until then, this is a testable
cryptographic request-integrity and replay mechanism only.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets
from typing import Mapping
from urllib.parse import urlsplit

from .engine import _PLAYER_RE
from .gateway import Gateway
from .private_host_contract import AdmissionDenied, PrivateHostPolicy


_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_DENY_PREFIX = ("x-forwarded-", "x-tailscale-", "x-original-")
_DENY_NAMES = frozenset({"forwarded", "x-real-ip", "remote-user", "x-auth-request-user",
                        "connection", "transfer-encoding", "proxy-authorization"})
_AUDIENCE = "realm.signed-private-edge.v1"
_MAX_SIGNATURE_LIFETIME = 15  # seconds; ephemeral keys/nonce cache not persistent
_MAX_INVITE_LIFETIME = 300  # seconds; no refresh/recovery service
_MAX_ACTIVE_INVITATIONS = 1024
_MAX_REPLAY_NONCES = 4096


@dataclass(frozen=True)
class LocalRequest:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes = b""


@dataclass(frozen=True)
class SignedForwarding:
    claims: Mapping[str, object]
    signature: str


def _canonical_headers(request: LocalRequest, policy: PrivateHostPolicy) -> dict[str, str]:
    if (not isinstance(request.method, str) or not isinstance(request.path, str)
            or not isinstance(request.body, bytes)
            or not isinstance(request.headers, tuple)):
        raise AdmissionDenied("invalid-request-shape")
    if (len(request.method) > 8 or len(request.path) > 256
            or any(ord(char) < 32 or ord(char) > 126 for char in request.path)):
        raise AdmissionDenied("request-line-limit")
    headers: dict[str, str] = {}
    size = 0
    for entry in request.headers:
        if (not isinstance(entry, tuple) or len(entry) != 2
                or not isinstance(entry[0], str) or not isinstance(entry[1], str)):
            raise AdmissionDenied("invalid-header")
        key, value = entry
        # Refuse whitespace/underscore aliases instead of stripping into a
        # sometimes-trusted header. Downstream proxies MUST do the same.
        if not _HEADER_NAME.fullmatch(key) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise AdmissionDenied("ambiguous-header")
        name = key.casefold()
        if name in headers or name in _DENY_NAMES or name.startswith(_DENY_PREFIX):
            raise AdmissionDenied("duplicate-or-forwarded-identity")
        try:
            size += len(key.encode("ascii")) + len(value.encode("utf-8")) + 4
        except UnicodeError as exc:
            raise AdmissionDenied("invalid-header-encoding") from exc
        if size > policy.max_header_bytes:
            raise AdmissionDenied("header-limit")
        headers[name] = value
    if len(request.body) > policy.max_request_bytes:
        raise AdmissionDenied("body-limit")
    # Host is still a client-controlled header, not TLS/SNI attestation. It
    # must at least equal the operator-pinned expected authority or fail.
    if headers.get("host") != urlsplit(policy.https_origin).netloc:
        raise AdmissionDenied("host-authority-mismatch")
    return headers


def _request_digest(request: LocalRequest, headers: dict[str, str]) -> str:
    raw = json.dumps({
        "method": request.method, "path": request.path,
        "headers": sorted(headers.items()),
        "body_sha256": hashlib.sha256(request.body).hexdigest(),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(b"REALM exact forwarded request v1\0" + raw).hexdigest()


def _operation(request: LocalRequest) -> str:
    # Deliberately exclude /feed and /map: current Gateway.feed returns old
    # room events to a newly arrived actor; there is no event-time grant.
    if request.method == "GET" and request.path == "/api/v1/room":
        return "read"
    if request.method == "POST" and request.path in {"/api/v1/join", "/api/v1/command"}:
        return "write"
    raise AdmissionDenied("route-unqualified")


@dataclass(frozen=True)
class _Invitation:
    player: str
    digest: str
    expires: int
    scopes: frozenset[str]


class EphemeralInvitations:
    """Operator-only test store; high-entropy in-memory grants, no persistence."""

    def __init__(self, *, host_id: str, world_id: str) -> None:
        self.host_id, self.world_id = host_id, world_id
        self._by_player: dict[str, _Invitation] = {}

    def issue(self, player: str, *, now: int, lifetime: int = 120,
              scopes: frozenset[str] = frozenset({"read", "write"})) -> str:
        if (not isinstance(player, str) or not _PLAYER_RE.fullmatch(player)
                or not isinstance(now, int) or not 1 <= lifetime <= _MAX_INVITE_LIFETIME
                or not isinstance(scopes, frozenset)
                or not scopes or not scopes <= frozenset({"read", "write"})):
            raise ValueError("Invalid operator-only test invitation")
        if player.casefold() in self._by_player:
            raise ValueError("Test invitation already exists; use explicit rotation")
        if len(self._by_player) >= _MAX_ACTIVE_INVITATIONS:
            raise ValueError("Too many operator-only test invitations")
        return self._replace(player, now=now, lifetime=lifetime, scopes=scopes)

    def _replace(self, player: str, *, now: int, lifetime: int,
                 scopes: frozenset[str]) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        self._by_player[player.casefold()] = _Invitation(player, digest, now + lifetime, scopes)
        return token

    def rotate(self, player: str, *, now: int, lifetime: int = 120) -> str:
        if not isinstance(player, str) or player.casefold() not in self._by_player:
            raise ValueError("No such test invitation")
        if not isinstance(now, int) or not 1 <= lifetime <= _MAX_INVITE_LIFETIME:
            raise ValueError("Invalid operator-only test invitation lifetime")
        old = self._by_player[player.casefold()]
        return self._replace(old.player, now=now, lifetime=lifetime, scopes=old.scopes)

    def revoke(self, player: str) -> None:
        if not isinstance(player, str) or player.casefold() not in self._by_player:
            raise ValueError("No such test invitation")
        del self._by_player[player.casefold()]

    def authenticate(self, token: str, *, now: int, scope: str) -> _Invitation:
        if not isinstance(token, str) or not 16 <= len(token) <= 255 or not token.isascii():
            raise AdmissionDenied("invalid-invitation")
        candidate = hashlib.sha256(token.encode("ascii")).hexdigest()
        found = None
        # Scan all fixed-length digests with compare_digest. Do not use
        # caller-provided player name as a lookup/authority shortcut.
        for grant in self._by_player.values():
            if hmac.compare_digest(candidate, grant.digest):
                found = grant
        if found is None or now >= found.expires or scope not in found.scopes:
            raise AdmissionDenied("invalid-or-expired-invitation")
        return found


class LocalEdgeSigner:
    """Reference trusted-process signer; no TLS/peer identity claim is made."""

    def __init__(self, policy: PrivateHostPolicy, store: EphemeralInvitations,
                 *, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("Edge signing key must contain at least 32 bytes")
        if (store.host_id, store.world_id) != (policy.host_id, policy.world_id):
            raise ValueError("Invitation store bound to another host/world")
        self._policy, self._store, self._key = policy, store, key
        self._window_counts: dict[tuple[str, int], int] = {}

    def sign(self, request: LocalRequest, *, now: int) -> SignedForwarding:
        if not isinstance(now, int):
            raise AdmissionDenied("invalid-host-clock")
        headers = _canonical_headers(request, self._policy)
        scope = _operation(request)
        origin = headers.get("origin")
        if origin is not None and origin not in self._policy.allowed_browser_origins:
            raise AdmissionDenied("origin-denied")
        authorization = headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not separator:
            raise AdmissionDenied("missing-invitation")
        grant = self._store.authenticate(token, now=now, scope=scope)
        window = now // 60
        self._window_counts = {
            bucket: count for bucket, count in self._window_counts.items()
            if bucket[1] >= window - 1
        }
        key = (grant.player.casefold(), window)
        count = self._window_counts.get(key, 0)
        if count >= self._policy.max_requests_per_window:
            raise AdmissionDenied("rate-limit")
        self._window_counts[key] = count + 1
        claims: dict[str, object] = {
            "aud": _AUDIENCE,
            "host": self._policy.host_id, "world": self._policy.world_id,
            "player": grant.player, "scope": scope,
            "digest": _request_digest(request, headers),
            "nonce": secrets.token_hex(16), "issued": now,
            "expires": min(grant.expires, now + _MAX_SIGNATURE_LIFETIME),
        }
        signed = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._key, b"REALM signed edge v1\0" + signed, hashlib.sha256).hexdigest()
        return SignedForwarding(claims, signature)


class LocalSignedBackend:
    """Verifies HMAC before dispatch to the authoritative *local* Gateway."""

    def __init__(self, gateway: Gateway, policy: PrivateHostPolicy,
                 invitations: EphemeralInvitations, *, key: bytes) -> None:
        if gateway.world_id != policy.world_id:
            raise ValueError("Gateway database belongs to another world")
        if (invitations.host_id, invitations.world_id) != (policy.host_id, policy.world_id):
            raise ValueError("Invitation store bound to another host/world")
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("Backend signing key must contain at least 32 bytes")
        self._gateway, self._policy, self._invitations, self._key = gateway, policy, invitations, key
        self._used_nonces: dict[str, int] = {}  # short-lived and bounded within one key lifetime

    def dispatch(self, request: LocalRequest, signed: SignedForwarding, *, now: int) -> dict:
        headers = _canonical_headers(request, self._policy)
        scope = _operation(request)
        claims = signed.claims
        expected_keys = {"aud", "host", "world", "player", "scope", "digest", "nonce", "issued", "expires"}
        if not isinstance(claims, Mapping) or set(claims) != expected_keys:
            raise AdmissionDenied("invalid-signed-claims")
        if (claims["aud"] != _AUDIENCE or claims["host"] != self._policy.host_id
                or claims["world"] != self._policy.world_id
                or claims["scope"] != scope
                or claims["digest"] != _request_digest(request, headers)
                or not isinstance(claims["player"], str)
                or not _PLAYER_RE.fullmatch(claims["player"])
                or not isinstance(claims["nonce"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", claims["nonce"])
                or not isinstance(claims["issued"], int)
                or not isinstance(claims["expires"], int)
                or not isinstance(now, int)
                or not claims["issued"] <= now < claims["expires"]
                or not 0 < claims["expires"] - claims["issued"] <= _MAX_SIGNATURE_LIFETIME):
            raise AdmissionDenied("wrong-or-stale-signed-claims")
        if not isinstance(signed.signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signed.signature):
            raise AdmissionDenied("bad-signature-shape")
        canonical = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(self._key, b"REALM signed edge v1\0" + canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signed.signature):
            raise AdmissionDenied("invalid-signature")
        # A still-signed envelope is not permission to use a rotated/revoked
        # invitation. Recheck the host's active grant on every dispatch.
        auth = headers.get("authorization", "")
        scheme, sep, token = auth.partition(" ")
        if not sep or scheme.casefold() != "bearer":
            raise AdmissionDenied("missing-invitation")
        active = self._invitations.authenticate(token, now=now, scope=scope)
        if active.player != claims["player"]:
            raise AdmissionDenied("signed-actor-no-longer-authorized")
        nonce = claims["nonce"]
        self._used_nonces = {
            key: expiry for key, expiry in self._used_nonces.items()
            if expiry > now
        }
        if nonce in self._used_nonces:
            raise AdmissionDenied("replayed-forwarding")
        if len(self._used_nonces) >= _MAX_REPLAY_NONCES:
            raise AdmissionDenied("replay-cache-full")
        # Must mark used BEFORE any Engine action so a repeated envelope cannot
        # create a second effect even if a subsequent command throws.
        self._used_nonces[nonce] = claims["expires"]
        actor = claims["player"]
        if request.path == "/api/v1/join":
            obj = _json_object(request.body)
            if obj.get("player") not in (None, actor):
                raise AdmissionDenied("actor-mismatch")
            return _scoped_text_reply(self._gateway.join(actor))
        if request.path == "/api/v1/room":
            if request.body:
                raise AdmissionDenied("body-on-read")
            return _scoped_text_reply(self._gateway.room(actor))
        if request.path == "/api/v1/command":
            obj = _json_object(request.body)
            if obj.get("player") not in (None, actor):
                raise AdmissionDenied("actor-mismatch")
            command = obj.get("command")
            action_id = obj.get("action_id")
            # A process-local nonce cache is not durable across backend
            # restart/workers. Every mutating command MUST carry a stable
            # Engine receipt ID so a replayed signed envelope cannot repeat
            # an already committed world effect after nonce-cache loss.
            if (not isinstance(action_id, str)
                    or not 1 <= len(action_id) <= 128
                    or action_id != action_id.strip()
                    or not action_id.isascii()
                    or not all(32 <= ord(c) <= 126 for c in action_id)):
                raise AdmissionDenied("durable-action-id-required")
            # Until event-time visibility grants exist, allow only narrow
            # commands; `history`/`listen` and event feed leak old room speech.
            if not isinstance(command, str) or not (command == "look" or
                    (command.startswith("say ") and 1 <= len(command[4:]) <= 280
                     and command[4:] == command[4:].strip()
                     and all(char.isprintable() for char in command[4:]))):
                raise AdmissionDenied("command-not-qualified")
            return _scoped_text_reply(self._gateway.command(actor, command, action_id))
        raise AdmissionDenied("route-unqualified")


def _json_object(body: bytes) -> dict:
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionDenied("invalid-json") from exc
    if not isinstance(obj, dict):
        raise AdmissionDenied("invalid-json-shape")
    return obj


def _scoped_text_reply(reply: dict) -> dict:
    """Do not leak Gateway's world-global event cursor to a scoped client.

    Event-time visibility and a per-audience cursor are not yet implemented.
    This bounded adapter does not offer feeds, so returning global MAX(id)
    would reveal off-room activity without any gameplay benefit.
    """
    return {"ok": bool(reply["ok"]), "text": reply["text"]}
