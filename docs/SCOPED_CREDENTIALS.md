# Opt-in scoped player credentials (source-only draft)

Default `Gateway` continues using legacy, unexpiring host-provisioned player
bearers. Nothing here migrates, rotates, deletes, or revokes an existing token.

For a **new or deliberately opted-in world**, start the private loopback gateway
with `--scoped-credentials`. Host-issue a 15-minute token with:

```powershell
py -3 -m realm.gateway --db world.sqlite3 --world-id demo --scoped-credentials --provision-scoped-player Alice
```

The host-only Python API, `ScopedCredentialStore.provision`, supports exact
capabilities and a TTL of 1–3600 seconds. `rotate(current_token)` atomically
consumes the previous bearer and preserves its capability grants. `revoke(name)`
marks the whole principal inactive. No HTTP provisioning, refresh, revoke, or
rotation routes are installed. Operators should avoid logging/displaying
credentials in shared terminals. Do **not** use the legacy CLI provisioning,
rotation, or revoke flags together with `--scoped-credentials`; those combinations
are rejected rather than issuing unusable credentials.

The default scoped token grants `world:join`, `room:read`, `map:read`,
`feed:read`, and `command:write`. It does **not** grant `ship:depart`.
Hosts may issue narrower grants, such as `capabilities=("feed:read",)`.
`GatewayHandler` enforces capability grants on the corresponding HTTP route,
including the future opt-in `/api/v1/feed/wait` route if that code is stacked.
The separate PR86 ship adapter is **not** integrated in this PR; it still uses
legacy host credentials until its own owner revises the transaction boundary.

The SQLite table stores only SHA-256 digests of high-entropy random bearers,
UTC issuance and expiry, world/principal, capability JSON, rotation generation,
and revocation status. The external `.scoped-auth-witness.json` file stores a
world-bound revision and random nonce, updated *before* the SQLite transaction
commits on issuance/rotation/revocation. It detects restoring a stale SQLite
database **without also restoring the witness**. Put the witness outside DB
backup/restore scope and keep it under operator-only filesystem permissions.
If the process crashes between witness replacement and DB commit, the store
fails closed: operator recovery or fresh reprovisioning must be separately
qualified, not automatically guessed.

Normal authenticated and forged-bearer requests use read-only SQLite snapshots
and never force witness updates. When the store first sees a *matching* expired
bearer it atomically tombstones that row once and advances the external witness;
forged strings and repeated expired-token replays cannot cause repeated writes.
The store maintains a process-wide, per-world UTC high-water mark to detect
clock rollback across its store instances. Cross-process backward clock
movement after **read-only** activity is not durably detected. Issuance,
rotation, revocation, and observed expiry compare UTC against the persisted
high-water mark. Forward clock jumps can expire tokens early and later
corrections can require operator intervention. No caller controls the
production clock through the HTTP API.

**Security holds:** A stolen *currently valid* bearer remains indistinguishable
from its authorized holder without device/key binding. SQLite+external-witness
rollback together, filesystem-privileged modification, and cross-process clock
rollback are outside the demonstrated guarantee. The delayed HTTP request-body
revocation race is reduced by a second check just before dispatch, but a revoke
between that check and the gameplay write still lacks an atomic transaction
boundary. High-impact commands (especially Hardcore/ship) must not claim
instantaneous in-flight revoke safety without a writer-transaction check.
The current room feed also has independent historical recipient-visibility
limits; token lifecycle is **not** an event-time privacy fix. Keep the HTTP
gateway loopback-only; this is not TLS/public-hosting qualification.
