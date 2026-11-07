# Experimental REALM PR #52 WorkBridge driver — not installed

This source-only candidate implements **only the ChatGPT Web → WorkBridge →
local stdio transport** lane. It is a proposed replacement for the older
PR #51-bound adapter in [Draft PR #53](https://github.com/thebrazenbeard/REALM/pull/53),
**not** a second MCP game server or plugin package. It invokes the engine MCP
server in [Draft PR #52](https://github.com/thebrazenbeard/REALM/pull/52)
through `python -B -m realm.mcp_server` with the checked-out REALM source.

The PR #52 head tested is
`88a608989d5a0ed854413a580b0b3cf4343be947` (the adapter also passed
against its parent `d8397151a3adc6a2cf102d6a35efbf5152b5651f`). It exposes:

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

- **10/10 isolated tests passed**, including an actual PR #52 stdio process
  against a fresh disposable REALM world. Sending the same speech and the
  same `action_id` twice created one `say` event, not two.
- The driver enforces argument allowlists, rejects wrong-world and wrong-actor
  preflight, rejects contradictory JSON-RPC success/error replies, and does not
  expose the token in normal official-backend responses. A compromised backend
  can deliberately put its own environment token into returned game text; no
  driver-side output filter is a substitute for trusted executable provenance.
- Neither tests nor source establish an installed WorkBridge command, ACL,
  Lappy Windows compatibility, or the ability to discover these tools directly
  inside ChatGPT Web.
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
