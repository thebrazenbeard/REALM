# REALM immutable server-mode binding (source-only)

This draft introduces a host-only mode-binding seam, not a live server policy.
Patrick's server choices are `CASUAL` and `HARDCORE`, scoped per world.
World time and rescue deadlines remain authoritative UTC.

## Invariants

- A world must already have a matching `world_meta.world_id` and Engine schema.
- The mode, mortality policy ID/hash, explicit policy version, and configuration
  ID are bound in one SQLite transaction **before the first character joins**.
- Exact retries preserve the first receipt; attempted mode/policy/version/config
  swaps fail. Existing played but unbound worlds require deliberate migration.
- Wrong-world, missing, damaged or restored-before-binding ledgers fail closed.
- Casual mode preflight returns `HOST_AUTH_REQUIRED`. Hardcore returns
  `WITNESS_REQUIRED` without a separately qualified `HardcoreFinalityGate`.
  Even after a fresh external witness-clear it returns `HOST_AUTH_REQUIRED`.
- **No mode preflight result authorizes join, player actions, medical effects,
  resurrection, transfer, deployment, or any production effect.**

The `Engine.join` and Gateway are **not wired** to this interface. They still
need a host-authenticated per-request admission gate and explicit server mode
provisioning. SQLite can be copied or restored: it is **not** an independent
immutable mode witness or backup rollback fence. A host-side protected mode
manifest and independent Hardcore tombstone authority remain required.

## Local source-level verification

```powershell
$env:PYTHONPATH = 'src'
py -3 -B -m unittest tests.test_server_mode -q
py -3 -O -B -m unittest tests.test_server_mode -q
py -3 -B -m unittest discover -s tests -q
```

No paid compute, private Wreckforge source, or VN2 service changes are involved.
