# REALM worldpacks, version 1

REALM's public engine is separate from the worlds that people host inside it.
Worldpacks supply initial room descriptions, exits, objects, and the starting
location to an **isolated SQLite instance**. This version does not yet define
planetary terrain, ships, crafting recipes, NPC behavior, or dynamic world
changes. Those require separate, versioned extensions.

## Create your own world, without editing engine code

The offline builder edits only a JSON worldpack file, never a running world or
another player's database. From a checkout, for example:

~~~sh
PYTHONPATH=src python -m realm.builder new --file my-world.json --world-id my-world --name "My World" --start-room square --room-name "Town Square" --description "A road divides the settlement."
PYTHONPATH=src python -m realm.builder room --file my-world.json --id forest --name "Forest Edge" --description "Pines darken the northern path."
PYTHONPATH=src python -m realm.builder exit --file my-world.json --from-room square --direction north --to-room forest --back south
PYTHONPATH=src python -m realm.builder item --file my-world.json --id twig --name "Dry twig" --description "A fallen branch." --room forest
PYTHONPATH=src python -m realm.builder show --file my-world.json
PYTHONPATH=src python -m realm.worldpack --db data/my-world.sqlite3 --file my-world.json
PYTHONPATH=src python -m realm --db data/my-world.sqlite3 --player Explorer
~~~

With the package installed, `realm-build` is an alias for
`python -m realm.builder`. On Windows PowerShell, set `$env:PYTHONPATH = 'src'`
and run the commands using `python` rather than the shell `PYTHONPATH=src` prefix.
`new` refuses to replace an existing file; invalid or conflicting edits leave
the original declaration unchanged. Edits are designed for a **single offline
author**; concurrent file editing, live player building, and migrations are
not yet supported. **Run builder edits one at a time against each JSON file.**
The builder validates and atomically replaces a complete file, but its
read-modify-write sequence does not acquire an interprocess lock. Two
simultaneous `room`, `exit`, or `item` commands can both report success while
the last writer silently overwrites the other's edit. A multi-author frontend
must serialize those operations with a host-owned lock or queue before calling
the CLI; atomic replacement alone does not prevent lost updates. Importing
after players have entered a world cannot silently replace their state. Start
a fresh database for a changed worldpack.

The flagship showcase is deliberately separate from `src/realm` under
`worlds/flagship/`; it is one optional content pack, not a dependency of the
engine or a default world injected into everybody's database.

## Try a generic example

From the checkout:

~~~sh
PYTHONPATH=src python -m realm.worldpack --db data/shore.sqlite3 --file examples/shore-world.json
PYTHONPATH=src python -m realm --db data/shore.sqlite3 --player Explorer
~~~

Once inside, try the look, north, south, take stone_one, and inventory commands.

Use a **new unique database path** for each world. The built-in three-room demo
is discarded *only when it has no players/events/actions and still exactly
matches its original untouched seed*. A previously imported worldpack cannot
be swapped for another pack. An identical import is idempotent and preserves
all subsequent gameplay. There is intentionally no force or overwrite
flag: world-state migration is a different, higher-risk operation.

The importer does not publish, expose, serve, or synchronize the database.
Nothing here configures networking, access control, or a hosted server.

## Shape

~~~json
{
  "schema": "realm.worldpack.v1",
  "world_id": "your-setting-id",
  "name": "Example World",
  "start_room": "start",
  "rooms": [
    {
      "id": "start",
      "name": "Starting Room",
      "description": "Describe a location.",
      "exits": {"north": "next"}
    },
    {
      "id": "next",
      "name": "Next Room",
      "description": "A connected location.",
      "exits": {"south": "start"}
    }
  ],
  "items": [
    {
      "id": "sample_item",
      "name": "Sample item",
      "description": "A portable object.",
      "room": "start"
    }
  ]
}
~~~

- Unknown fields and unknown schema versions are rejected.
- Room IDs, item IDs, world IDs and exit labels are lower-case ASCII names.
- All exit destinations and initial item locations must exist.
- A pack may contain 1–512 rooms and 0–2048 items, with a 512 KiB file limit.
- Non-executable JSON only; no Python, script hooks, remote URLs, AI prompts or
  database instructions are interpreted by this loader.
- The canonical validated JSON is SHA-256 hashed and the digest recorded in
  local world metadata. The digest establishes identity of the imported
  **declaration**, not independent authenticity of its author or sources.

## World ownership and canon

Keep private setting content in its own private worldpack/source repository.
Importing a fictional location into game state does **not** establish it as
novel canon. REALM itself contains only generic demonstration material.

The Driftspace world belongs to its own private host configuration and
Wreckforge's separate literary authority. Nothing in this PR imports or
publishes that property.
