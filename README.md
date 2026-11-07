# REALM — Roleplaying Engine for Autonomous Living MUDs

## Product vision (target capabilities)

> REALM (Roleplaying Engine for Autonomous Living MUDs) transforms static text
> worlds into living ecosystems. Replacing rigid scripts with neural agents, it
> features persistent state, dynamic drives, and deep memory. Players explore
> an ever-evolving world, co-creating dynamic history alongside autonomous NPCs
> in real time.

**This is not a list of completed features.** In the
current release, the deterministic engine validates and commits gameplay.
Neural NPCs will eventually *propose* actions, but will never acquire authority
to bypass world rules or write directly to the state database. See
[`docs/ROADMAP.md`](docs/ROADMAP.md) for implemented versus planned milestones.

This is an original **Python/SQLite text-world prototype** with local terminal,
loopback HTTP, loopback Telnet, and a static browser console. It is deliberately
independent of any one fictional setting. The previous README was a speculative
concept document, not binding canon or an implemented feature list.

**Players and creators:** Author your own isolated worldpack, import it into a
fresh SQLite instance, and play with multiple local clients. The setting-neutral
engine does not depend on its example worlds. Inviting players over a remote
network needs a separately qualified encrypted access path; the current gateways
are not safe to expose directly to the internet. Optional AI co-players are
under development, not yet live NPCs in the shipped prototype.

Patrick's intended separation: **REALM** is the public engine; **Driftspace** is
a distinct private world Patrick can host as a REALM instance. Driftspace canon,
lore ownership, and player state are not included in this repository. The
generic worldpack importer exists; a canonical Driftspace worldpack does not.

## What exists now

The current integration candidate supports:

- Separate persistent worlds: each SQLite file is a world, seeded with three deliberately **noncanonical demo rooms**. The loopback gateway persists its `world_id` binding and rejects an attempt to relabel an existing database (see `docs/GATEWAY_PROTOCOL.md` for legacy adoption).
- Player names and persistent positions across restarts. Presence is their **last saved location**, not proof they are currently online.
- Room descriptions and exits; movement; item take/drop/inventory (with transaction-level contention); persisted room positions and event-time-granted room history.
- Durable player speech, readable through `listen`/`history` only by actors granted event-time room delivery (not retroactively by later arrival).
- Append-only application event records for joins, movement and speech.
- API-level optional action IDs for replay-safe mutation retries; duplicate IDs for different commands are rejected.
- One terminal client per player, using the same database path, with stdlib-only runtime dependencies.
- Versioned worldpack validation/import with a generic shore example; existing
  played worlds cannot be silently replaced (see `WORLDPACKS.md`).
- An offline worldpack authoring CLI (`python -m realm.builder`) to create rooms,
  link exits, and place initial items without editing engine code.
- Persistent authored geography (systems, planets, settlements), bounded 2D
  ASCII terrain, weighted routes, and explicit spacecraft ticks.
- Resource gathering in bounded batches, recipes, persistent structures,
  and optional generic crafting demo fixtures.
- Loopback HTTP with **host-provisioned, player-bound bearer credentials**;
  commands, room feeds, and map queries derive the character from the token
  (see `docs/GATEWAY_PROTOCOL.md`).
- A Pages-compatible static browser client under `docs/`, including a live map
  display. GitHub Pages does not run the game server.
- A separately gated loopback-only Telnet adapter (`TERMINAL.md`). Its local
  token is not the HTTP player identity system. The two incompatible identity
  modes now claim their world transactionally: they cannot serve the **same**
  world database without an explicit, offline operator migration.

**Not implemented or qualified:** public TLS hosting, rate limits, public
account registration, expiring sessions, fog of war, AI/NPC co-player loops,
automatic simulation ticks, inter-world portals, combat, moderation,
distributed state, or the canonical Driftspace setting. Local CLI/Telnet names
are not durable authenticated account identities. **Do not expose these
development servers to the public internet.**

