# Private multi-user ingress — bounded qualification contract

**Status:** research contract with a pure in-process reference evaluator and
synthetic adversarial tests. **No remote gateway is implemented or certified.**
No Tailscale, TLS listener, external port, invitation issuer, token store,
real credential, or remote player was created by this lane.

REALM should let each creator host an **independent** text-only world from
their own machine and invite other human or AI players. Hosting is not a
central service; the same engine can run multiple disjoint SQLite worlds, each
with separate worldpack, owner, player identities and state. A public Siltglass
Reach example must not become a privileged engine dependency.

## Current source evidence versus release requirements

| Source-inspected current code (PR #23 base `82242f0`) | What a remote private host still needs |
| --- | --- |
| `GatewayServer` binds `127.0.0.1` only, plaintext HTTP. | Operator-approved, authenticated HTTPS reverse proxy; loopback backend still private. |
| `Gateway` binds `world_id` transactionally to the SQLite DB. | Pin a **host identity** as well as world ID at a trusted ingress; reject mismatched host/world routing. |
| `CredentialStore` provisions independent random player bearers, stores digests, supports rotate/revoke; tokens remain valid until revoked. | Explicit host/world-bound invitations, expiry, refresh/rotation/revocation, player role/scope and stolen-token handling. |
| HTTP resolves actor from credential, not claimed `player`. | Keep actor binding at all route layers, including future GMCP/Android/WebSocket/OLC. Never promote a Telnet player name to an account. |
| Exact browser Origin allowlist is optional; requests without Origin are accepted if bearer is valid. | Browser origin/CSRF policy and no ambient-cookie auth. CORS by itself never authenticates clients. |
| POST JSON has 4096-byte `Content-Length` limit; no application rate limiter. | Host-measured body/header/query quotas, token/client buckets, concurrent connection cap, timeouts, audit, overload tests. |
| Room feeds filter by player's **current** room and `history` is game history; map has no full fog-of-war. | Event-time visibility/consent grants, privacy boundaries, gap/backfill rules; don't expose private history as soon as a player enters a room. |
| `/api/v1/health` reveals `world_id` without a player credential on loopback. | Deliberate health endpoint information policy before reverse-proxy exposure. |

The `src/realm/private_host_contract.py` evaluator is an **offline contract
oracle** only. It does not plug into `GatewayHandler`. Its `TrustedEdgeEvidence`
object is trivially constructible by Python code and is **not** security proof.
`tls_verified`, `adapter_authenticated`, host identity, request sizes, rate
counter and clock MUST come from an independently authenticated, host-owned
adapter, never from request headers/JSON/query parameters. Passing these unit
tests does not mean the adapter or invitation issuer exists.

## Minimum private host path, after explicit authorization

1. Operator selects a private world DB and configures an exact host identity,
   canonical HTTPS origin, allowed browser origins, world ID and resource
   limits. These are immutable for the lifetime of an admitted request.
2. An operator-controlled private HTTPS edge (for example **Tailscale Serve**,
   not public **Funnel**) proxies to the single instance's loopback gateway.
   Verify tailnet grants, host certificate/name and listener inventory. Serve
   on Windows may require an elevated administrator context and tailnet HTTPS
   enablement; neither is authorized by this document. An SSH private forward
   may be an operator-selected developer alternative, not a substitute for
   application player authentication.
3. The edge strips *client-supplied* forwarding/identity headers such as
   `Forwarded`, `X-Forwarded-*`, `X-Real-IP`, `X-Tailscale-*`; any trusted
   post-proxy identity metadata must be delivered over a genuinely authenticated
   local channel with provenance and never accepted from direct loopback
   clients. A TLS URL alone does not authenticate the in-process adapter.
4. Operator issues one scoped invitation for an exact `(host_id, world_id,
   player, scopes, expiry)`. Store only a high-entropy secret digest under the
   world's host-owned credential store. On request, verify token, revocation,
   expiry, actor, operation, host/world and current limits; return a generic
   denial without a bearer echo or account/world enumeration.
5. Browser requests require an exact origin allowlist. A native app may omit
   `Origin` but still must supply its own bearer over verified HTTPS; absence
   of `Origin` proves nothing about identity. If ambient cookies are added
   later, introduce explicit CSRF token/origin defenses rather than assuming
   bearer-header rules suffice.
6. World action/room/feed/map APIs remain authoritative and actor-bound. A
   cross-world request cannot select another world DB or bypass host identity;
   an NPC model or worldpack cannot mint invitations. Cleartext, invalid
   certificate, wrong host, redirect and stale/rotated tokens fail closed.

Transport options (none deployed here):

| Option | Scope | Decision |
| --- | --- | --- |
| Loopback HTTP/Telnet prototypes | Same host only; Telnet shared token is plaintext | Keep for local testing; **never expose directly**. |
| Tailscale Serve → authenticated gateway | Private tailnet HTTPS, operator/admin configuration | Preferred pilot **after** identity/security tests and explicit host approval; device membership is not player login. |
| SSH forwarding → loopback | Authenticated operator private tunnel | Diagnostic alternative; requires independently qualified SSH host/key policy. |
| Tailscale Funnel, router port forwarding, public TLS reverse proxy | Internet-visible | **Out of scope**, not authorized or qualified. |

Official references: [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve),
[Funnel](https://tailscale.com/docs/features/tailscale-funnel), and
[Android local-network permissions](https://developer.android.com/privacy-and-security/local-network-permission).
For Android 17-targeting clients, direct LAN access has additional permission
behavior; the native APK remains a separate client-only release lane.

## Offline adversarial contract tests

```sh
PYTHONPATH=src python -m unittest tests.test_private_host_contract -v
```

Synthetic tokens and fixed timestamps only. The tests instantiate no
`CredentialStore`, launch no socket or service, and check:

- Verified edge required; wrong host URL/device/world fails.
- Forged forwarded/Tailscale identity headers fail.
- Cross-world invitation, wrong player, stolen/wrong token and unscoped write
  fail even when request JSON claims otherwise.
- Revoked/expired grant fails; exact allowed browser origin succeeds, lookalike
  and HTTP origin fail; native Origin-less requests still require a valid bearer.
- Wrong method, oversize body/headers and exhausted trusted quota fail.
- Invalid operator host policy fails before admission.

These tests validate only a **pure decision function**. They do not exercise
actual certificates, proxy identity or stripping, real clock/skew, rate-limit
storage, existing gateway handlers, two-device connections, token management,
Windows ACLs, Android network behavior or real host resource exhaustion.

## Real-system adversarial acceptance gates — not executed

| Gate | Scenario | Required outcome |
| --- | --- | --- |
| H1 host identity | Two privately served REALM instances; change Host, SNI, URL, TLS CA and tailnet access grants. | Wrong/foreign host rejected without any credential disclosure or cross-realm DB route. |
| H2 player isolation | Two independent invited players and two worlds; replay/swapped token, query `player`, actor/role spoof, map leakage. | Only provisioned actor/world data accessible; no undiscovered map/room/history. |
| H3 lifecycle | Steal synthetic test token, rotate/revoke, expire mid-reconnect, stop/restart host. | Revoked/expired token rejected; legitimate session recovers only after reauthorization. |
| H4 transport | Cleartext, invalid certificate, redirect to attacker, intermediary header injection. | Fail closed; credentials never sent to untrusted destination; forwarded identity not trusted. |
| H5 request pressure | Oversize/fragmented HTTP, conflicting lengths, slow client, header floods, bursts/restarts. | Consistent limits and backpressure; no stuck worker, memory leak or request desync. |
| H6 state replay | Two clients contest one item; retry same `action_id` across disconnect/restart. | Exactly one committed game effect/receipt; legitimate second player sees authoritative event. |
| H7 privacy | Enter room after prior private events, revoke visibility, try old cursor and another world. | Event-time grants and consent enforced, no retrospective disclosure. |
| H8 phone | Patrick's authorized Android device after separately approved APK install and private host setup. | Valid HTTPS host identity, credential storage, LAN permission handling, reconnect/backfill and accessibility pass with exact artifact hashes. |

Evidence receipts must identify source commit, backend DB/world IDs, client
builds, host/operator configuration, authenticated edge version, network path,
TLS/certificate checks, redacted logs and observed effects. Record violations
without printing tokens or private player text. No mock, model review or
tailnet diagram substitutes for real H1–H8 qualification.
