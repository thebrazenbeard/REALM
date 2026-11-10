# Host-invoked resident turns (local-only)

`realm.npc_turn_host.ResidentTurnHost` is a **bounded host orchestration seam**.
It is not a daemon, autonomous scheduler, model training service, trusted
memory component, or authorization endpoint. The trusted host explicitly
chooses pre-joined actors, allowed commands, private actor-specific views,
and stable action IDs.

```python
from realm.engine import Engine
from realm.npc_local_model import LocalChatConfig, LocalChatProposer
from realm.npc_turn_host import ResidentTurn, ResidentTurnHost
from realm.residents import ResidentController

engine = Engine("one-local-world.sqlite3")
engine.join("ExampleNpc")               # host provisioning, not client signup
controller = ResidentController(engine, "ExampleNpc", allowed_verbs=frozenset({"say"}))
proposer = LocalChatProposer(LocalChatConfig(
    endpoint="http://127.0.0.1:5001/v1/chat/completions",
    model="operator-approved-local-model",
))
host = ResidentTurnHost(engine, proposer, max_turns=4)
result = host.run_tick([
    ResidentTurn(controller, "Private, host-selected NPC scene", "tick-00001"),
])
```

This code example has not been executed with real model weights. A local
model server is an independent host prerequisite; automated tests use a
purpose-built HTTP fixture.

## Invariants

- Each explicit host call processes 1–8 residents (default maximum 4), at
  most once per actor even under a case-varied name. There is no timer.
- Every resident must already exist in the same exact `Engine` object.
  A foreign-world controller, invalid later batch entry, malformed view or
  receipt ID is rejected **before any inference or world mutation**.
- Host view length is 1–6,000 printable characters, and host-owned action
  IDs are bounded to 1–128 safe ASCII characters. These are structural
  limits, not proof of view privacy or uniqueness.
- Models propose only `command`. `ResidentController` checks the actor's
  current host policy; `Engine` owns world rules and transaction effects.
- When an actor's receipt already exists, the host reads its committed
  command, revalidates current policy, and returns the engine receipt without
  another model call. This survives an Engine restart using the same world
  database, new controller, and stable action ID.
- If a later proposal fails, earlier committed turns are **not rolled back**.
  Retry using the *same* per-actor logical action IDs to avoid duplicate
  mutation or inference for those already committed.

## Material limitations

> Stable action receipts are not atomic inference reservations. Two simultaneous
> first attempts from separate processes may both invoke the local model;
> Engine prevents conflicting duplicate commits, not duplicated inference
> cost. Multi-process coordination requires a durable host lease protocol.

> A host that reuses an unrelated receipt ID can replay an earlier command;
> one that changes the ID on retry can duplicate an action. Scene privacy,
> lifecycle, actor access control, model CPU/GPU budgets, and tick cadence
> are the owning host's responsibilities, not this module's.

Evidence: `tests/test_npc_turn_host.py` drives real loopback HTTP fixtures,
SQLite events, restart replay, cross-world rejection, command-policy
tightening, actor injection, and partial retry. None of these are evidence
of an installed neural NPC runtime or user-facing deployment.
