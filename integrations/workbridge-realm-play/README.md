# REALM WorkBridge MCP driver candidate (private, not installed)

This is a host-side transport shim for REALM's already-authored local MCP
backend in [Draft PR #51](https://github.com/thebrazenbeard/REALM/pull/51).
It is not a second MCP server, a plugin package, a new gameplay engine, or a
public gateway. No Lappy state was changed to develop this candidate.

The driver launches the configured existing stdio backend once for a single
`tools/call`, verifies `initialize` and `tools/list`, returns one JSON result,
then exits. A WorkBridge-invoking chat supplies one request on stdin; no token,
SQL, worldpack, or arbitrary shell command can be selected in that request.

## Private host configuration

On the host, create a private JSON configuration (do not commit it):

```json
{
  "schema": "realm.workbridge-mcp-transport.v1",
  "python_executable": "C:\\Absolute\\Path\\To\\python.exe",
  "realm_root": "D:\\VERA\\Path\\To\\REALM",
  "server_script": "D:\\VERA\\Path\\To\\REALM\\plugins\\realm-play\\server\\realm_mcp.py",
  "world_db": "D:\\VERA\\Path\\To\\ExistingWorld.sqlite3",
  "expected_world_id": "the-existing-bound-world-id",
  "actor": "Vera"
}
```

The listed `actor` must already be joined. The world database must exist and
its `world_meta.world_id` must equal `expected_world_id`. The client makes a
read-only preflight before starting the backend; it never creates a world or
character. Nothing should be installed until PR #51 owner releases an exact
server revision and a private host deployment is authorized.

Invoke the Python script with `--config` and a single JSON request via stdin,
for example `{"tool":"realm_look","arguments":{}}`. A mutation request can
use `realm_move` with `direction` or `realm_say` with `text`. For one-shot
WorkBridge process calls that cannot close stdin, `--request-b64` accepts the
*unpadded base64url encoding* of that JSON request. This encoding is **not
encryption**; arguments can be visible in host process listings and tool logs.
Use it for non-sensitive game actions, not private speech. Neither transport
automatically retries any action.

Use `--request-b64=<encoded-value>` as **one argument** rather than two
tokens. The `=` is necessary when the encoded string starts with `-`, so
`argparse` does not treat its value as a different option. Bind the private
`--config` path in the host-owned WorkBridge invocation, never in model
controlled tool arguments.

## Evidence boundaries and blockers

- Nine unit tests use a fake stdio backend and disposable SQLite tables.
  A tenth optional integration test invokes the *actual* PR #51 stdio MCP
  server against a newly-created disposable REALM world; it is activated only
  when `REALM_PR51_ROOT` points at the matching local PR #51 checkout. It
  deliberately sends `realm_say` twice to exhibit the replay gap. No valued
  world is touched.
- PR #51 currently generates a new random action ID on each move/say. The
  backend is **not yet qualified for replay-safe retries**; an ambiguous
  failure may have committed. The driver therefore never retries or claims
  exactly-once behavior.
- Read-only world/actor preflight and the later tool call are not atomic. An
  independent adversarial test replaced the DB between checks and committed
  speech in the wrong world. Bind world identity at the server commit boundary
  before installation; this driver alone cannot guarantee that binding.
- The executable, script, actor, and database are protected only when the
  host pins the WorkBridge command and private config ACL. This source-only
  draft does not establish that a restricted invocation has been installed.
- MCP stdout is treated as untrusted game text. A ChatGPT caller must treat
  room descriptions and player speech as data, never as tool instructions.
- WorkBridge is a private host connector. This driver contains no network
  server, tunnel, public URL, API key, login flow, token, or paid service.

Run tests with:

```shell
python -B -m unittest discover -s tests -v
REALM_PR51_ROOT=/absolute/path/to/checked-out-pr51 python -B -m unittest discover -s tests -v
```
