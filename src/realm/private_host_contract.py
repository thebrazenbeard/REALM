"""Pure, offline *candidate* contract for a future private REALM ingress.

NOT wired to GatewayServer, not a TLS implementation, not an invitation issuer,
not an authorization service. Input labelled ``edge``/``quota`` MUST originate
from a trusted host adapter, never raw client headers or query parameters.
The function performs no I/O, credentials writes, socket binding or clock read.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re
from typing import Mapping
from urllib.parse import urlsplit


_SPOOFED_HEADERS = frozenset({
    "forwarded", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-real-ip", "x-tailscale-user-login",
    "x-tailscale-user-name", "x-tailscale-user-profile-pic",
    "remote-user", "x-auth-request-user",
})
_SCOPES = frozenset({"read", "write"})
_WORLD_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _is_https_origin(value: object) -> bool:
    if not isinstance(value, str) or value.endswith("/"):
        return False
    try:
        url = urlsplit(value)
        return bool(
            url.scheme == "https" and url.hostname and url.netloc
            and not url.username and not url.password and not url.path
            and not url.query and not url.fragment
        )
    except ValueError:
        return False


class AdmissionDenied(PermissionError):
    """Safe external denial; reasons are for trusted local test/log code only."""

    def __init__(self, internal_reason: str) -> None:
        super().__init__("Private REALM admission denied")
        self.internal_reason = internal_reason


@dataclass(frozen=True)
class PrivateHostPolicy:
    """Operator-pinned host/world values, not values selected by a player."""

    host_id: str
    world_id: str
    https_origin: str
    allowed_browser_origins: frozenset[str]
    max_request_bytes: int = 4096
    max_header_bytes: int = 8192
    max_requests_per_window: int = 60

    def __post_init__(self) -> None:
        if (not isinstance(self.host_id, str) or not self.host_id
                or not isinstance(self.world_id, str)
                or not _WORLD_ID.fullmatch(self.world_id)
                or not _is_https_origin(self.https_origin)):
            raise ValueError("Private host requires an exact HTTPS origin and host/world IDs")
        if (not isinstance(self.allowed_browser_origins, frozenset)
                or any(not _is_https_origin(x) for x in self.allowed_browser_origins)):
            raise ValueError("Browser origins must be an explicit HTTPS allowlist")
        if min(self.max_request_bytes, self.max_header_bytes, self.max_requests_per_window) <= 0:
            raise ValueError("Admission limits must be positive")


@dataclass(frozen=True)
class InvitationGrant:
    """Synthetic/host-held grant *metadata*; no token issuance is implemented."""

    host_id: str
    world_id: str
    player: str
    token_sha256: str
    expires_unix: int
    scopes: frozenset[str]
    revoked: bool = False


@dataclass(frozen=True)
class TrustedEdgeEvidence:
    """Only a future authenticated local edge may assemble this evidence.

    This dataclass is NOT a cryptographic attestation: an untrusted caller can
    construct it. Never accept the object via network JSON/HTTP directly.
    """

    tls_verified: bool
    adapter_authenticated: bool
    host_id: str
    https_origin: str
    client_supplied_headers: Mapping[str, str]


@dataclass(frozen=True)
class RequestMetadata:
    """Parsed request values plus *host-measured* size/quota counters."""

    world_id: str
    authorization: str
    method: str
    operation: str
    origin: str | None
    asserted_player: str | None
    body_bytes: int
    header_bytes: int
    requests_in_window: int


@dataclass(frozen=True)
class AdmittedPlayer:
    host_id: str
    world_id: str
    player: str
    operation: str


def evaluate_private_admission(
    policy: PrivateHostPolicy, grant: InvitationGrant,
    edge: TrustedEdgeEvidence, request: RequestMetadata, *, now_unix: int,
) -> AdmittedPlayer:
    """Fail-closed **contract oracle**, not currently used by REALM gateway.

    Host configuration, grant storage, transport verification, token delivery,
    clock, rate counters and request-size measurement are outside this oracle.
    Its success must never be cited as proof those dependencies actually exist.
    """
    # Strings such as "false" and the integer 1 are NOT edge proof. The only
    # accepted values are actual bool True from a trusted host-side adapter.
    if edge.tls_verified is not True or edge.adapter_authenticated is not True:
        raise AdmissionDenied("unverified-edge")
    if edge.host_id != policy.host_id or edge.https_origin != policy.https_origin:
        raise AdmissionDenied("wrong-host-identity")
    if (request.world_id != policy.world_id or grant.world_id != policy.world_id
            or grant.host_id != policy.host_id):
        raise AdmissionDenied("wrong-world-or-host-grant")
    if not isinstance(grant.player, str) or not grant.player:
        raise AdmissionDenied("invalid-host-grant")
    if (not isinstance(request.authorization, str)
            or not isinstance(request.method, str)
            or not isinstance(request.operation, str)
            or (request.origin is not None and not isinstance(request.origin, str))
            or (request.asserted_player is not None
                and not isinstance(request.asserted_player, str))):
        raise AdmissionDenied("malformed-request")
    if (not isinstance(edge.client_supplied_headers, Mapping)
            or any(not isinstance(key, str)
                   or not _HEADER_NAME.fullmatch(key)
                   or key.casefold() in _SPOOFED_HEADERS
                   or key.casefold().startswith(("x-forwarded-", "x-tailscale-", "x-original-"))
                   for key in edge.client_supplied_headers)):
        raise AdmissionDenied("untrusted-forwarded-identity")
    if (request.origin is not None
            and request.origin not in policy.allowed_browser_origins):
        raise AdmissionDenied("origin-not-allowed")
    if (request.method not in {"GET", "POST"}
            or (request.operation == "read" and request.method != "GET")
            or (request.operation == "write" and request.method != "POST")
            or request.operation not in _SCOPES
            or not isinstance(grant.scopes, frozenset)
            or not grant.scopes <= _SCOPES
            or request.operation not in grant.scopes):
        raise AdmissionDenied("method-or-scope-not-allowed")
    if (not isinstance(request.body_bytes, int) or request.body_bytes < 0
            or request.body_bytes > policy.max_request_bytes
            or (request.method == "GET" and request.body_bytes != 0)
            or not isinstance(request.header_bytes, int) or request.header_bytes < 0
            or request.header_bytes > policy.max_header_bytes):
        raise AdmissionDenied("request-size-limit")
    if (not isinstance(request.requests_in_window, int)
            or request.requests_in_window < 0
            or request.requests_in_window >= policy.max_requests_per_window):
        raise AdmissionDenied("rate-limit")
    if (grant.revoked or not isinstance(now_unix, int)
            or not isinstance(grant.expires_unix, int)
            or now_unix >= grant.expires_unix):
        raise AdmissionDenied("revoked-or-expired")
    if (request.asserted_player is not None
            and request.asserted_player.casefold() != grant.player.casefold()):
        raise AdmissionDenied("actor-mismatch")
    scheme, sep, token = request.authorization.partition(" ")
    if (not sep or scheme.casefold() != "bearer" or not token.isascii()
            or not 16 <= len(token) <= 255
            or not isinstance(grant.token_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", grant.token_sha256)):
        raise AdmissionDenied("invalid-token-shape")
    try:
        candidate = hashlib.sha256(token.encode("ascii")).hexdigest()
        if not hmac.compare_digest(candidate, grant.token_sha256):
            raise AdmissionDenied("token-mismatch")
    except (UnicodeError, TypeError) as exc:
        raise AdmissionDenied("invalid-token") from exc
    return AdmittedPlayer(policy.host_id, policy.world_id, grant.player, request.operation)
