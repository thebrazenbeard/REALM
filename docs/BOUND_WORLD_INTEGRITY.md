# Bound world integrity at startup and worldpack retry

## Failure being addressed

An already-imported world has a stable `world_id` and worldpack SHA-256 in
`world_meta`. The prior `Engine` startup seeded generic demo rooms whenever
`rooms` became empty, regardless of that binding. An external damaged writer
could delete the imported rooms, exits and items with SQLite foreign keys
disabled; startup would silently install the three demo rooms **under the
imported world's identity**. A subsequent same-hash `import_worldpack` would
return `unchanged: true` without inspecting authored content.

This is a **reproduced local damaged-database recovery defect**, not an
ordinary SQLite crash, remote exploit, or evidence that the author's save is
corrupt. Acceptance PR #46 is the independent red reproduction using a
disposable Siltglass Reach database.

## v1 recovery contract

1. At startup, an existing bound world with any metadata is checked against
   REALM's current **core table/index definitions before idempotent schema
   DDL**. Missing or modified core SQL objects, including an externally
   dropped events table/index, fail closed rather than being silently restored.
   Extension-owned tables are not rejected merely for existing.
2. With zero rooms, `Engine` seeds the demo **only** when no game metadata,
   actors, events, action receipts, items, exits, or past SQLite event IDs
   remain. A previously bound or used world missing every room raises a
   `ValueError`; it does not silently reseed.
3. A same-semantic-hash worldpack retry validates the **current** world ID,
   schema, title and start-room metadata; the exact authored room and exit
   definitions; and item IDs, names and descriptions. It rejects missing,
   added or changed definitions, invalid SQLite foreign-key relationships, and
   impossible item placement (simultaneously held and in a room, or neither).
4. Only item *placement* is mutable under current v1 gameplay: players may
   take, carry and drop authored items without invalidating an unchanged
   retry. Player rows, event history and receipts are not cleared/replaced.
5. On an authored-row or metadata mismatch in a correctly schemed DB, the
   verification rejects without rewriting game rows. There is deliberately
   no automatic repair, force overwrite, world-ID rebinding, or recovery
   reset. Schema DDL can still occur before detection for an unmarked/legacy
   damaged database where current world binding is absent; forensic review
   is required before treating such a database as safe.
6. A damaged bound-world `Engine` raises the typed `WorldIntegrityError`.
   `import_worldpack` maps **only that specific startup error** to its public
   `WorldpackError` while preserving the exception cause. The CLI exits 2
   with one diagnostic line, never a Python traceback for this case.

## Source/test evidence and limitations

`tests/test_bound_world_damage_acceptance.py` reproduces total loss of
Siltglass authored rooms; `tests/test_worldpack_integrity.py` tests partial
room/exit/item/metadata drift, an orphaned FK after disabled checks, item
   CHECK-bypass corruption after `PRAGMA ignore_check_constraints=ON`, missing
   core events table/index, a one-line CLI rejection, no repair, and legitimate
   carried/dropped items.
   All changes to damaged databases occur
inside disposable temporary test directories.

> **Hostile challenge:** These are application-level **structural checks**, not
> a cryptographic anti-tamper proof. A local database writer can change both
> source data and metadata, and the untrusted on-disk database has no secure
> signature/authority boundary. The check does not certify WAL power-loss
> durability, detect all malformed extension schema objects, prove previously
> deleted event rows were preserved, or prove historical events are honest.
> `Engine` startup can recognize *total* room loss but cannot
> rederive every authored room without a supplied original worldpack: a
> partial topology loss is checked on same-hash import retry, not on every
> generic engine open.

The v1 authored-room/exit/item-definition contract is intentionally immutable
once imported. A future live builder must introduce an explicit signed or
host-authorized world revision/migration mechanism and revise these checks;
silently modifying authored rows and keeping the old worldpack SHA is **not**
currently a valid live-world edit.

If a bound world fails integrity validation, stop serving it, preserve the
original database read-only, and investigate **offline** with its known
worldpack and separate backups. Recover only to a separate new path through a
deliberate operator workflow; do not automatically copy a pristine worldpack
over player progress. No live Lappy files, network listeners, model runtimes,
services, or user saves are modified by this change.
