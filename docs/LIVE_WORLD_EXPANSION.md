# REALM live world expansion — host seam (draft)

**Status: source-only, HELD by default.** This separate additive transaction
path does not replace the existing worldpack importer: that importer remains
restricted to an unplayed pristine world. No live Driftspace/VN2 data was read,
changed, deployed, or canonized by this feature.

## Interface

- inspect_expansion(engine, proposal) verifies the bound world_id,
  original worldpack hash, exact expected_revision, existing schema and
  identities, and returns a deterministic read-only additive diff.
- apply_expansion(engine, proposal) is **HELD unconditionally** at present.
  An injected approval callback (even one returning True) does not qualify as
  Patrick's release decision. ExpansionHeld is raised without writes.
- An internal _apply_synthetic_transaction test fixture starts BEGIN IMMEDIATE,
  revalidates the invariants, and atomically inserts new rooms/exits/items,
  a ledger receipt and an incremented revision in a disposable test database.
  The fixture rejects a hosted-path claim and verifies PRAGMA database_list
  identifies the same physical SQLite main file before any mutation.
  It is intentionally NOT a public/production authorization mechanism.
- A future host release owner must independently bind exact proposal SHA,
  world ID, original worldpack hash, revision, source provenance, and Patrick's
  canon/source-rights/host decision. Never wire a permissive callback into
  a public route or use Creator staging HMAC as release approval.
- No worldpack rename, fresh reimport, player relocation or restoration,
  rewrites to existing rooms/exits/items/history/receipts, or actor events.
  Existing clients keep their positions and may enter new areas afterward.
- Every query uses main.-qualified SQLite objects and rejects protected TEMP
  table/view shadows. SQLite foreign keys stay enabled through Engine.
- Revision CAS is enforced under the SQLite writer lock; same expansion_id
  and identical payload replay without reapplying, changed replay is rejected.
  Crash/error rolls back entities, ledger and revision atomically.

## Proposal v1

Only exact fields are accepted; IDs use the existing lowercase worldpack grammar.

~~~json
{
  "schema": "realm.world-expansion.v1",
  "world_id": "sample",
  "base_worldpack_sha256": "<original imported worldpack semantic SHA-256>",
  "expected_revision": 0,
  "expansion_id": "chapter_2",
  "provenance": {
    "source_id": "host-verified-original-source-id",
    "source_sha256": "<external source SHA-256>"
  },
  "rooms": [{"id": "cave", "name": "Cave", "description": "A new chamber."}],
  "exits": [
    {"room_id": "shore", "direction": "north", "destination": "cave"},
    {"room_id": "cave", "direction": "south", "destination": "shore"}
  ],
  "items": [{"id": "crystal_1", "name": "Crystal", "description": "A crystal.", "room": "cave"}]
}
~~~

Provenance is a claim transported in the proposal, not independently verified
source-rights or canon release. The host must bind digest, world, revision,
source and actual owner decision before allowing effects. The module never
copies private source material into a public world automatically.

## Synthetic verification

~~~powershell
$env:PYTHONPATH = 'src'
py -3 -B -m unittest tests.test_world_expansion -v
py -3 -B -m unittest discover -s tests -q
~~~

Tests cover two played clients and unchanged action receipts/events/positions,
read-only preview, HELD default and denied callback, exact replay and changed
replay rejection, cross-world/base digest/revision mismatch, item/room/exit
overwrites, duplicate additions, dangling references, true competing writer
CAS, SQLite trigger crash rollback and retry, and TEMP shadow attack.

## Deferred, intentionally

- Actual human/canon/source-rights release decision and cryptographic binding.
- Private Wreckforge/Driftspace extraction, VN2 deployment or backup.
- Changes beyond additive rooms/exits/items, such as modified topology,
  deletion, player migration, NPC population or historical canon revision.
- A host HTTP/player route: this remains a privileged backend seam only.