The model-free [`ResidentController`](docs/RESIDENT_PROPOSALS.md) now gives a
host one validation boundary for future NPC **action proposals**, but does
not generate actions or provide resident drives, memories, or autonomous ticks.
Small local NPC-model options, including Patrick's Hugging Face `character` and
`npc` searches, are tracked as **research candidates** in
[`docs/NPC_MODEL_CANDIDATES.md`](docs/NPC_MODEL_CANDIDATES.md) and the offline
[`research/npc-model-candidates.v1.json`](research/npc-model-candidates.v1.json).
No weights are bundled, installed or benchmarked by this registry.

The browser uses HTTP polling, not push notifications. Presence still means
saved room position, not reliable online/offline status.

## Play locally (Python 3.11+)

No installation or API key required:

```bash
PYTHONPATH=src python -m realm --db data/demo.sqlite3 --player Patrick
```

In a second terminal, join the same world:

```bash
PYTHONPATH=src python -m realm --db data/demo.sqlite3 --player Vera
```

Try `look`, `north`, `east`, `take token`, `inventory`, `drop token`, `say Hello`, `listen`, `who`, `help`, and `quit`. The next time you join, your position is saved.

For optional noncanonical crafting fixtures add `--demo-crafting` to the CLI
or gateway startup; then try `gather stone`, `craft field knife`, `resources`,
or `build supply cache` after collecting the required materials. The fixtures
are explicitly not Driftspace lore.

One-off smoke command:

```bash
PYTHONPATH=src python -m realm --db data/demo.sqlite3 --player Patrick --once look
```

Run tests without third-party packages:

```bash
python -m unittest discover -s tests -v
node --test tests/browser/test-map.cjs tests/browser/test-client.cjs
```

On Windows PowerShell use `$env:PYTHONPATH = 'src'` before running `python -m realm`.

### SQLite journal and runtime qualification

New worlds use SQLite WAL by default **only when the loaded SQLite release
contains the documented WAL-reset fix**. Otherwise, new worlds use the DELETE
rollback journal; explicitly requesting WAL or reopening an existing WAL world
under an unqualified library fails closed. The programmatic `Engine(path,
journal_mode="DELETE")` option can also select a fresh rollback-journal world.
Reopening that database with `Engine(path)` retains
its existing journal instead of silently converting it back to WAL. Explicit
mode changes on an existing world are rejected; changing a played world's
journal mode requires an offline, separately reviewed migration with a verified
backup. Initial journal selection and schema creation are serialized by a
local OS lock using a persistent `*.sqlite3.init.lock` sidecar. Keep that
sidecar in place: deleting it while processes are running defeats the startup
lock. Active SQLite worlds belong on host-local storage, not network shares.
The existing CLI/gateway do not provide an online journal migration.

Before considering a **durable, concurrently accessed WAL world** for production,
run the read-only loaded-library check:

```bash
PYTHONPATH=src python -m realm.sqlite_runtime
```

Exit `0` means the loaded library's version is in a release line carrying
SQLite's documented 2026 WAL-reset fix; exit `2` means it is not. The command
prints the actual linked SQLite version and source ID. The Engine now enforces
this version gate for WAL access, but passing the version
check **does not** replace concurrency, consistency, restart and online-backup
qualification. The vendor calls the bug rare, not an emergency; it has not
been observed in REALM. See [official SQLite WAL guidance](https://sqlite.org/wal.html#walresetbug).

To create your **own** world, start with the guarded offline builder in
[`WORLDPACKS.md`](WORLDPACKS.md). To try the separate, original 24-room flagship
showcase, see [Siltglass Reach](worlds/flagship/siltglass-reach/README.md).
Each world runs from its **own SQLite file**; sharing a database path is how
local players share *one* world, not how unrelated world owners share state.

## Architecture boundary

The first slice is deterministic. Language models are optional future **action proposers**, not authorities over world state. In an eventual agent loop, every proposed action should pass a server-side world validator before being committed and broadcast. The SQLite ledger and room/exit graph remain authoritative for the prototype.

The Wreckforge source repository is proprietary, and its canonical authority is separate. No Wreckforge/Driftspace book text, character, place, or plot belongs in the public REALM engine. Any future Driftspace worldpack should stay private and require explicit, authorized import, with gameplay events separated from novel canon.

This repository currently has **no license file**; until the owner chooses one, don't presume public redistribution or derivative-use rights just because the repository is visible on GitHub.
