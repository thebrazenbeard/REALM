# REALM Play — local MCP plugin prototype

**Status: source-only prototype.** This plugin is not installed or connected to
this ChatGPT session. No remote endpoint, tunnel, cloud hosting, or playable
world is created by copying these files.

This plugin is a different surface from REALM's experimental original NPC
learner. It lets a compatible **local** MCP client (for example, a properly
configured Codex runtime) offer real game tools to an agent, while REALM's
existing SQLite engine decides which actions succeed.

## Configuration on the machine running REALM

The server at `server/realm_mcp.py` takes these environment variables from the
trusted local MCP host:

- `REALM_DB` — absolute path to an **already existing** REALM SQLite database.
- `REALM_ACTOR` — the exact character name **already joined** to that world.
- `REALM_ROOT` — absolute path to the real REALM source checkout, required
  when the plugin is installed outside that checkout. It must contain
  `src/realm/engine.py`. An in-repository plugin can locate the root itself.

Do not put personal credentials or private game content in `mcp.json`, Git,
or the plugin ZIP. These environment variables are runtime configuration, not
arguments that a model can select per action. The server must reject missing
configuration rather than silently creating a fresh world or actor.

The portable `mcp.json` uses the command `python` and the path
`${PLUGIN_ROOT}/server/realm_mcp.py`. On a Windows machine where `python` is
only the Microsoft Store app alias, point the local plugin host's command at
the **actual** installed Python executable (or the `py -3` launcher with an
appropriate argument). The bundled source does not set up Python or install
any process itself.

## Local verification

From the REALM repository root, once the server source is present:

```sh
python -m unittest discover -s plugins/realm-play/tests -v
```

The tests run against disposable worlds. Successful tests prove only local
stdio MCP operations and scoped Engine actions; they do not prove tool
discovery in the user's installed Codex/ChatGPT product or Lappy runtime.

## Scope and boundaries

- Tools are intentionally narrow: room observation, inventory, permitted
  movement and speech; no arbitrary SQL, file browsing, actor creation, or
  arbitrary command execution.
- A local stdio process runs with its host user's OS permissions. The caller
  must trust the MCP client, the configured actor, and the local machine.
- Web ChatGPT cannot automatically reach the private stdio process on Lappy.
  Any later network bridge requires an explicit design and verified
  authentication, transport security, actor binding, and deployment approval.
- $0 local-first remains mandatory. No model downloads or paid compute are
  required to run this plugin.

Keep this plugin in review until the server's real-engine tests, package
validation and local host installation checks have been completed.
