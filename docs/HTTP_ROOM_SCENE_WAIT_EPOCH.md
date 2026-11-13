# Room, scene and opt-in long-poll response-epoch checks (source-only)

**Dependency:** REALM held [Draft PR #198](https://github.com/thebrazenbeard/REALM/pull/198),
which adds current-room arrival freshness to default HTTP `GET /api/v1/feed`.
This is a separately held *stacked* source candidate; neither the parent
nor this change has been merged, installed, or approved for public hosting.

## Why this exists

Two independent HTTP connections holding the **same valid player bearer**
may currently move or rejoin the canonical actor while a prior connection
is preparing an authorized response. This is a policy still undecided
under [REALM issue #192](https://github.com/thebrazenbeard/REALM/issues/192).
It does not mean either client impersonated a different actor.

The earlier PR #198 only fences the default feed. Here, `GET /api/v1/room`,
`GET /api/v1/scene` and the opt-in `FeedWaitServer`'s
`GET /api/v1/feed/wait` (including `wait_ms=0`) receive their own
near-send current-room arrival checks.

## Source design

- `Gateway.room()` retains the established text from the engine's
  classic `look` route. It samples the latest *provable* actor arrival
  before and after composing the text and cursor. A move/rejoin between
  these reads produces an untrusted/absent epoch marker. Since the look
  method and cursor use different read snapshots, this is **not** an
  atomic scene renderer; it is conservative tamper/race rejection.
- `Gateway.scene()` already composes the room, exits, items, occupants,
  and event cursor in one SQLite read snapshot. It now also obtains a
  provable current-room arrival **inside that snapshot**. If the event
  ledger cannot prove the actor is in that room, the result's epoch is
  absent; the HTTP handler refuses to send that view.
- The default HTTP handler, after its existing bearer and channel checks,
  validates the snapshot's `room_entry_cursor` for `/room` and
  `/scene` as well as the parent PR's `/feed`. Mismatch or unprovable
  arrival -> HTTP **409** with **no stale view payload**.
- The separately opt-in `FeedWaitHandler` already revalidated the room
  during polling, but could still wait for a SQLite presence-registration
  writer after those checks. It now rechecks the original feed snapshot's
  provable arrival **after** that late refresh and immediately before
  `_send(200, result)`. Mismatch -> HTTP **409** without stale event
  text. This applies to immediate `wait_ms=0` and real pending waits.
- No new player identity, session lease, exclusive login policy, world
  DDL, credential issuance/rotation, event write, or public listener.

## Test-integrity evidence

`tests/test_same_actor_view_response_epoch.py` tests 10 deterministic
loopback socket interleavings (legacy and scoped bearer):
- Four `/room` + `/scene` cases for each credential mode:
  second same-token client moves the actor, or rejoins the same room,
  **after the first connection builds the old response but before send**.
- An additional `/room` test per credential mode pauses the engine
  `look` itself, then moves the actor **between the arrival precheck and
  rendering**. This catches non-atomic room-view assembly.

`tests/test_same_actor_wait_response_epoch.py` tests eight interleavings:
legacy/scoped, move/rejoin, and `wait_ms=0`/positive wait. Positive-wait
tests first capture an **empty initial feed**, then append the second
actor's room-specific speech while polling. The first client has its
authorized event result but pauses at the late presence refresh; the
other same-token connection then changes the actor's arrival. The first
must return **409 with no stale room text**, and a fresh read must not
reveal the earlier event.

Against untouched parent PR #198 source, the 10 view cases and eight
wait cases fail their 409 assertions. With the candidate patch, all
18 pass in normal and optimized Python. Existing gateway, room-entry,
credential, transport and event-stream regression suites must still pass
on pinned published-source checkouts before any broader qualification.

## Hostile boundaries and release holds

> A near-send room-arrival lookup is **not an atomic fence on network
> transmission**. A writer can move an actor after the last check but
> before all response bytes arrive at the client. Exclusive session
> custody, generation-bound leases, or transactional response ownership
> requires an explicit, separately reviewed design. The user has not
> authorized such a policy through this source change.

> Two valid client connections remain allowed to share one character.
> Client caches/transcripts cannot be remotely erased: after a newer
> movement, an older client might *already have received* previous room
> data legitimately. This guard refuses **new stale response delivery**
> within a reproduced window; it is not retroactive amnesia.

> `/map`, game command responses, group/private text,
> long-poll channel membership and late disconnect/revoke paths have
> additional independent authorization contracts. This test is not a
> general confidentiality certification. The `/map` route explicitly
> lacks fog-of-war authorization and remains disallowed for public hosting.

No live Driftspace game service, world save, existing real tokens,
private Wreckforge lore, GGUF weights, or deployed host was changed.
