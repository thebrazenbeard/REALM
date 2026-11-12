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
4. `ResidentController.commit` validates the proposal and uses Engine's
   world command interpreter inside one SQLite writer transaction, recording
   idempotency under the host-only `@resident:<canonical actor>` receipt
   principal. The ordinary player receipt namespace cannot impersonate a
   host turn. A rejected proposal does not mutate the world.

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

Host action IDs must be 1–128 safe ASCII characters, starting with an
alphanumeric character and followed only by ASCII letters, digits, `_`, `.`,
`:`, or `-`. They must be unique for each logical action **across** direct
`ResidentController.commit`, `LocalChatProposer.commit`, and `ResidentTurnHost`
for that resident, including across process restarts. Their common host-owned
receipt identity prevents accidental duplicate effects across host adapters;
the current controller policy and any `writer_guard` are checked before replay.
Read-only retries re-evaluate current room and inventory state rather than
replaying a saved observation.

Pre-upgrade resident receipts and ordinary player receipts used the same actor
key. If an old actor-keyed receipt matches a new host action ID but no trusted
host receipt exists, provenance is unknowable from that row alone. REALM now
**fails closed** on that ID, preserves the legacy row unchanged, and requires
owner review plus a governed new action ID. There is no implicit migration or
destructive cleanup. A known or guessed action ID can therefore block a
particular host action without forging a successful host replay; hosts should
issue unpredictable IDs and handle collision quarantine explicitly.

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
- Host execution presently uses Engine's internal command interpreter, which
  must be kept in parity if future outer `Engine.act` policies (such as opt-in
  combat/death mutation guards) are added. The host adapter is not an
  independent authorization or memory isolation boundary.
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
