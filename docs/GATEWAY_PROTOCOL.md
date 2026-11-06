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
| `GET /api/v1/feed?player=Patrick&after=1` | query | `{"ok":true,"events":[...],"cursor":2}` |

Each event is `{id,kind,actor,text,room,at}`. Movement creates a `leave` event
in the source room and a `move` (arrival) event in the destination in one
transaction. The feed is **polling**, not
server push. It is filtered to the player's current room, returns up to 100
events per request, and advances its cursor only for returned events.
The join response supplies a starting cursor to avoid displaying a room's
entire previous history as new activity.

Credentials are bound to one character name, but are long-lived bearer
capabilities rather than expiring sessions with account recovery. Anyone who
obtains a player's token can act as that player until rotation or revocation.
Rate limiting, meaningful presence, public account flows, denial-of-service
controls, and independent host/security qualification are still required before
**any** network exposure. CORS is not authentication. This branch remains a
loopback-only development adapter.

Terminal play without this gateway is already available via:

```sh
PYTHONPATH=src python -m realm --db data/demo.sqlite3 --player Patrick
```

This is a local CLI, **not Telnet or SSH server support**. Classic network
MUD client support can be added as separate transport adapters with session
authentication and structured event/GMCP handling; do not expose plaintext
Telnet by default.

REALM is the independent public engine. Driftspace can later be a separately
hosted, **private** REALM worldpack; Wreckforge proprietary content must
not be copied into public REALM code or demo data.
