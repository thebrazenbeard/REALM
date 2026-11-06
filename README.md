# REALM — a small playable MUD foundation

This is an original **terminal + SQLite prototype** of a reusable, standalone text-world engine. It is deliberately independent of any one fictional setting. The previous README was a speculative concept document, not an implemented feature list or binding requirements.

Patrick's intended separation: **REALM** is the engine; **Driftspace** is a distinct private world Patrick can host as a REALM instance. Driftspace content, lore ownership, and player game-state remain separate from REALM's public source. This first vertical slice implements the generic shared-state core, **not yet** a configurable Driftspace worldpack or server.

## What exists now

The current prototype supports:

- Separate persistent worlds: each SQLite file is a world, seeded with three deliberately **noncanonical demo rooms**.
- Player names and persistent positions across restarts. Presence is their **last saved location**, not proof they are currently online.
- Room descriptions and exits; movement; item take/drop/inventory (with transaction-level contention); player presence and shared room history.
- Durable player speech, readable by others with `listen`/`history`.
- Append-only application event records for joins, movement and speech.
- API-level optional action IDs for replay-safe mutation retries; duplicate IDs for different commands are rejected.
- One terminal client per player, using the same database path, with stdlib-only runtime dependencies.

**Not implemented:** network clients, authentication, AI/NPC loops, autonomous offline ticks, inter-world portals, combat, a canonical Wreckforge map, a moderation layer, or distributed state. Do not interpret the prototype as a secure internet-facing server. Local player names are display identifiers, **not authenticated identities**.

There is no live push/notification yet: another player sees your speech by issuing `listen` or returning to `look`. The first goal is reliable shared **state**, not pretending this is already a polished MUD server.

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

One-off smoke command:

```bash
PYTHONPATH=src python -m realm --db data/demo.sqlite3 --player Patrick --once look
```

Run tests without third-party packages:

```bash
python -m unittest discover -s tests -v
```

On Windows PowerShell use `$env:PYTHONPATH = 'src'` before running `python -m realm`.

## Architecture boundary

The first slice is deterministic. Language models are optional future **action proposers**, not authorities over world state. In an eventual agent loop, every proposed action should pass a server-side world validator before being committed and broadcast. The SQLite ledger and room/exit graph remain authoritative for the prototype.

The Wreckforge source repository is proprietary, and its canonical authority is separate. No Wreckforge/Driftspace book text, character, place, or plot belongs in the public REALM engine. Any future Driftspace worldpack should stay private and require explicit, authorized import, with gameplay events separated from novel canon.

This repository currently has **no license file**; until the owner chooses one, don't presume public redistribution or derivative-use rights just because the repository is visible on GitHub.
