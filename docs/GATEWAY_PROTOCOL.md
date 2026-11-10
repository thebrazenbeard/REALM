# REALM local gateway: player-bound authorization revision (provisional HTTP/JSON v1 routes)

This adapter lets a local browser client use the **same SQLite world state**
as the command-line client. It does not create a hosted public MUD, public
account registration, Telnet/GMCP compatibility, or an encrypted internet endpoint.
The wire format is deliberately provisional while classic MUD prior art
(including Evennia, AresMUSH and Ranvier) is being evaluated.

## Provision player credentials locally

With Python 3.11+ from the repository checkout:

```sh
PYTHONPATH=src python -m realm.gateway --db data/demo.sqlite3 --provision-player Patrick
PYTHONPATH=src python -m realm.gateway --db data/demo.sqlite3 --provision-player Vera
```

Each command prints one independently generated 256-bit player bearer token
**once** on the host terminal. Give each player only their own token through
a trusted private channel. Never put tokens in source control, an HTTP URL,
or a public log. The SQLite `player_credentials` table stores only SHA-256
digests of high-entropy random tokens, not plaintext bearer tokens.

To recover a lost or compromised token, run `--rotate-player Patrick`, which
immediately revokes the old token. `--revoke-player Patrick` disables remote
access without deleting game progress. Token issuance, rotation, and revocation
are **host-local administrative operations**; there is no network enrolment
route. Credentials persist across gateway restarts.

## Run private-host gateway

```sh
PYTHONPATH=src python -m realm.gateway \
  --db data/demo.sqlite3 \
  --world-id demo \
  --port 8768 \
  --allow-origin http://127.0.0.1:8000
```

The obsolete process-wide `REALM_GATEWAY_TOKEN` no longer authorizes gameplay.
The `GatewayServer` constructor no longer accepts a world-wide token parameter.

The first gateway opening a new SQLite file transactionally binds its
`--world-id` into the database's `world_meta`. Every later gateway reopening
that file **must** supply the same identity; changing only the HTTP label can
never turn an existing player's world into a different REALM. A pre-upgrade
database that already contains player/event history but has no binding fails
closed until the local owner reviews it and explicitly runs the intended
`--world-id` with `--adopt-unbound-world` **once**. That flag never overrides
an existing different binding. Back up any valued local data before adoption;
this is administrative metadata migration, not an inter-world import or merge.

The server listens on `127.0.0.1` only, regardless of flags. Host your
static HTML locally for initial testing. A GitHub Pages site can host HTML,
CSS, and JavaScript but **cannot execute the Python game host**. An actual
remote Pages-to-host connection would require an authorized HTTPS endpoint,
TLS, authentication, and further deployment/security review. Do not assume
that a Pages HTTPS site can access an HTTP loopback/LAN address.

## Provisional endpoints

Requests other than `GET /api/v1/health` require header:

```
Authorization: Bearer <per-player-token>
```

Use the token provisioned for the *specific player*.
The server derives the actor identity from the bearer token. A `player` field
in JSON or query parameters is optional legacy syntax and, if supplied, **must
match** that token's identity (case-insensitively); it can never select a new
actor or feed audience. Mismatches return HTTP 403; missing/invalid credentials
return 401.

**Breaking auth-contract change:** these `/api/v1/*` paths are retained solely
to avoid rewriting the experimental browser client's routes. The former
world-wide bearer-token behavior has been retired, not kept as a fallback.
Clients must replace shared-token configuration with player-specific tokens.
Future stable external protocols should use explicit `/api/v2` versioning.

For browser requests, Origin must exactly match one explicitly configured
`--allow-origin`; wildcard CORS is never granted. Responses disable caching.

