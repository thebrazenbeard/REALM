# REALM offline action-proposal smoke curriculum

Scope: a source-derived **small deterministic sanity check** for an eventual
REALM character-model proposal interface. It does not train, invoke, or prove
learning by any model, and does not use Lappy, resident credentials, production
worlds, or the existing `D:\VERA\realm-npc\demo-20261106` speech duet.

The source of truth for executable action rules is the current local REALM
`Engine` and `ResidentController`. Every scenario has a new disposable SQLite
world. Only the host controls resident identity, policy, action IDs, state
mutation and receipts. Targets use visible object names, not inaccessible DB
IDs; the generator checks that the requested objective actually changed (or
read) authoritative world state as expected and that replay did not alter any
player/item state or total event/receipt counts. A receipt alone can also
record a *failed* action, and is not counted as success here. This remains
evidence about authored fixtures, **not** evidence of inference.

## Regenerate and test ($0, standard Python, no dependencies)

From the REALM repository root, with the repository's `src/` on the import path:

```bash
PYTHONPATH=src python -m unittest discover -s experiments/realm_actions/curriculum -p 'test_*.py' -v
PYTHONPATH=src python experiments/realm_actions/curriculum/offline_curriculum.py \
  --realm-root . --output /tmp/realm-curriculum-unique-output
```

The output directory must not already exist. Five files are produced:

- `train.jsonl`: four labeled examples.
- `dev.jsonl`: two labeled examples for development.
- `test.inputs.jsonl`: **five blind heldout input-only** examples for model use.
- `test.oracle.jsonl`: evaluator-only labels; **never** provide to model.
- `audit.jsonl`: host-execution verification and exact source-head/file-hash
  provenance; **never** provide to model.

One input is `{room,inventory,objective,allowed_verbs}`. The requested
output is `{id,decision,command}`, where `decision` is `act` or `abstain`
and abstention requires `command: null`. Predictions are data, not commands
to run. `evaluate_predictions(records,predictions)` reports heldout exact
match and syntactic/policy unsafe counts **without executing predictions**.

## Limitations and next falsifiers

> This corpus uses three renamed descriptions of the **same three-room demo
> graph**. It cannot prove transfer to unseen topology, language generalization,
> long-term memory, world discovery, autonomy, dialogue, or actual inference.

> Dataset IDs are opaque to prevent direct action names leaking through IDs,
> but five heldout cases are far too small for a reliable capability estimate.
> This generator is source-visible; any developer can regenerate its oracle.
> Separate files are a convenient interface, **not** independent sealed custody.

Next increment needs independently authored/version-pinned worldpacks with
different room graphs and dynamics; frozen objectives; separate training/eval
custody; adversarial invalid exits, contested items, stale observation and
untrusted text; then compare a selected model with rule-only and random
baselines on exactly the same blind input, with explicit run receipts.

This work deliberately does not install or train a Qwen/LLM model, and must
remain distinct from REALM's actual two-GGUF speech-duet runtime. Training
data rights and GGUF redistribution require their own checks.
