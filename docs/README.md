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

## Compatible REALM gateway contract (v1 preview)

The browser is designed for the independent gateway branch; until the gateway
is connected and tested, the live display is **not qualified**.

| Method | Route | Contract |
| --- | --- | --- |
| GET | `/api/v1/health` | `{ "ok": true, "world_id": "..." }` |
| POST | `/api/v1/join` | Request `{ "player": "..." }`; response `{ "text": "...", "cursor": 0 }` |
| POST | `/api/v1/command` | Request `{ "player": "...", "command": "look", "action_id": "..." }`; response `{ "text": "...", "cursor": 0 }` |
| GET | `/api/v1/room?player=...` | `{ "text": "..." }` |
| GET | `/api/v1/feed?player=...&after=0` | `{ "events": [{ "id": 1, "kind": "say", "actor": "...", "text": "...", "room": "...", "at": "..." }], "cursor": 1 }` |

All routes except health require `Authorization: Bearer <token>` in the HTTP
header. The page accepts the token through a password input and holds it only
in memory: it is **never** embedded in the URL, repository, or browser storage.
The gateway must still enforce authentication, permissions, and exact-origin
CORS. **A token in a browser is accessible to the local user and browser
extensions**; do not regard this design as hardened internet deployment.

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

That is **not Telnet**. Telnet, SSH-backed terminal sessions, and GMCP support
are future server-side adapters that should share the same validated command
engine as the browser client. Plain Telnet transmits data without encryption;
do not put authentication tokens through it on an untrusted network.

## Separate worlds

REALM is the generic engine. A private Driftspace worldpack can be loaded by a
private host when a governed worldpack interface exists. The public browser
client need not ship the worldpack, unpublished canon, or author-only secrets.

## Optional live map (not yet a gateway v1 route)

The map panel never generates terrain and does not imply that a host supports
maps. It attempts an optional authenticated `GET /api/v1/map?player=...` route.
A 404/501 marks the route unsupported for this browser session; room commands
and the activity feed continue normally. Transient failures show an unavailable
state instead of stale or fabricated terrain.

Proposed response contract, to align with the server's geography module:

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
player's actual permissions.** A shared bearer token is not player identity.
The map endpoint itself is proposed, not implemented by this client PR.

Security improvement: the browser rejects HTTP host URLs except loopback
(`localhost`, `127.0.0.1`, `[::1]`), even if the page was opened locally.
Remote hosts require HTTPS before sending a token.

### Map validation tests

```bash
node --test tests/browser/test-map.cjs
node --check docs/map.js
node --check docs/app.js
```

These are JavaScript unit/syntax checks, not a real-browser end-to-end host test.