| Request | Body/query | Response |
| --- | --- | --- |
| `GET /api/v1/health` | none | `{ "ok": true, "world_id": "demo" }` |
| `POST /api/v1/join` | `{"player":"Patrick"}` | `{"ok":true,"text":"Welcome ...","cursor":1}` |
| `POST /api/v1/command` | `{"player":"Patrick","command":"look","action_id":"optional-unique-id"}` | `{"ok":true,"text":"...","cursor":1}` |
| `GET /api/v1/room?player=Patrick` | query | `{"ok":true,"text":"...","cursor":1}` |
| `GET /api/v1/scene?player=Patrick` | query | Authenticated saved-room facts: `room`, `exits`, `items`, `occupants`, `presence_semantics`, `truncated`, `cursor` |
| `GET /api/v1/feed?player=Patrick&after=1` | query | `{"ok":true,"events":[...],"cursor":2}` |
| `GET /api/v1/map?player=Patrick` | query; optional authored geography | `{"ok":true,"space":{...},"origin":{...},"width":3,"height":2,"tiles":[...],"markers":[...],"player":{...},"legend":[...]}` |

The optional `/scene` endpoint reads the authenticated player's room, exits,
visible items, and actors' saved room positions in one SQLite read snapshot.
Each collection is capped at 50 entries with a `truncated` flag. The
`presence_semantics` value is `saved_position_only`: an occupant is not proof
of a connected human, an NPC classification, an AI agent, or an available
`talk` action. Unknown inhabitants are never invented by this endpoint.

The map endpoint reads the joined player's **actual persisted room** from
`players.room_id` and picks the closest authored geography layer containing
that room. If no geography/worldpack map is installed for it, the host returns
`404 {"ok":false,"error":"No map is configured for this location"}`. It does
not manufacture a terrain map from ordinary textual rooms. `tiles` contain
viewport-relative `x,y,glyph,terrain,passable`; `markers` contain
`id,name,kind,x,y`; `origin` contains the absolute tile offset. The response
is bounded to 32×32 cells, and the `player` marker may be `null` when absent
from the viewport. The hosting browser must render content as inert text.

`Geography(db_path)` can define a world hierarchy (universe/system/planet/
region/settlement/room) alongside the base Engine's SQLite tables, and
world authors can supply explicit terrain tiles and travel routes. This is
an authoring API, not an automatic galaxy generator or public worldpack
loader. Ship departure/arrival state is persisted and advanced by explicit
host ticks; no always-running simulation is implied.

### Opt-in scoped ship-owner history (source-only)

An explicitly scoped gateway can also serve:

`GET /api/v1/ship/events?ship_id=vesta&after=0`

This read-only route requires an **active scoped** bearer already carrying the
existing `ship:depart` capability, a joined principal, and exact persisted
`geo_ships.owner` equality for that ship in the gateway's bound world. It does
not issue credentials, grant ship access to crew, use a legacy unscoped bearer,
or expose a fleet list. Unknown and other-owned ships are both HTTP 403; missing,
expired or revoked scoped pilot credentials are HTTP 401. Invalid, repeated,
foreign-world-selector or future-cursor query parameters are HTTP 400.

The bounded response has `{ok,world_id,ship_id,events,cursor}`; each event is
`{id,at,ship_id,actor,kind,payload}` sourced from persistent `geo_ship_events`,
ordered by increasing database ID, at most 100 per page. `at` is a UTC epoch
second from the ship ledger. Repeating an earlier cursor may redeliver events;
clients deduplicate by `(world_id,ship_id,id)` and persist the last consumed ID.
This is **owner-only history**, not the room feed, an authorized crew timeline,
live ship telemetry, or a perpetual world ticker. Ship arrival still needs an
explicit host UTC tick. The provisional loopback transport remains unsuitable
for public network exposure.

`WorldClock.tick` additionally compares the complete persisted ship, ship-event
and receipt ledgers before and after its trusted UTC callbacks, including idle
and unrelated audit-only ticks. Only exact scheduled, in-transaction ship
arrivals and bound host ship departures may add ledger effects; SQLite trigger
injections that add, delete, or rewrite other ship history abort that tick.
This is **clock-transaction custody**, not a proof that all rows returned by
the GET were originally legitimate: independent gateway departure transactions,
direct host `Geography` writes, out-of-band privileged SQLite modifications,
and whole-file database rollback are outside this tick's authorization boundary.
Owner history is a persisted observation surface, not a cryptographic
attestation or assurance that arbitrary raw event payloads are secret-free.

