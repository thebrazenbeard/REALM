# REALM local gateway: provisional HTTP/JSON v1

This adapter lets a local browser client use the **same SQLite world state**
as the command-line client. It does not create a hosted public MUD, player
accounts, Telnet/GMCP compatibility, or an encrypted internet endpoint.
The wire format is deliberately provisional while classic MUD prior art
(including Evennia, AresMUSH and Ranvier) is being evaluated.

## Run

With Python 3.11+ from the repository checkout:

```sh
PYTHONPATH=src python -m realm.gateway \
  --db data/demo.sqlite3 \
  --world-id demo \
  --port 8768 \
  --allow-origin http://127.0.0.1:8000
```

The process generates a random bearer token and displays it **locally at
startup**. Alternatively, set `REALM_GATEWAY_TOKEN` in the gateway host's
environment. Never check the token into source or put it in a URL.

The server listens on `127.0.0.1` only, regardless of flags. Host your
static HTML locally for initial testing. A GitHub Pages site can host HTML,
CSS, and JavaScript but **cannot execute the Python game host**. An actual
remote Pages-to-host connection would require an authorized HTTPS endpoint,
TLS, authentication, and further deployment/security review. Do not assume
that a Pages HTTPS site can access an HTTP loopback/LAN address.

## Provisional endpoints

Requests other than `GET /api/v1/health` require header:

```
Authorization: Bearer <per-process-gateway-token>
```

For browser requests, Origin must exactly match one explicitly configured
`--allow-origin`; wildcard CORS is never granted. Responses disable caching.

| Request | Body/query | Response |
| --- | --- | --- |
| `GET /api/v1/health` | none | `{ "ok": true, "world_id": "demo" }` |
| `POST /api/v1/join` | `{"player":"Patrick"}` | `{"ok":true,"text":"Welcome ...","cursor":1}` |
| `POST /api/v1/command` | `{"player":"Patrick","command":"look","action_id":"optional-unique-id"}` | `{"ok":true,"text":"...","cursor":1}` |
| `GET /api/v1/room?player=Patrick` | query | `{"ok":true,"text":"...","cursor":1}` |
| `GET /api/v1/feed?player=Patrick&after=1` | query | `{"ok":true,"events":[...],"cursor":2}` |

Each event is `{id,kind,actor,text,room,at}`. The feed is **polling**, not
server push. It is filtered to the player's current room, returns up to 100
events per request, and advances its cursor only for returned events.
The join response supplies a starting cursor to avoid displaying a room's
entire previous history as new activity.

Only names, not authenticated identities, exist in the first prototype.
The gateway bearer token grants full local world access, including use of
other player names. It is strictly a single-owner development adapter.
Public multiplayer requires player-bound authentication and authorization,
rate limiting, meaningful presence, privacy rules, and an independent
security review before any network exposure.

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
