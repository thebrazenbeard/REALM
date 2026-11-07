# Experimental REALM PR #52 WorkBridge driver — not installed

This source-only candidate implements **only the ChatGPT Web → WorkBridge →
local stdio transport** lane. It is a proposed replacement for the older
PR #51-bound adapter in [Draft PR #53](https://github.com/thebrazenbeard/REALM/pull/53),
**not** a second MCP game server or plugin package. It invokes the engine MCP
server in [Draft PR #52](https://github.com/thebrazenbeard/REALM/pull/52)
through `python -B -m realm.mcp_server` with the checked-out REALM source.

The current PR #52 engine head tested by this adapter is
`345af0e8199b60794150927078c526d0dfcc623d` (Linux tests; Lappy Windows
still last qualified on older engine `a89b64ca9aa8927f2c2f59bc862085569c7d6312`).
It exposes:

- `realm_status`, `realm_join`, `realm_look`, `realm_inventory`,
  `realm_feed`, `realm_map`;
- `realm_move` with explicit full direction and caller-stable `action_id`;
- `realm_command` with mandatory `command` and caller-stable `action_id`;
- `realm_say` with mandatory `message` (1–280 characters) and
  caller-stable `action_id`.

The driver validates the tool and arguments, checks an expected world ID and
actor/token digest in the existing SQLite database before invoking the server,
and passes the token only in the subprocess environment. It does **not**
create accounts, rotate credentials, start a network service, make model calls,
or automatically retry gameplay actions.
Successful responses preserve both `game_text` (human-readable) and
`game_data` (the backend's typed `structuredContent`, including event cursors,
feed events and map fields). Both are labeled untrusted in-world content.
The driver rejects absent, malformed, unsuccessful or contradictory typed data.

## Host-owned configuration, not committed

The following is a structural example, *not a working Lappy config*:

```json
{
  "schema": "realm.workbridge-mcp52.v1",
  "python_executable": "C:\\Full\\Path\\To\\python.exe",
  "realm_root": "D:\\VERA\\AnExistingREALMCheckout",
  "world_db": "D:\\VERA\\AnExistingWorld.sqlite3",
  "expected_world_id": "the-real-bound-world-id",
  "expected_actor": "Vera",
  "token_file": "D:\\VERA\\Private\\vera-player-token.txt"
}
```

The token file is a separate private, host-access-controlled text file
containing the existing provisioned Vera player token. Never put the token in
the request, command-line arguments, GitHub, PR comments, chat, or logs.
The adapter reads the file locally and passes it to PR #52 via
`REALM_PLAYER_TOKEN` in the child process environment. Do not install until
the target checkout, world, character, exact credential source, and host-bound
invocation have been independently qualified.

## Host-pinned, read-only REALM Play candidate (not installed)

`host_pinned_readonly.py` is a **separate, narrow proposed host entrypoint**
for an initial ChatGPT gameplay connection. It delegates to the existing
WorkBridge MCP driver but permits only `realm_status` and `realm_look`, with
empty arguments. Neither creates, joins, moves, speaks, submits an arbitrary
game command, inspects history/feed/map, nor mutates the world. It rejects
every other tool, extra game argument, duplicate JSON key and oversized
payload before it reads the host-private config or token. A test covers an
attempt to override the world/actor via request and process environment.

Unlike the general-purpose driver below, this narrow entrypoint accepts
**no `--config` option** and resolves a fixed `host_binding.json` adjacent to
the *installed* wrapper. The binding uses the configuration schema above and
must be provisioned privately by the host owner. It is excluded from Git.
The wrapper rejects a mismatched actor/token/world during read-only
preflight and checks the returned status world ID and player. The response
still labels `game_text` and `game_data` as untrusted in-world content.
`realm_look` is projected to **only** `ok` and the existing room `text`;
the engine's global event `cursor` and any future metadata fields are
omitted from both result surfaces. `realm_status` is projected to the pinned
world/actor and its text is generated locally from those values, not copied
from an untrusted backend content block. This prevents a metadata cursor
from revealing off-room activity; it cannot sanitize arbitrary words embedded
inside the game's room description without also altering game content.

**Host policy remains mandatory.** The script alone is not an access-control
list: the host must install immutable/protected copies of the wrapper and
driver, a privately owned binding and token file, and a fixed-command tool
allowlisting *only* this wrapper and its request payload. The ChatGPT-facing
principal must have no generic shell/command runner, no option to invoke the
broader driver, no write access to the installed Python files/config/token,
and no permission to select a different command or working directory.
The exact host tool capability and ACL require separate live readback.
Do not publish or install this candidate solely because source tests pass.

An example shape for a fixed WorkBridge invocation, where the host owns the
script path and injects only an opaque generated request value, is:

```text
<protected-python> -B <protected-host-pinned-readonly.py> --request-b64=<game-request>
```

The nonsecret request JSON is either `{"tool":"realm_status","arguments":{}}`
or `{"tool":"realm_look","arguments":{}}`; base64url is transport encoding,
not authentication. Host-only world selection is entirely in the protected
binding. Release gates: protected command + file ACL readback; pinned
world/actor/token verification; unauthorized command/config bypass probes;
real backend and plugin transport tests on disposable world; then explicit
authorization before persistent-world play.

## One-shot invocation

The host starts the script with its **fixed private** `--config` path, then
supplies a single JSON request on stdin or as base64url on the command line:

```json
{"tool":"realm_look","arguments":{}}
```

```json
{"tool":"realm_say","arguments":{"message":"Hello in REALM!","action_id":"sessionA-turn0001"}}
```

For WorkBridge calls unable to close stdin, supply one shell argument
`--request-b64=<unpadded-base64url-of-JSON>`. This encoding is **not secrecy**:
non-sensitive gameplay text in a command argument may appear in host process
listings and tool logs. Do not use that mode for private speech.

### Evidence and unresolved gates

- **11/11 isolated tests passed** on Linux with PR #52 at `345af0e`,
  both normal Python and `-O`, including an actual stdio process
  against a fresh disposable REALM world. Sending the same speech and the
  same `action_id` twice created one `say` event, not two. The current suite
  also asserts typed status/room/feed data and rejects malformed structured
  results. Native Windows requalification of this updated commit is pending.
- The driver enforces argument allowlists, rejects wrong-world and wrong-actor
  preflight, rejects contradictory JSON-RPC success/error replies, and does not
  expose the token in normal official-backend responses. A compromised backend
  can deliberately put its own environment token into returned game text; no
  driver-side output filter is a substitute for trusted executable provenance.
- Before the typed-result update, the WorkBridge driver passed **10/10** normal
  and optimized tests on Lappy Windows Python 3.14.5 against an earlier engine;
  a peer reran **10/10** against engine `a89b64c`. The current typed-result
  revision must be requalified against the same head before claiming a new pass.
- Neither tests nor source establish an installed WorkBridge command, ACL,
  or the ability to discover these tools directly inside ChatGPT Web.
- **World selection remains non-atomic:** preflight and PR #52's startup world
  binding occur before the eventual game command transaction. An adversarial
  DB path substitution can bypass early checks; the engine must guarantee
  world ID at the same commit boundary or bind an immutable file object.
- **Caller responsibility:** an uncertain mutating response must reuse its
  original action ID after investigation; do not mint another ID or silently
  retry. A host-owned durable action outbox is not implemented here.
- Room content and player speech remain untrusted game data. The driver labels
  the text; it does not prove resistance to prompt injection.
- A general-purpose WorkBridge shell tool is broader authority than this
  script. Host configuration and invocation must be genuinely pinned by host
  policy, not merely described as fixed in documentation.

No paid compute, public tunnel, installation, credential provisioning, or
played-world mutation was performed for this candidate.

Run tests:

```sh
python -B -m unittest discover -s tests -v
REALM_PR52_ROOT=/absolute/path/to/checked-out-pr52 python -B -m unittest discover -s tests -v
```
