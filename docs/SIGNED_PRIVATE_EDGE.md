# Signed private-edge fixture — in-process integration, not TLS deployment

**Status:** an actual host-owned HMAC request-integrity and gameplay-dispatch
experiment, executed **in process** against REALM's SQLite `Gateway`.
No network listener, HTTPS reverse proxy, local socket authorization, device
identity, Windows ACL or real multiplayer transport is configured or qualified.

This is a narrower milestone than Issue #21's remote invited multiplayer.
The old `private_host_contract.py` evaluator accepted caller-provided
`TrustedEdgeEvidence` booleans. A model or hostile client could construct a
positive object; it is **not attestation**. This branch also corrects two
independently reproduced fail-open cases in that old oracle: string values
`"false"` previously passed truthiness checks, and underscore/whitespace
aliases of forwarded identity headers bypassed its prefix blacklist.

## What this experiment implements

`src/realm/signed_private_edge.py` adds **three separate roles**:

1. `EphemeralInvitations`: trusted-operator-only memory store; random
   256-bit bearer per synthetic player, only SHA-256 digest retained, a maximum
   300-second lifetime, explicit rotation/revocation, and constant-time digest
   comparison while checking all current records. Store is lost on restart;
   it is **not** a durable production account store. Never invoke it through
   an untrusted network route.
2. `LocalEdgeSigner`: candidate authenticated edge component. It accepts a
   local `LocalRequest`, rejects ambiguous/duplicate/forwarding identity
   headers (including `X_Forwarded_For` and ` X-Forwarded-For `), requires the
   client `Host` header to match the pinned HTTPS authority (without treating
   that header as TLS proof), measures body and header bytes, applies an
   in-memory per-player window budget, resolves
   the invitation to a canonical actor, then HMAC-SHA256 signs an envelope.
   Claims bind exact host ID, world ID, actor, operation, method/path/all
   canonical headers/body hash, nonce and lifetime up to 15 seconds.
3. `LocalSignedBackend`: independently recomputes the HMAC, exact request
   digest, host/world, nonce and expiry before any `Gateway` call; rejects
   replay within one process, mismatched key and stale/rotated invitation;
   uses the **verified actor**, never request `player`, when dispatching to the
   real Engine-backed `Gateway`. **Every command requires a nonempty, bounded
   ASCII `action_id` without surrounding whitespace;** the persistent Engine
   receipt deduplicates the same effect
   even if a signed packet is replayed to a fresh backend with an empty
   process-local nonce cache.

The fixture is intentionally **closed** to `/api/v1/feed` and `/api/v1/map`.
Only `/join`, `/room`, and strictly limited `say`/`look` commands are accepted.
Reply filtering removes the base gateway's **world-global event cursor** from
all these responses; otherwise off-room activity changes `MAX(events.id)` and
leaks coarse activity timing even when text is hidden. Safe room/audience
cursors need a visibility-aware design; simply stripping global cursors is
valid here only because this adapter has no feed/reconnect protocol yet.
Signed speech also refuses newline, terminal-control and invisible
bidirectional-format characters instead of silently relying on Engine text
normalization.
This is not because a late arrival must never read history: a world author may
deliberately designate public chronicles. The current gateway lacks an
authoritative **event visibility policy** separating public chronicle from
private/presence-only events, however. Its ordinary `feed(after=0)` currently
returns earlier room speech to new arrivals; that behavior is explicitly
reproduced in the test fixture. An invitation does **not** justify exposing
possibly private past events. The adapter therefore defaults to no feed,
map or `history`/`listen` until world/event privacy and author consent rules
exist and are enforced in the actual Engine/gateway transaction path.

## Authenticity scope (hostile boundary)

The HMAC proves possession of the configured signing key and integrity of the
envelope under that key. It does **not** prove that the player originally used
TLS, that the local signer was authorized to run, that the host process cannot
be inspected, or that a proxy stripped headers correctly. Signer and backend
share an in-memory key in these tests. A future operator-approved deployment
needs authenticated local IPC/host-bound key custody, process isolation,
TLS/certificate and reverse-proxy policy, nonce/key lifecycle across process
restart, and truly bounded parser/connection/rate resource limits.

The `PrivateHostPolicy` HTTPS URL is an expected identity value, **not** an
opened endpoint. Client-supplied forwarding headers are never treated as
principal authority. A stolen bearer remains useful until expiry/rotation,
and stolen signing key defeats the local HMAC boundary. Neither is solved by
calling the key private in a Python object. If signed receipts are ever used
across processes, key exchange and atomic durable replay checks are mandatory.
In particular, a valid envelope can pass a **new** `LocalSignedBackend`
instance using the same key within its 15-second lifetime: its nonce cache
starts empty. The fixture requires a durable `action_id` for every command
so the Engine prevents duplicate *gameplay mutations*, but this does **not**
establish durable transport-level replay rejection or cross-worker quotas.

## Executed local acceptance tests

```sh
PYTHONPATH=src python -m unittest tests.test_signed_private_edge -v
PYTHONPATH=src python -m unittest tests.test_private_host_contract -v
```

The fixtures deliberately forbid opening a socket in the positive integration
test. They instantiate two temporary SQLite-backed `Gateway` instances,
exercise real `Engine.join`, `say` and receipt persistence, test forged signed
actor/changed body/replay, rotated/revoked invitation, incorrect host/world/key,
expiry, invalid Origin, header aliasing, bounded replay cache, global cursor
metadata redaction, hostile speech characters, size/window budget and late-user
room-history disclosure. No Lappy model, registered player token or real host
service is used. The explicit old-oracle tests assert string-`"false"` and
underscore/whitespace forwarded aliases are denied.

**Not proven:** TLS transport, encrypted client handshake, authenticated
proxy-to-backend channel, persistent cross-process grants, Windows ACL,
real Android client, two-user network play, event visibility, fair/gameplay
policy, robust production rate limiting, or public exposure. Any such claim
requires independently executed host/device tests and exact authority.
