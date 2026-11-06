# Owned local REALM worlds — first isolated builder boundary

**Status:** implemented *host-only, offline* world creation and per-world
capability check. This is not an authenticated multi-user authoring server,
public MUD host, live online construction (OLC), or remote login mechanism.

## Why the boundary matters

REALM is reusable engine code. Each self-hosted world has its own user-authored
worldpack and own SQLite game database. The flagship Siltglass Reach pack is
example content, not a special engine schema or a world every host must run.
One host may operate several instances; two hosts may use the same fictional
`world_id` without sharing files, players, inventory, NPC memory or events.

The existing `python -m realm.builder` can edit **any offline file the local
OS user is allowed to edit**. It is not a server-side permission system. CLI
character names, HTTP player tokens, local Telnet session names and a chosen
`world_id` do not grant world ownership. Hosting and player ingress remain
separately security-gated.

`realm.hosted_builder.HostedWorlds` is a small opt-in local host manager:

| Artifact | Per-world path beneath a host-owned root | Authority |
| --- | --- | --- |
| Authored declaration | `<root>/<world_id>/worldpack.json` | World owner capability required for managed edits. |
| Owner metadata | `<root>/<world_id>/owner.json` | Schema/world ID and SHA-256 digest of high-entropy owner capability; never plaintext token. |
| Authoritative game state | `<root>/<world_id>/world.sqlite3` | Separate SQLite instance per world, created only by explicit provision. |

The manager validates lower-case world IDs, rejects path traversal, checks
for symbolic link replacements at API boundaries, and requires the matching
opaque capability before any edit, description or instance provision. Denial
does not create a database, touch worldpack bytes or disclose another world.
Once an instance is provisioned, managed worldpack edits are refused instead
of silently rewriting live rooms. Same-hash import retries remain idempotent.

## In-process host example

Use this from trusted **local operator code only**. Store the returned secret
in an operator-owned secret store outside Git, logs and screenshots. There is
currently no safe CLI token-storage/recovery interface.

```python
from realm.engine import Engine
from realm.hosted_builder import HostedWorlds

worlds = HostedWorlds("D:/REALM/local-instances")
owner_capability = worlds.create(
    world_id="my_forest", name="My Forest", start_room="clearing",
    room_name="Forest Clearing", description="A spring flows through ferns.",
)
# Store owner_capability privately; it is only returned on initial creation.

worlds.add_room(
    "my_forest", capability=owner_capability, room_id="grove",
    name="The Grove", description="Old trees shelter a moonlit path.",
)
worlds.link_rooms(
    "my_forest", capability=owner_capability, source="clearing",
    direction="north", destination="grove", back="south",
)
worlds.provision_instance("my_forest", capability=owner_capability)
engine = Engine(worlds.instance_path("my_forest", capability=owner_capability))
print(engine.join("Explorer"))  # Local engine only; does not start a server.
```

Different host roots are physically independent; the same `world_id` in two
different roots does not designate a shared world. The worldpack importer
retains its exact schema/pristine-state checks; no general database migration
or import-into-a-running-world is introduced here.

## Security limits that are **not** solved

- `HostedWorlds.create()` is intentionally **host-privileged**. Never expose
  this class or generic filesystem paths directly as untrusted HTTP methods.
  Future online creators require authenticated accounts, explicit owner/role
  grants, path-independent API handles, quotas, moderation and audit.
- This Python API cannot constrain code with arbitrary local OS filesystem
  access. A person who can rewrite `owner.json`, replace directories or modify
  a world DB can bypass these checks. The host must protect root and child
  directories with operating-system permissions/ACLs; POSIX creation modes
  are not Windows ACL qualification. Symbolic-link checks are defensive but
  do **not** eliminate local attacker-controlled race windows.
- The owner capability is high entropy and digest-stored, but possession of
  its plaintext value grants all managed offline edits for that world. No
  rotation, recovery or network session management exists yet. Loss of the
  secret requires separate operator-controlled recovery work.
- This first managed builder is single-author **offline**. Concurrent
  read-edit-write on one worldpack has no per-file compare-and-swap guard; do
  not allow multiple writers or call it an OLC transaction system.
- After provisioning, ordinary engine player commands use their own
  auth/rules and cannot invoke managed edits. A host can choose to run several
  local Engine instances, but this module does not start listeners, assign
  ports, configure Tailscale, provide TLS or qualify remote connections.
- It does not create NPC autonomy, memory, guild rights, PvP or licensed MUD
  assets; those are independent gameplay/worldpack lanes.

## Acceptance evidence and future gates

Run the bounded host-only tests:

```sh
PYTHONPATH=src python -m unittest tests.test_hosted_builder -v
```

The tests check two distinct worlds under one host, two separate host roots
with identical `world_id`, independent SQLite item/event state, capability
denials before mutation, failed path traversal and symbolic-link escape,
owner digest persistence after manager restart, and post-provision edit
rejection. These are local source tests, **not** multi-device or live service
proof. Future work needs actual Windows ACL checks, atomic online builder
edits, role-specific view/search/traverse/edit rights, credential rotation,
remote two-client tests and an authenticated private TLS endpoint—under a
separate owner-approved deployment lane.
