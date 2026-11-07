# REALM observation-grounded, untrained action baseline

This tiny baseline is a **control**, not a learned character model. It consumes
only `input.room`, `input.inventory`, `input.objective` and
`input.allowed_verbs`. It produces an `{id, decision, command}` proposal; it
never writes a REALM world or calls `ResidentController.commit`.

Run the tests:

```shell
PYTHONPATH=src python -m unittest discover -s experiments/realm_actions/baseline -p 'test_*.py' -v
```

Make predictions against an **input-only** fixture from the separate REALM
curriculum generator:

```shell
python experiments/realm_actions/baseline/grounded_baseline.py \
  --inputs /tmp/realm-curriculum-unique-output/test.inputs.jsonl \
  --predictions predictions-test.jsonl
```

The prediction command refuses to overwrite an existing file. A separate,
trusted scorer evaluates predictions against oracle labels and REALM receipts.
Do not pass sealed answers, audit records, or post-action database state into
the baseline. Host policy and the game engine, not this script, must decide
whether a proposal may be committed.

## What was demonstrated

- Eight unit-test methods pass, including blocked unsupported verbs, invented
  exits/items, malformed input, and a forged `Exits:` marker embedded in a room
  description.
- On the **known-template** November 7 REALM 11-case curriculum, it produced
  5/5 exact held-out predictions and 0 unsafe proposals. This is a **smoke
  check only**: the baseline author had already inspected the test templates,
  and all test cases share the same seed world's topology.

## What is not demonstrated

- No weight training, online adaptation, autonomous play, model inference, or
  personality persistence.
- No independent model quality or generalization result. Evaluation on novel,
  independently authored world layouts and tasks must be sealed after this
  baseline is frozen, with at least a deterministic planner and frozen-model
  control before assessing a trained adapter.
- No authoritative structured observation envelope. The script parses the
  current engine's display text. It defensively ignores description lines that
  resemble `Exits:` or `Items:`, but a trusted typed observation channel is
  preferable to any text heuristic.

Provenance: originated in isolated offline scratch experiments against source
`77b34d771b48ab1d8aa756fc77c6453a300b8dee`; later retested against
the exact PR #52 parent head, with those results recorded in the draft PR.
