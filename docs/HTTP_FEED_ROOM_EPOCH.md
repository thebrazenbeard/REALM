# Same-actor feed response epoch — source-only qualification

Issue: [REALM #192](https://github.com/thebrazenbeard/REALM/issues/192).
This document describes one narrow *current-room response freshness* fix.
It does **not** establish one-login-per-character, a session lease, TLS,
a durable per-connection session epoch, or safe public web hosting.

## Threat / demonstrable counterexample

Two independent HTTP sockets can present **the same valid player bearer**.
The first authenticates and computes a current-room feed. It pauses before
the HTTP response. The second authenticates with that same bearer and
moves the joined player to another room or rejoins in the same room.
The first sends the earlier room's event text **after** the actor has
departed or created a newer arrival. This behavior occurs for both
legacy and opt-in scoped credentials in unmodified REALM main at
`77e46517b5ea5d44e2dcbc408211dadc7c7808a1`.

That is a response-timing disclosure, not evidence that the token was forged
or that the second connection changed an actor other than its authenticated
principal. Same-bearer multi-session policy remains unspecified.

## Minimal candidate

`Gateway.feed` already binds the player, room, provable latest arrival
event, event rows and channel revision to **one SQLite read snapshot**.
Expose that read snapshot's latest `room_entry_cursor` in the response.
The default HTTP feed handler then compares the snapshot arrival to the
current `Gateway._room_entry_cursor(principal)` **as close to response send
as possible**, after existing bearer-rotation and channel membership
revalidation. If the actor moved, rejoined, or the arrival ledger became
unprovable, respond `409 Room arrival changed; retry feed` without the
stale event content. The client must re-read the new current room.

The entry hint is an existing kind of advisory cursor, not a credential,
room entitlement, or grant to rewind room history. The underlying feed
still checks the exact room arrival and channel authorization on each read.

## Reproducible negative and positive tests

`tests/test_same_actor_feed_response_epoch.py` uses only generated tokens,
a disposable SQLite world and two simultaneous connections to an OS-selected
loopback HTTP port. It exercises:
- Legacy player bearer, second client moves the actor.
- Legacy player bearer, second client rejoins in the same room.
- Scoped player bearer, second client moves the actor.
- Scoped player bearer, second client rejoins in the same room.

The first request **finishes constructing** a feed containing another
player's current-room speech, pauses immediately before HTTP send, and
the second connection performs its allowed world action. Each test asserts
that the first socket returns `409` without the old-room event and that
a fresh feed succeeds without disclosing the old event.

Against unchanged main, **4/4 tests fail**: the first HTTP feed returns
`200` with the old-room event. Against the candidate, **4/4 pass** in
normal and optimized Python. Separate gateway, feed-history, cross-transport
and scoped credential regressions must also pass.

## Security ceiling and unresolved decisions

> This check is **not atomic with network write**. Another writer can move
> the actor between the final room-entry lookup and bytes being transmitted.
> A stronger guarantee requires transactional response custody, token-bound
> session epochs, or an actor ownership lease with carefully scoped
> invalidation, plus load testing.

> `GET /room`, `GET /scene`, long-poll feed and text-based private
> commands have separate response paths; this change only covers the
> default `GET /api/v1/feed` read. Do not silently claim the others are
> protected by this specific room-epoch comparison.

> If shared-character sessions are intentionally permitted, a clear
> documented policy must still define how older clients handle moves,
> room-scoped content, token rotation and replay. A 409 retry protects one
> response, not an entire old client's session. If exclusive sessions are
> intended, this is not an implementation of exclusivity.

No REALM or Driftspace production server, worldpack, player save, user
credential, published Wreckforge canon or host model is changed.
