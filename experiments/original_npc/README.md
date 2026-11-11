# Original REALM NPC learner — isolated experiment

**Status:** original offline action learner and host adapter; **not** a deployed
resident, language model, or neural checkpoint. Built against REALM `main`
`09fc7cf6fcd8f7826c315d69c045467f27a7c8c7` (2026-11-06).

This experiment answers a narrow question: can a very small, locally created
policy choose legal actions, remember observed movements, and change its
behavior after actual host-confirmed game outcomes without pretrained weights?
It does **not** establish full-game understanding, natural dialogue, or
performance on unseen worlds.

## Components

- `npc_policy.py`: standard-library-only tabular preference learner plus a
  graph of observed room transitions. It proposes one of the host's exact
  `allowed_commands`. Each character should have its own instance.
- `realm_adapter.py`: host-owned turn adapter. A trusted observer supplies a
  *detached* JSON state, the policy proposes a command, and REALM's
  `ResidentController.validate` / `commit_with_receipt` performs host-gated
  transaction commit. Optional learning is allowed **only when the Engine
  reports a newly created receipt from the same SQLite transaction**. A
  legacy controller may still act, but never authorizes learning from a
  precommit receipt lookup.
- `turn_scheduler.py`: bounded, synchronous host-controlled turn loop. Stops
  immediately on completed reach/take goals, objective changes, stalls, and
  turn limits. Does not start a resident service or create characters.
- `state_store.py`: opt-in, host-triggered checkpoint save/restore of the
  per-actor/world policy, including receipt history. Requires a trusted
  private directory; SHA detects corruption, not malicious rewriting.
- `tests/`: contract tests **and real Engine tests** using disposable SQLite
  databases. No live world or ongoing autonomous service is involved.

The observer, goal, candidate-command enumeration, actor provisioning, reward
function, tick timing, receipt allocation and checkpoint authorization remain
**host responsibilities**. No resident is created or background-scheduled by
these modules. Required policy observation fields:
`world_id`, `room`, `exits`, `items`, `inventory`, `goal` (`kind` = `explore`,
`reach` or `take`; optional `target`), and `allowed_commands`.

## Offline checks

From this directory:

```sh
python -m unittest discover -s tests -v
python -O -m unittest discover -s tests -q
```

From REALM repository root, also run:

```sh
python -m unittest discover -s experiments/original_npc/tests -q
python -m unittest discover -s tests -q
node --test tests/browser/test-client.cjs tests/browser/test-map.cjs
```

No network access, models, remote inference or GPU is necessary. No model
weights, secrets, or world data are bundled.

## Qualification gates before a host integration

1. Extend the passed **actual** `Engine` / `ResidentController` disposable-world
   smoke to unfamiliar maps, multi-character interaction, and realistic goals.
2. Use a trusted event-time visibility observer and allow only commands the
   engine can currently validate; make each resident's objectives explicit.
3. Compare learned and frozen policies over unseen worldpacks with fixed seeds
   and separate train/evaluation maps. Measure legal action rate, objective
   completion, regret and failed-action loops, including 8 residents × 20
   turns and interrupted/retried turns.
4. Wire the new checkpoint API and scheduler into an explicitly provisioned
   live host. Checkpoints can now survive restart in tests, but pending
   in-flight decisions and host receipt allocation are still in-process.
   The Engine now determines receipt creation transactionally; hosts must still
   bind the asserted actor/world to the actual connected Engine and serialize
   their checkpoint writers.
5. Qualify the trust boundary: model Python running in-process is **not**
   sandboxed. A hostile plugin cannot be made safe by handing it a JSON copy.

An exploratory policy module is not a miniature copy of ChatGPT. Its limited
learning is actual state change in an action-value table, not proof of
reasoning generalization or consciousness.
