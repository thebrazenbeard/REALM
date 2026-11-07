# REALM browser console (static GitHub Pages source)

These files are a setting-neutral browser client. There is no Wreckforge or
Driftspace lore here. They can be served as a static site; no build step,
JavaScript framework, CDN, or paid API is required.

## Reality check

- `index.html` renders offline with an explicit **OFFLINE PREVIEW** label.
- A **live feed** appears only after the browser has joined a separately
  running, compatible REALM gateway. Static hosting does not execute the world.
- A Pages deployment has **not** been enabled by creating these files or PRs.
- The world server must still run on a host authorized by its operator.
- Browser connection is not the same thing as a standard Telnet/GMCP MUD
  session; those require a separate terminal protocol service.
- A public Pages URL is a **public interface**, even if the backing world
  requires private login. It must contain no proprietary worldpack content.

## Integrated REALM gateway contract (local v1 prototype)

The browser controller and Python gateway are integrated in this source tree.
Their loopback HTTP interaction is covered by an executed Node-to-Python test;
this does **not** qualify remote hosting or real-browser rendering.

| Method | Route | Contract |
| --- | --- | --- |
| GET | `/api/v1/health` | `{ "ok": true, "world_id": "..." }` |
| POST | `/api/v1/join` | Request `{ "player": "..." }`; response `{ "text": "...", "cursor": 0 }` |
| POST | `/api/v1/command` | Request `{ "player": "...", "command": "look", "action_id": "..." }`; response `{ "text": "..." }` |
| GET | `/api/v1/room?player=...` | `{ "text": "..." }` |
| GET | `/api/v1/feed?player=...&after=0` | `{ "events": [{ "id": 1, "kind": "say", "actor": "...", "text": "...", "room": "...", "at": "..." }], "cursor": 1 }` |

All routes except health require `Authorization: Bearer <token>` in the HTTP
header. The page accepts the token through a password input and holds it only
in memory: it is **never** embedded in the URL, repository, or browser storage.
The gateway must still enforce authentication, permissions, and exact-origin
CORS. **A token in a browser is accessible to the local user and browser
extensions**; do not regard this design as hardened internet deployment.

Disconnect aborts outstanding browser HTTP requests and clears the in-memory
session credential. Delayed responses from an old connection cannot create a
new polling loop, append errors to a rejoined player's transcript, or reset the
controls for a newer connection. Concurrent join attempts are ignored while
one is in flight. **Aborting
the browser request does not roll back a command that the host may have already
committed.** The host action-ID replay guard remains authoritative for retries.

Only output returned by the gateway can populate live world fields. Remote
text is added through `textContent`, never parsed as HTML. The room and feed
must represent only the connected character's authorized knowledge, enforced
by the server.

## Running locally

For static layout inspection, open `index.html` or serve this directory with
any local static HTTP server. The client remains clearly offline until a
compatible host is entered and the join request succeeds.

For eventual GitHub Pages, configure the REALM repository's Pages publishing
source to the `/docs` directory on the owner-approved branch. This configuration
is a **separate external publication action** and is intentionally not done by
the PR. GitHub Pages serves these files, not the Python engine/database.

From a Pages `https://` URL, the API must also be exposed through an authorized
**HTTPS** endpoint. No host exposure, tunnel, port forwarding, or remote login
is enabled by this source tree.

## Terminal paths

The current standalone Python prototype supports multiple terminal processes
using the **same local SQLite file**:

```bash
PYTHONPATH=src python -m realm --db data/demo.sqlite3 --player Explorer
```

That local Python command is **not Telnet**. A separate loopback-only Telnet
adapter exists under `realm.telnet` (see [`TERMINAL.md`](../TERMINAL.md));
its local access token is not a durable player account. SSH-backed terminal
sessions and GMCP remain future work. Plain Telnet transmits credentials
without encryption; never expose this adapter to untrusted networks.

## Separate worlds

REALM is the generic engine. A versioned worldpack importer exists (see
[`WORLDPACKS.md`](../WORLDPACKS.md)). Any private Driftspace pack must remain
on an authorized private host; it has not been imported or published here.
The public browser client must not ship unpublished canon or author-only secrets.

## Optional live map (integrated gateway v1 route)

The map panel never generates terrain and does not imply that a host supports
maps. It attempts an optional authenticated `GET /api/v1/map?player=...` route.
A 404 may mean the **current location** has no configured map; the client
retries after commands and periodically so entering mapped territory can show
terrain. A 501 marks map support unavailable for the session. Room commands
and the activity feed continue normally; failures never display a fabricated map.

Current response shape, subject to v1 prototype compatibility changes:

```json
{
  "space": { "id": "region-1", "name": "Green Shore", "kind": "region" },
  "width": 3,
  "height": 2,
  "tiles": [
    { "x": 0, "y": 0, "glyph": "~", "terrain": "water", "passable": false },
    { "x": 1, "y": 0, "glyph": "^", "terrain": "forest", "passable": true }
  ],
  "markers": [{ "id": "s1", "x": 2, "y": 0, "kind": "ship" }],
  "legend": [{ "glyph": "^", "terrain": "trees" }],
  "player": { "x": 1, "y": 0 }
}
```

Coordinates start at top-left. The browser limits viewports to 32×32 cells and
64 markers, rejects invalid/duplicate coordinates and control-character glyphs,
and renders every cell through DOM `textContent`. Unsent tiles remain blank;
the client must not infer unexplored rooms or hidden locations. **The server is
solely responsible for filtering terrain, actors, and maps based on the current
player's actual permissions.** The current gateway binds its bearer tokens to
individual player names, but does **not** yet filter undiscovered geography;
the authenticated player may still see author-defined markers they should not
know. The optional map endpoint is implemented in the combined gateway
integration candidate, not by static Pages hosting itself.

The `player` field can explicitly be `null` when the host provides an
authorized viewport that does not contain the player. The browser still renders
the host's tiles, reports **Player outside viewport**, and does not invent an
`@` location. A missing or malformed `player` field is rejected as a protocol
error. Blank cells are announced to assistive technology as unknown or hidden,
not necessarily unexplored.

Security improvement: the browser rejects HTTP host URLs except loopback
(`localhost`, `127.0.0.1`, `[::1]`), even if the page was opened locally.
Remote hosts require HTTPS before sending a token.

### Map validation tests

```bash
node --test tests/browser/test-map.cjs
node --check docs/map.js
node --check docs/app.js
```

These are JavaScript unit/syntax checks, not a real-browser rendering test.

### Browser-to-gateway mock tests

`node --test tests/browser/test-client.cjs tests/browser/test-map.cjs`
checks browser events, authentication header plumbing, authorized map requests,
missing-map recovery after travel, local token DOM clearing, and denial of
cleartext non-loopback endpoints. These tests exercise the actual `docs/app.js`
source inside a controlled simulated DOM and HTTP fetch; they are **not** a
Chromium/Firefox run.

### Live Python gateway protocol test

`node --test tests/browser/test-client.cjs` also starts an ephemeral,
**loopback-only** Python REALM server against a temporary SQLite database. It
builds one explicitly authored **generic** 3×2 map, then drives the actual
browser controller's join, map, movement, disconnect, and wrong-token paths
through real HTTP requests. The subprocess is terminated and temporary data
is removed after the test.

The browser suite includes the null-player viewport compatibility regression
and stale-session cancellation tests. The executed suite includes a live Python
gateway integration test. This is stronger
than independent mocks but **still not a Chromium/Firefox rendering test**, nor
production authentication, remote HTTPS qualification, deployment or publication.
