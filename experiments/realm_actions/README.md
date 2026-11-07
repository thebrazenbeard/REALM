# REALM character action-proposal evaluation (experimental)

Two deliberately simple $0 offline research tools live here:

- `curriculum/`: a source-engine-validated 11-case smoke data generator and evaluator with separate input-only test export, labels, and execution audit.
- `baseline/`: a frozen no-weight-change observation-grounded action selector. It reads no SQLite state or answer keys and never commits game actions.

These are **not** shipped model inference, LoRA training, character autonomy, independent blind evaluation, or production credentials. They do not alter persistent REALM worlds; engine actions are tested against a new disposable SQLite database per case. No generated test oracle is checked into source control.

Run from the REALM repository root:

```sh
PYTHONPATH=src python -m unittest discover -s experiments/realm_actions/curriculum -p 'test_*.py' -v
PYTHONPATH=src python -m unittest discover -s experiments/realm_actions/baseline -p 'test_*.py' -v
```

The existing five-case rule-only baseline can match all labels because the tasks reuse the same demo map and constrained command templates; the author had already seen the template definitions. Treat **5/5 as an implementation smoke score**, not out-of-distribution generalization. Future trained and frozen models must face independently authored, genuinely novel worlds under heldout custody, with identical visibility and action budgets.

Do not use a model's proposal as authority. Host validation, replay controls, and authoritative world receipts remain the only route to gameplay mutation.
