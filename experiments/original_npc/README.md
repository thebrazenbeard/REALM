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
  `ResidentController.validate` / `commit` performs the authority checks and
  database mutation. An optional **trusted** outcome evaluator can feed back
  bounded rewards after commit **only** when a host-owned `receipt_exists`
  witness confirms that the action ID was absent before this commit. Without
  that witness, the action may still commit but learning is not claimed.
- `tests/`: offline contract tests. Their fake resident controller is **not**
  evidence of a running world integration.

The observer, goal, candidate-command enumeration, actor provisioning, reward
function, tick scheduling, receipt allocation and persistent learning-storage
contract are intentionally **host responsibilities**. No resident is created
or scheduled by these modules. Required policy observation fields:
`world_id`, `room`, `exits`, `items`, `inventory`, `goal` (`kind` = `explore`,
`reach` or `take`; optional `target`), and `allowed_commands`.

## Offline check

From this directory:

```sh
python -m unittest discover -s tests -v
python -O -m unittest discover -s tests -q
```

No network access, models, remote inference or GPU is necessary. No model
weights, secrets, or world data are bundled.

## Qualification gates before a host integration

1. Run this adapter against REALM's **actual** `Engine` and
   `ResidentController` in a disposable world; verify real event receipts and
   actor/world isolation, not only fakes.
2. Use a trusted event-time visibility observer and allow only commands the
   engine can currently validate; make each resident's objectives explicit.
3. Compare learned and frozen policies over unseen worldpacks with fixed seeds
   and separate train/evaluation maps. Measure legal action rate, objective
   completion, regret and failed-action loops, including 8 residents × 20
   turns and interrupted/retried turns.
4. Persist learned data and pending action IDs through a host-owned verified
   scheme; current retry caching is only in-process. Serialize each resident's
   action submission across host workers: a precommit receipt lookup is **not**
   an atomic freshness proof if multiple writers race with the same ID.
5. Qualify the trust boundary: model Python running in-process is **not**
   sandboxed. A hostile plugin cannot be made safe by handing it a JSON copy.

An exploratory policy module is not a miniature copy of ChatGPT. Its limited
learning is actual state change in an action-value table, not proof of
reasoning generalization or consciousness.
