# Resident action proposals — first host-side seam

**Status:** implemented deterministic host adapter only. No language model,
NPC scheduler, drives, persistent persona or semantic memory has run here.
`realm.residents` is a narrow integration seam for later, optional neural
residents; it does **not** make REALM an autonomous living world yet.

## Authority path

1. The **host**, outside any model response, selects a world database, joins
   the intended resident identity, chooses an allowlist, and creates a
   `ResidentController`. A model cannot nominate another player as its actor.
2. An untrusted model or deterministic planner may propose **only**
   `{"command": "say Hello there."}`. Extra keys, including `actor`, `player`,
   `action_id`, `sql`, or `world_id`, fail validation.
3. The host supplies an independent, stable action ID for the logical action.
   `ResidentController.validate` checks printable text, length, and the exact
   host-allowed verb. Optional item verbs are denied by default.
4. `ResidentController.commit` delegates to `Engine.act`, whose world graph,
   player membership, transaction, and receipt checks still decide the actual
   outcome. A rejected proposal does not mutate the world.

Minimal local demonstration (no inference):

```python
from realm.engine import Engine
from realm.residents import ResidentController

engine = Engine("my-world.sqlite3")
engine.join("Moss")                 # Trusted host registration, not model output.
resident = ResidentController(engine, "Moss")
response = resident.commit(
    {"command": "say The bells are ringing."},
    action_id="host-tick-0001",    # Host-generated, retry-stable receipt.
)
print(response)
```

Use a different `ResidentController` and persistent player identity per NPC;
each actor owns separate action receipts and room position. The controller is
strictly a trusted **in-process** utility. Do not wire it to an unauthenticated
HTTP route, accept model-defined identities, or give a model direct Engine,
SQLite, file-system or host-admin capabilities. Existing local CLI methods are
host-trusted and do not constitute remote player account authorization.

### Non-goals and remaining qualification

- Profiles, drives, long-term/semantic memory, event-grounded recollection,
  simulation clocks, autonomous ticks, dialogue generation, model runtime,
  multi-world orchestration, and economic/action policy beyond the engine are
  **not implemented** by this adapter.
- No sandbox or isolation boundary is created against malicious Python code
  running in the host process. Keep model output as **data** and use explicit
  host-controlled parsing and tool permissions.
- Engine's valid but failed gameplay command, such as `go up` with no exit,
  may still receive a replay receipt; it does not imply a successful action.
- Host policy must further decide safety of conversation, privacy, rate
  limits, turn budgets, player consent, moderation, and whether a resident can
  interact with items or economy. No claims of safe public hosting follow.
- Later models should be compared with event-grounded memory tests, adversarial
  actor spoofing, unauthorized item actions, world isolation, latency, receipt
  replay, and complete restart recovery on exact pinned weights and runtime.
  Weight size is not measured resident performance.

Run the core regression:

```sh
python -m unittest tests.test_residents -q
```
