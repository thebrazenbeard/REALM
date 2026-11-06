# Atomic event-to-observation contract (PROPOSED; not implemented)

**Status:** source-review design + deferred failing acceptance test. This
document is **not** evidence that NPC simulation creates memories. PR #27
(`9dc20a43ce8738c1d516b52eecf5292eb4e81f1c`) and PR #28
(`58a0e7a227ebe780a9c411a5af56dfd703734fcc`) were inspected separately;
neither owns their integration. This contract is for the scheduler and memory
owners to implement and review together. It does not authorize model input to
grant observations or consent.

## Reproduced gap

On a fresh imported `shore-example` database using the exact PR #27 scheduler
and PR #28 `ActorMemory` loaded without altering either implementation:

1. Register two co-present deterministic residents, `Moss` and `Ember`.
2. Plan and `apply_pending_tick()` once. Both intents commit.
3. The action ledger receives speech events `(3, 'Ember')`, `(4, 'Moss')`.
4. Calling `grant_latest_event('Moss', 3)` *after* the tick fails with
   `No retrospective grants: source is not the latest committed action`.
5. `grant_latest_event('Moss', 4)` succeeds.

The error is a deliberate no-backfill guard, not a defect in that guard.
Adding a callback **after** `apply_pending_tick()` would miss earlier events;
weakening the guard would permit retrospective entitlement. The deferred test
`tests/test_atomic_memory_integration_contract.py` is skipped on main because
both optional modules are absent, and **fails** when run with the two draft
modules without integration. Do not count its main-branch skip as a pass.

## Minimum proposed transaction seam

For each accepted deterministic `say` intent, within the scheduler's **one
existing `BEGIN IMMEDIATE` write transaction** (already fenced for lease and
epoch), after validating the command and actor/drive revision:

1. Capture the actual emitted `events.id` from `Engine._event`/`_perform` or
   from a verified SQLite insert cursor (not a model-provided ID or unrelated
   `MAX(id)` read after another transaction). Bind exact actor, room, kind,
   payload and action receipt to that event ID.
2. Select host-authorized observer principals and their event-time room and
   consent/visibility state **while holding that same SQLite write lock**.
   Phase-one NPC policy can consider known registered residents in the room;
   human presence must not be inferred from durable `players.room_id` alone.
   Never accept a model-selected witness list or model-written grant request.
3. Validate the source against the memory module's narrow public-kind/payload
   whitelist. Add a host-private `grant_event_in_transaction(db, ...)` path
   that accepts the scheduler's existing `sqlite3.Connection`, never opens a
   nested writer connection, checks exact world and principal authorization,
   hashes the source, and inserts one durable grant per observer/event.
4. Commit **event, observation grants, action receipt, drive change and intent
   disposition together**. Any failed mandatory step rolls all of them back.
   Rejected/no-op intents produce no source event and no grant. Repeated
   idempotent action receipts cannot generate fresh entitlements.
5. `ActorMemory.recall` is the only model-consumable memory surface. The model
   never obtains the host writer, DB connection, observer selection, lease,
   grant/revoke authority or approval role.

**Compatibility notice:** PR #27 currently emits NPC joins with
`{"host_simulation_npc": true}`, while PR #28 permits public join payload
`{}` or `{"resident": true}`. This **fails closed** if a host tries to grant
memory for a simulation join; agreeing on one exact, reviewed public shape is
required before join observations, without accepting arbitrary metadata.

## Acceptance matrix (not executed unless explicitly noted)

| ID | Hostile falsifier | Required behavior | Evidence now |
| --- | --- | --- | --- |
| A1 | Two residents, two speech events in same tick | Both co-present opted-in NPCs can recall each witnessed committed event after tick/restart | **Reproduced failing seam**; future test added |
| A2 | Exception after source insert but before grant | Event, receipt, drive, intent and grant all roll back | **Not tested**; fault-injection required |
| A3 | Crash after commit, then scheduler retry/restart | One receipt/effect and one grant per observer/event; no backfill | **Not tested** |
| A4 | Revoked observation with existing derived summary | Source and summary remain hidden; replay cannot regrant | PR #28 isolated unit tests, **not integrated test** |
| A5 | Observer moved in later, or was in another room | No event-time grant; no retrospective room-position inference | PR #28 isolated unit tests, **not integrated test** |
| A6 | Wrong world / stale lease / stale drive revision | Reject atomically; no cross-world or rejected-intent grant | Isolated tests only, **not integrated test** |
| A7 | Private/unknown event kind or forged extra payload fields | No grant or model-visible memory | PR #28 source whitelist reviewed; **not integrated** |
| A8 | Missing memory schema in memory-required world | Explicit fail-closed decision; no silent event-only commit | **Not implemented** |
| A9 | Humans remain persisted in room while offline or without consent | Persisted location is insufficient for a human witness grant | **Policy missing; do not claim human privacy** |
| A10 | Summaries generated by an LLM | Stored as unverified derivative; cannot establish grant or truth | PR #28 isolated behavior; **not integrated** |

Engine history and gateway room feed remain separate privacy surfaces; an
ActorMemory grant policy is **not** a retroactive access-control system for
those APIs. This design asserts only local in-process SQLite transaction
atomicity, not global distributed ordering or live sensory/presence proof.
