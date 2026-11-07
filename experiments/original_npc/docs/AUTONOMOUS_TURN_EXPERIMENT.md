# Bounded resident turn scheduling (2026-11-06)

This is an **offline synchronous experiment**, not an installed NPC service,
event loop, multiplayer scheduler or neural dialogue model. The trusted host
may call `ResidentTurnScheduler.run()` to perform a bounded batch of resident
actions through the existing `ResidentTurnHost`. The model still cannot write
directly to the engine or bypass `ResidentController`.

## What it does

- Requires a specific host-provisioned `actor_id`, `world_id`, observer,
  resident turn host, and host-owned unique action-receipt allocator.
- Validates observations with the same schema as the original NPC policy.
- Stops **before** an action when a `reach` target is the current room or a
  `take` target is already in the inventory. No automatic `look` transaction.
- Stops on objective changes, a bounded number of non-progressing turns,
  completion, or a hard per-call action budget. It does not sleep or spin.
- Requires host-controlled receipts and checks for duplicates during this
  scheduler object's lifetime. Cross-process ID uniqueness and durable
  custody remain the host's job.

## Evidence

From the REALM repository root:

```sh
python -m unittest discover -s experiments/original_npc/tests -p 'test_turn_scheduler.py' -v
```

The test suite includes disposable **real Engine/ResidentController** SQLite
execution. A resident travels from atrium through corridor to workshop in
two actions, learns two receipts, and then performs **no additional** `look`
or other actions when the goal is already met. Other tests challenge missing
progress, changing goals, world mismatch, duplicate receipt IDs, bad inputs,
and bogus commit results.

## Explicit host responsibilities and limitations

The observed `world_id` is a **host assertion**, not a cryptographic or
database-attested identity; the host must bind it to the controller's actual
Engine instance and resident. The scheduler cannot inspect the controller's
private Engine fields. Use one dedicated policy per resident/world, serialize
same-actor actions across workers, and allocate durable action IDs. The host
must also enforce event-time visibility, room membership, user consent and
rights, player fairness, and outcome verification.

Completion for `explore` has **no objective-specific success predicate** here;
it stops by budget or stalled behavior. Progress is approximated from room
and inventory changes, so speech, world changes, quests, social interactions
and economics need richer host-defined evaluators. Multiple calls to `run()`
are intentionally driven by the trusted host; this is not a background agent.

Synchronous observer checks and subsequent commit are not one game transaction.
If the world changes between observation and action, the controller remains
authoritative for engine rules, but a stale goal may cause one harmless action.
Host scheduling must coordinate these decisions for strict completion timing.
