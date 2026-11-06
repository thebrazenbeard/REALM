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
