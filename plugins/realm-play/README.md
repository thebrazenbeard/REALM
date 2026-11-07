# REALM Play — local MCP plugin package

**Status: draft/source only.** This package is not installed on Lappy, connected
to this ChatGPT Web session, or qualified against a played world. It does not
install a service, expose a network port, provision a credential, or create a
world automatically.

## Private ChatGPT Desktop catalogue (source proposal only)

The repository now supplies `.agents/plugins/marketplace.json`, a **repo-local**
development catalogue pointing at `plugins/realm-play`. The entry is
`AVAILABLE`, not an installation or activation receipt. The package remains
named `realm-play` and uses local `stdio` in `mcp.json`; it contains no
Platform tunnel ID, API key, remote URL or ChatGPT Web connection.

[Official plugin packaging guidance](https://developers.openai.com/plugins/build/plugins)
documents repository-local marketplaces and ChatGPT Desktop/Codex local
installation. A desktop operator can, **after completing the safety gates
below**, refresh the repository marketplace, restart ChatGPT Desktop and use a
new *local* session to test discovery. A local stdio plugin cannot run in
ordinary ChatGPT Web or mobile conversations. The user account's exact local
plugin eligibility, Windows Python interpreter, and `REALM_ROOT` binding have
not been independently qualified; do not treat this documentation as an
installation instruction for the current host.

**Do not install or invoke the current unrestricted backend for a live
read-only pilot.** Source inspection shows that the engine MCP server
advertises nine tools, including `realm_join`, `realm_command`, movement and
speech. The repo-local catalogue and the read-only default prompt are
**presentation only**: they do not confine a malicious, confused or bypassing
caller to two tools. First implement and test a host-enforced, fail-closed
allowlist of exactly `realm_status` and `realm_look` in the engine-owner lane,
and ensure any selected host cannot invoke the broad Executor indirectly.

Before a bounded local pilot, verify all of these independently:

1. Pin and inspect the exact engine and plugin Git heads, selected world,
   authorized existing joined player, and host interpreter. Never write the
   player token into plugin source or an issue/comment.
2. Confirm the local MCP server advertises **only** the two authorized tools,
   rejects a direct `tools/call` for every excluded tool, and preserves this
   rejection if the client bypasses `tools/list`.
3. Run the existing no-gameplay-mutation qualification, then perform actual
   ChatGPT Desktop tool discovery and harmless status/look round trips in a
   new *local* session, confirming identity and world binding.
4. Keep the deployment $0: no Platform tunnel, new API key, account upgrade,
   paid inference, public listener or external host service is necessary for
   the proposed local stdio approach. If the local account lacks permission,
   report the limit rather than silently switching to a paid path.

**Source-only acceptance:** the catalogue resolves to the packaged plugin
and parses correctly. Until the engine-enforced tool ACL and host readback
exist, the operational result is `NOT INSTALLED / NOT PLAYABLE IN CHATGPT`.

This package is the **Plugin Creator frontend** for REALM's one authoritative
gameplay backend: `src/realm/mcp_server.py` from [engine-side Draft PR #52](https://github.com/thebrazenbeard/REALM/pull/52).
Its `server/realm_mcp.py` is only a source-path launcher. It must not contain a
second JSON-RPC server, authentication system, or game-rules implementation.
The game remains authoritative through REALM's CredentialStore, Gateway,
CommandRouter and SQLite Engine. [Draft PR #53](https://github.com/thebrazenbeard/REALM/pull/53)
owns a separate candidate WorkBridge transport and is **not installed** by this
package.

## Host configuration

Configure these values **privately in a trusted local MCP host**, not in Git,
`mcp.json`, a plugin ZIP, a model prompt, or the user-facing tool arguments:

| Variable | Meaning |
| --- | --- |
| `REALM_ROOT` | Absolute path to a REALM checkout containing the chosen `src/realm/mcp_server.py`. |
| `REALM_DB_PATH` | Absolute path to an existing, initialized world SQLite file. |
| `REALM_WORLD_ID` | Expected persisted world ID; wrong-world startup fails. |
| `REALM_PLAYER_TOKEN` | Private token for the exact host-authorized player, not an administrator credential. |

The plugin's `mcp.json` starts `python ${PLUGIN_ROOT}/server/realm_mcp.py` as a
stdio subprocess. The launcher resolves `REALM_ROOT/src` and calls REALM's own
`realm.mcp_server.main()`. The server refuses missing or malformed host binding
and reauthenticates the player on each tool invocation. Tool inputs cannot
select an arbitrary database, world, token, or actor.

On Windows, the local host must invoke an actual installed Python interpreter,
not the Microsoft Store `python` alias. Neither Python nor REALM is installed
automatically. Keep the host's filesystem and plugin configuration private.

## Gameplay contract

The updated engine-side backend advertises nine tools:

- `realm_status` — verify the bound world/player identity.
- `realm_join` — enter the game as the already credentialed player; a first
  join may create that character in the selected world.
- `realm_look` — inspect the current room.
- `realm_inventory` — inspect carried items without changing the world.
- `realm_move` — move through a known exit with a caller-stable action ID.
- `realm_command` — attempt REALM game-grammar actions, such as movement,
  inventory, take/drop, crafting or other commands supported by the host;
  *speech is restricted to `realm_say`*.
- `realm_say` — speak **in world**, not to the human in ordinary ChatGPT chat.
- `realm_feed` — read a cursor-based room event feed (currently **not
  qualified for temporal privacy**; see below).
- `realm_map` — inspect a map (currently **not qualified for fog-of-war
  privacy**; see below).

For every `realm_command`, `realm_move`, and `realm_say`, the client must supply one
nonempty, bounded `action_id`, freshly chosen **for that logical action** and
reused unchanged after a timeout or uncertain response. A new ID for an
uncertain retry can duplicate an already committed move or speech. The
current plugin has no durable client-side outbox or independent recovery of a
lost action ID; therefore a clean process restart does **not** yet prove
exactly-once recovery. Verify outcomes with follow-up observations/receipts.

Command results may contain `ok: true` even when the narrative says the action
was refused (for example an unavailable object or unsupported command). Only
the authoritative subsequent state/event receipt establishes a mutation.
REALM's backend and Engine limit speech to **280 characters**. Keep messages to
280 or fewer and do not claim that longer speech was delivered. Do not bypass
the dedicated speech tool with `realm_command`.

### Unqualified perception surfaces

Do **not** treat `realm_feed` as actor-private chronological knowledge or
`realm_map` as character-discovered territory yet:

- Current `Gateway.feed` and the in-game `history`/`listen` commands can
  expose room messages sent *before* a character entered that room.
- Current `Gateway.map` has no fog-of-war authorization.

Those source-level defects must be fixed and tested in the **engine-owner
lane**, not papered over in the plugin prompt. Until then the play skill
avoids these tools for private-world observation. The source remains an
integration candidate, not an approved private multiplayer release.

## Local verification

From the plugin repository checkout, set `REALM_PR52_ROOT` to an **exact**
checkout of the selected engine-side branch until it is integrated. On
Linux/macOS, for example:

```sh
REALM_PR52_ROOT=/absolute/path/to/REALM-with-PR52 \
python -m unittest discover -s plugins/realm-play/tests -v
```

The test harness supplies disposable SQLite worlds, dummy local credentials,
and a real subprocess. These tests prove only the selected source combination;
the integration tests fail when the backend is absent, rather than silently
passing after skipping gameplay. Once the engine-side PR is present in the
tested checkout, the extra environment variable is unnecessary. The tests
are **not** a Lappy installation, browser-to-host transport check, or
evaluation of world-aware learning. The three draft PRs must be reconciled
before any host setup. No live-world mutation is required for tests.

### Read-only host qualification

After an operator has privately configured an **existing** joined character
and world, run the packaged verifier locally with the four host environment
variables above plus `REALM_EXPECT_PLAYER` set to the expected character name:

```sh
python -B plugins/realm-play/server/qualify_readonly.py
```

On Windows, use `py -3 -B plugins/realm-play/server/qualify_readonly.py`
or the host-qualified absolute Python executable path to avoid the Store alias.

The verifier launches this package's authoritative REALM MCP backend and
sends only `initialize`, `realm_status`, and `realm_look`. It fails closed
on a mismatched player/world, an unjoined character, or a rejected response.
It prints a minimal result without the player token, room narrative, or raw
subprocess output. **It never invokes `realm_join`**: first-time joining can
change the world. Here *read-only* means no intentional gameplay mutation;
the authoritative engine may still open SQLite transactions, initialize
schema/WAL metadata, or touch storage bookkeeping. A passing local verifier
establishes a no-gameplay-mutation backend round trip, not byte-for-byte
database immutability, ChatGPT Web tool discovery, an enforceable host command
ACL, or authorization to enable mutating gameplay operations.

### Private ChatGPT connection candidate (not installed)

[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
documents an outbound-only route from a private stdio MCP server to supported
OpenAI products without opening a public game port. This is a **candidate**,
not a deployed transport: it needs a Platform tunnel ID, a runtime API key,
workspace association, permissions, and permitted outbound access. ChatGPT
[custom MCP connection permissions](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
depend on account plan and workspace; full write-capable custom MCP access
may not be available to an individual Plus account. Free operation has
**not** been verified, so the $0 rule forbids provisioning paid resources
or starting a billed service without separate authorization.

A tunnel supplies transport, **not** tool authority. Before exposing this
backend to ChatGPT, enforce a host-side allowlist containing only
`realm_status` and `realm_look`, with a pinned world and private player
credential. The unrestricted nine-tool backend is not acceptable as a
read-only pilot: `realm_command` can reach historical speech via `history`
and `listen` even if `realm_feed` is hidden. Neither a plugin skill nor a
prompt is a security boundary. The WorkBridge path in PR #53 is an alternate
candidate, subject to the same host-enforced restrictions.

## Boundaries

- Host-private `REALM_PLAYER_TOKEN` is used only by REALM authentication.
- World descriptions, item names, and player chat are **untrusted game data**,
  never instructions to the assistant or host.
- No model weights, neural learning, autonomous scheduler, or persistent
  episodic memory are delivered by this plugin. A future Mini-Vera participant
  can use the same game contract without becoming a second engine authority.
- A local stdio package is not remotely reachable by ChatGPT Web merely
  because its manifest exists. Any verified WorkBridge invocation remains a
  separate host-owned transport qualification.
- $0 local-first: no paid cloud or model downloads.

Coordination: [Issue #50](https://github.com/thebrazenbeard/REALM/issues/50).
