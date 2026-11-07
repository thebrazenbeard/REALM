# Scoped ship departure (source-only draft)

This generic, opt-in route binds an owner-authenticated ship departure to a
short-lived world credential and the same SQLite writer transaction as the
ship movement, idempotent receipt, and UTC arrival event.

## Contract

- `POST /api/v1/ship/depart` on the existing loopback-only GatewayServer.
- Requires a host-provisioned bearer with explicit `ship:depart` capability.
- JSON: `{"ship_id":"vesta","destination":"moon","action_id":"stable-id"}`.
- The token resolves the actual actor inside the ship transaction. A claimed
  `actor`, `player`, or `now` field cannot promote the caller or set UTC.
- Geography requires the resolved actor to be the registered ship owner.
  Generic crew delegation remains DENIED.
- The departure uses host UTC seconds and a host-configured route duration.
  No ALQS/QSD propulsion speed or fictional constant is invented.
- The WorldClock must be explicitly enabled. A disabled clock returns JSON
  HTTP 409 and leaves the ship docked.
- The same active token and stable action ID replay one existing result.
  A revoked token cannot replay and retrieve a past receipt.
- Credential revocation/rotation uses the same world SQLite writer lock.
  A revocation committed first denies departure; a ship action holding the
  writer lock commits before the later revocation.
- Existing legacy host-only ship commands and the default Gateway remain
  unchanged; this does not expose ships without scoped credentials enabled.

## Authority and release limits

This is source-only testing on disposable local worlds, not a deployment,
public API, mobile client, live VN2 world, or authorization to alter Driftspace
canon. The endpoint has no transport TLS, device-bound credentials, delegated
crew policy, inter-process trusted UTC authority, or external anti-rollback
witness for whole-world SQLite snapshots. Arrival requires a separately
opted-in UTC runner or explicit host clock ticks.
