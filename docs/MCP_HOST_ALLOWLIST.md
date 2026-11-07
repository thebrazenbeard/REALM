# REALM MCP host tool allowlist (source candidate)

The local `realm.mcp_server` module is a **stdio** tool adapter, not a public
server. It binds one existing SQLite world and one provisioned character
credential. The process must also receive a **nonempty host-owned tool
allowlist**. The initial ChatGPT pilot admits only:

1. `realm_status`: report the configured world and authenticated character.
2. `realm_look`: inspect the room for a character who has **already joined**.

The adapter advertises only permitted entries in `tools/list` and checks the
same restriction on **every** `tools/call`, even when a client calls a hidden
tool directly. No model-supplied arguments, MCP `_meta`, or tool listing request
can expand the allowlist.

## Host-owned launch configuration

Provide these environment variables to the **fixed executable** through the
trusted host process, without exposing the credential in the model transcript:

| Variable | Requirement |
| --- | --- |
| `REALM_DB_PATH` | Absolute path to an existing host-owned world database |
| `REALM_WORLD_ID` | Exact ID persisted in that database |
| `REALM_PLAYER_TOKEN` | Host-provisioned token bound to one character |
| `REALM_MCP_ALLOWED_TOOLS` | Exactly `realm_status,realm_look` for the initial pilot |

Example host configuration value (not an instruction to start a shared
service):

```text
REALM_MCP_ALLOWED_TOOLS=realm_status,realm_look
```

Omitting the allowlist, setting it to empty, including duplicate or unknown
names, or inserting spaces around names prevents the stdio process from
starting (exit code 2). There is **no wildcard** and no silent fallback to
the entire registry. Direct in-process construction of `RealmMCPBridge`
defaults to the same two-tool restriction.

**WorkBridge/launcher integration contract:** the parent process must explicitly
set `REALM_MCP_ALLOWED_TOOLS=realm_status,realm_look` in the child environment
it constructs, regardless of its own inherited environment. A launcher that
only forwards `REALM_DB_PATH`, `REALM_WORLD_ID`, `REALM_PLAYER_TOKEN`, and
`PYTHONPATH` will now fail startup by design. The launcher must additionally
restrict its own exposed tool dispatch and use a fixed, host-protected config
path. When composing engine and WorkBridge changes, test the full
driver-to-stdio roundtrip and direct denied calls; neither lane's standalone
unit tests substitute for this integration qualification.

The engine still has nine implemented tools. To run existing trusted local
full-game regression tests, the test host **explicitly** opts into all nine
names; this is not the pilot configuration. Do not install that full-tool
configuration in a host exposed to an untrusted model or third party.

## What this does *not* enforce

> **Hostile challenge:** An environment variable is *not* host-enforced if the
> client or agent can replace that variable, choose a different executable,
> run a shell command, select another configuration file, or reach a separate
> unrestricted MCP endpoint. A process-local allowlist is one defense, not
> proof of an end-to-end host permission boundary.

The actual host wrapper must pin the command, executable, environment and
protected configuration; limit filesystem and credential access; and refuse
unlisted tool names independently. The current WorkBridge integration is an
independent lane and must be reviewed and qualified before remote exposure.

`realm_look` performs no intended gameplay mutation but currently passes
through `Engine.act("look")`, which begins a SQLite `BEGIN IMMEDIATE` transaction.
Its potential write-lock contention means **effect-limited** is more precise
than claiming the implementation is strictly database-read-only. It is not a
database sandbox or formal non-interference proof.

The `realm_look` result also contains a `cursor` from `Gateway.cursor()`, whose
current implementation reads the **global** maximum event ID. Consequently,
changes in that cursor can reveal the existence of activity outside the
character's room, without revealing its contents. The allowlist does not
remove this cross-room timing/activity side channel; address it separately
before making an information-isolation claim.

The adapter still depends on a trustworthy database path and parent directory,
valid host token, authenticated character, and separate runtime/transport
authorization. No network publication, player signup, token creation, or
gateway deployment is authorized by this source change.

## Verification

`tests/test_mcp_server.py` covers both the source interface and a real stdio
child process:

- Exact two-tool discovery and successful bound `realm_status`/`realm_look`.
- Every other implemented tool denied on direct `tools/call`, including calls
  not preceded by discovery and calls with forged metadata.
- Unjoined `realm_look` cannot create a character.
- No change to `players`, `events`, or `action_receipts` counts from rejected
  tool calls.
- Missing, malformed, duplicate and unknown host allowlists refuse startup.
- Explicit trusted local full-tool opt-in retains the existing gameplay tests.

These tests demonstrate the engine process's scope enforcement, **not** the
external host's policy, filesystem ACLs or a live ChatGPT/WorkBridge pairing.
