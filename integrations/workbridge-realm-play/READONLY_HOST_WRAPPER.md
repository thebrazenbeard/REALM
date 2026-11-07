# REALM read-only WorkBridge entrypoint prototype

This is a **source-only candidate**, not an installed launcher or a persistent
ChatGPT plugin connection. It uses the neighboring WorkBridge REALM driver
from PR #53 head `6a4642b396c0f36fe7e72246caf966c9a69fbd55`, rather than
introducing a competing production driver.

`realm_readonly_entry.py` only accepts exactly one argument: `realm_status` or
`realm_look`. It constructs the RPC itself with empty arguments, pins the host
config to `private/realm-host-config.json` beside the script, rejects symlinked
config/driver paths, strips ambient API tokens from the subprocess environment,
and returns redacted error codes. It never accepts a world, actor, token,
configuration path, arbitrary command, movement, speech or action ID through
its user-facing interface. A bounded subprocess runs the verified PR #53 driver
and validates an `ok: true`, matching tool, `mutating: false` structured result.
The wrapper checks the exact PR #53 driver source SHA-256 and compares status's
world/actor to its host-owned config. Any driver upgrade requires explicit
hash requalification rather than an invisible source drift.

The host operator would separately create and protect its private config and
token file and verify the world/actor, expected file hashes, interpreter,
Windows ACLs, driver source, full host identity, and WorkBridge command policy.
**Do not use a real player credential as a first qualification.** Start with a
disposable SQLite world; no public tunnel or server restart is needed.

From the REALM repository root, run the host-independent unit tests:

```bash
python -m unittest discover -s integrations/workbridge-realm-play/tests -p 'test_realm_readonly_entry.py' -v
```

The *real disposable-engine integration* test additionally needs a REALM source
checkout with PR #52 present, for example when both changes are in the same
source tree:

```bash
REALM_SOURCE_ROOT="$PWD" python -m unittest discover -s integrations/workbridge-realm-play/tests -p 'test_realm_readonly_entry.py' -v
```

> Security boundary: this launcher checks its own arguments, but the existing
> WorkBridge Commander App still offers generic process execution. Merely
> attaching that App to a REALM plugin cannot turn it into nine game-only
> tools or establish least-privilege host authorization. A separately verified
> host allowlist/proxy would be required for that property.

> A successful read-only trial does not authorize mutation. Move, say, join,
> and generic commands remain forbidden in this version, even if the upstream
> PR #53 driver supports them.
