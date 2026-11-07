# Optional persisted compartment state (source-only)

`realm.atmosphere_ledger.AtmosphereLedger` adds world-scoped SQLite persistence
to the deterministic in-memory `AtmosphereModel` from draft PR #70.

This is **not** a ship interior schema, playable EVA command, authenticating
gateway, medical outcome model, or continuous timer. The host must supply
validated compartment manifests, actor identity/permissions, and an explicit
per-world initialization after a `world_meta.world_id` binding exists.

```python
from realm.atmosphere_ledger import AtmosphereLedger

ledger = AtmosphereLedger(world_db_path)
ledger.enable(world_id="synthetic_world", model=host_validated_model)  # once
old = ledger.status(world_id="synthetic_world")
result = ledger.apply(
    world_id="synthetic_world", action_id="host_action_1",
    operation="hatch",
    params={"hatch_id": "h1", "actor": "host_authenticated_pilot", "open": True},
    expected_version=old["version"],
)
```

For an event callback already inside a host-owned `BEGIN IMMEDIATE` world
transaction, call `ledger.apply_in_transaction(db, ...)` with that SAME DB
connection. Do not call `.apply()` from inside a clock tick: it would try
another writer connection. A nested savepoint rolls back state **and** receipt
even if the enclosing host catches a late SQL error and commits.

- Exact action-ID repeats return their original JSON result; command changes
  at the same ID fail closed. Fresh stale versions reject. Cross-file and
  cross-world attempts reject.
- State stores exact integer gas/oxygen pressure-volume inventories. Reading
  does not reconstruct pressure through lossy rounding. Restart retains
  pressure, hatches, breaches, occupants, and receipts. Restoration rejects
  over-limit *exact* gas inventories even if integer-truncated displayed
  pressure happens to remain at the maximum allowed value.
- Sample equalization, breach, and exposure semantics remain synthetic and
  setting-neutral. A sealed suit is a nominal protective flag, not modeled
  oxygen supply, heat transfer, mass transfer or canonical Wreckforge physics.
- This subsystem does not authorize operator names from client input. A
  trusted host must bind actors to authenticated principals and enforce
  location, crew rights, hatch/airlock state and server policies separately.
- SQLite file rollback/replacement can roll back version and receipts; neither
  this ledger nor a clock tick is an independent irreversible witness.

Acceptance: disposable-world restart/conservation, concurrent competing hatch
actions, hostile operator/cross-world/connection errors, breach exposures,
receipt fail/partial trigger rollback, malformed state and repeated actions.
No live VN2 world was modified by this draft.