The scoped, authenticated `POST /api/v1/ship/depart` writer also checks **its
own transaction's** complete ship-state/event/receipt delta before commit,
including exact one-time replay, registered owner, route, and scheduled UTC
arrival. A same-writer privileged trigger that forges an extra departure,
changes another ship's history or removes a receipt rejects and rolls back the
entire POST; unrelated permitted room `say` events still proceed. This is not
global tamper-proofing: an explicitly caller-owned transaction may continue
to write after `depart_in_transaction` returns, standalone host Geography
methods and out-of-band privileged SQL do not use this guard, and a later
whole-file rollback still lacks an external witness.

The scoped departure writer separately snapshots **world/credential/clock
authority** before authorization: all `world_meta` rows, scheduler queue and
UTC watermark, roster attestations, player identity/credential tables, and
scoped credential guard/capability rows. The only permitted queue change is
the departure's exact new pending `ship_arrival` (or no change on replay).
Privileged installed triggers cannot silently rebind `world_id`, grant another
player a capability, revoke a credential, rewrite an earlier clock receipt or
add an ungranted second deadline while returning successful HTTP departure;
these mutations reject and roll back the request. The separate room event
ledger still allows unrelated `say` records. This is an authenticated ship
writer postcondition, not a universal attestation of every gameplay table or
of privileged external database activity after commit.

Each event is `{id,kind,actor,text,room,at}`. Movement creates a `leave` event
in the source room and a `move` (arrival) event in the destination in one
transaction. The feed is **polling**, not
server push. It is filtered to the player's current room, returns up to 100
events per request, and advances its cursor only for returned events.
The join response supplies a starting cursor to avoid displaying a room's
entire previous history as new activity.

Keyed gameplay commands retain mutation idempotency: replaying the same
`action_id` cannot take, gather, craft, or say twice. Read-only commands,
including `look`, `examine`, `inventory`, `who`, `history`, `resources`,
`recipes`, `structures`, and bare `gather`, are evaluated against current
authorized state on each retry. An old observation receipt cannot grant
access to an item another actor now holds or to speech from before arrival.
`examine <item>` (alias `x <item>` or `look at <item>`) only reveals items
in the caller's room or inventory and writes no world event.
New keyed read receipts retain the actor, ID and original command with an
empty `response` sentinel; observations are returned fresh without persisting
their contents or rewriting the receipt on replay. Existing receipts created
by older builds may still contain nonblank observation responses. Those rows
persist until a **separately authorized migration**; this change performs no
automatic destructive database rewrite or receipt cleanup.

`CommandRouter.act` owns the shared Engine/Crafting action-ID namespace for
interactive commands. It dispatches recognized crafting verbs before Engine,
which otherwise treats those verbs as unknown. Calling `Engine.act` and
`Crafting.perform` directly with the **same** actor, command, and action ID
across handlers is unsupported: a low-level caller can replay the other
handler's receipt instead of its own command result. Embeddings should route
interactive player commands through `CommandRouter` consistently; this is an
internal API boundary, not an observed HTTP/Telnet/CLI authorization bypass.

Credentials are bound to one character name, but are long-lived bearer
capabilities rather than expiring sessions with account recovery. Anyone who
obtains a player's token can act as that player until rotation or revocation.
The map endpoint enforces the same authenticated character identity, but
it may still expose author-defined markers the player has not discovered;
fog-of-war and information-flow rules remain unimplemented.
Rate limiting, meaningful presence, public account flows, denial-of-service
controls, and independent host/security qualification are still required before
**any** network exposure. CORS is not authentication. This branch remains a
loopback-only development adapter.

Terminal play without this gateway is already available via:

```sh
PYTHONPATH=src python -m realm --db data/demo.sqlite3 --player Patrick
```

The local CLI needs no network server. A separate loopback-only experimental
Telnet adapter is also available (see `TERMINAL.md`), but its local connection
tokens and live-name reservation are not durable account authentication.
GMCP, public Telnet, and SSH transport qualification remain outstanding.

REALM is the independent public engine. Driftspace can later be a separately
hosted, **private** REALM worldpack; Wreckforge proprietary content must
not be copied into public REALM code or demo data.

