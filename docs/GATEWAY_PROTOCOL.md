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
The authenticated gateway also transactionally claims `player-bearer-http`
for its world database. Standalone shared-token Telnet claims
`shared-token-demo` on first player join; attempts to mix the two identity
systems against one world fail closed. These are durable configuration claims,
not verified human presence, actor-kind ownership, or tamper-proof custody.

For browser requests, Origin must exactly match one explicitly configured
`--allow-origin`; wildcard CORS is never granted. Responses disable caching.

| Request | Body/query | Response |
| --- | --- | --- |
| `GET /api/v1/health` | none | `{ "ok": true, "world_id": "demo" }` |
| `POST /api/v1/join` | `{"player":"Patrick"}` | `{"ok":true,"text":"Welcome ...","cursor":0}` |
| `POST /api/v1/command` | `{"player":"Patrick","command":"look","action_id":"optional-unique-id"}` | `{"ok":true,"text":"..."}` |
| `GET /api/v1/room?player=Patrick` | query | `{"ok":true,"text":"..."}` |
| `GET /api/v1/feed?player=Patrick&after=1` | query | `{"ok":true,"events":[...],"cursor":2}` |
| `GET /api/v1/map?player=Patrick` | query; optional authored geography | `{"ok":true,"space":{...},"origin":{...},"width":3,"height":2,"tiles":[...],"markers":[...],"player":{...},"legend":[...]}` |

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

Each event is `{id,kind,actor,text,room,at}`. `id` and `cursor` are **per-player
delivery positions**, not SQLite event IDs or shared world clocks. Movement creates a `leave` event
in the source room and a `move` (arrival) event in the destination in one
transaction. The feed is **polling**, not server push. It returns only events
with an event-time delivery grant for that authenticated player, up to 100
per request. Delivery is determined by **persisted room co-location at the
original event transaction**; an actor also receives their own movement
events. Later arrival or room re-entry never grants older events. `history`
and `listen` use the same grant table and show only previously granted events
for the actor's current room. Legacy events without durable grants fail closed.
Unknown future event kinds and incompatible payload shapes receive no implicit
delivery. An authorized operator can redact a previously granted delivery via
the host-owned `revoked_at` field; no public revocation endpoint exists yet.

**Important scope:** A player left in a room in persisted world state counts
as present for delivery even if no browser/terminal session is active. This is
not proof of live observation or consent. Authenticated online presence,
session lifecycle, private messages, public-chronicle opt-in and richer
event-time sensory/consent policy remain future work. These event grants are
not automatically grants for NPC training or derived memory.

`join` returns `cursor:0` on both first join and reconnect: joining does not
acknowledge receipt of unseen events. Clients must page through their own
authorized backlog or explicitly retain a previously acknowledged cursor.
`command` and `room` deliberately return **no cursor**: they aren't delivery
acknowledgments. Only a feed response's actually returned events may advance a
client's consumption cursor; an empty feed returns its input cursor, including
when a new event commits concurrently with the read.

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

