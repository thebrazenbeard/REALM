# REALM worldpacks, version 1

REALM's public engine is separate from the worlds that people host inside it.
Worldpacks supply initial room descriptions, exits, objects, and the starting
location to an **isolated SQLite instance**. This version does not yet define
planetary terrain, ships, crafting recipes, NPC behavior, or dynamic world
changes. Those require separate, versioned extensions.

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
