# 8 × 20 actor matrix — model-neutral host boundary

**Status:** executable disposable-Engine proposal/replay harness. **Not** a
live language-model evaluation, NPC autonomy qualification or world privacy
test. No model weights, endpoints, web services or real secrets are used.

The deterministic corpus contains eight synthetic actors each with twenty
turns, alternating between two completely separate disposable SQLite worlds.
It gives each turn an explicitly host-supplied fictional toll and an
untrusted rumor that contradicts it. Model output is allowed to *speak* a lie;
that never converts the lie into canonical truth. Actual semantic fidelity,
agency and listener behavior remain manual review questions.

Run directly from the repository root, with Python 3.11+:

```sh
python evals/actor_matrix/runner.py --emit-cases
python evals/actor_matrix/runner.py --emit-scripted-control > /tmp/scripted-control.jsonl
python evals/actor_matrix/runner.py --responses /tmp/scripted-control.jsonl
python -m unittest tests.test_actor_matrix -q
```

The control output is scripted and must be labeled `SCRIPTED_CONTROL` in any
report. To evaluate a local model *later*, an independently qualified runner
may present each corpus entry to a model and capture exactly one JSONL reply:

```json
{"case_id":"moss-01","proposal":{"command":"say The bridge sounds quiet tonight."}}
```

The model must **not** choose its `world`, `actor`, `action_id`, credentials or
observer grants. The host fixes all of those. The harness rejects extra
response fields, non-speech commands and synthetic fake-token reproduction,
then submits accepted proposals through REALM's real `ResidentController` and
`Engine` in temporary databases. It verifies host-side actor attribution,
world separation, receipt replay and receipt conflict rejection. The database
files are removed when the evaluation returns.

`host_gate_pass=true` means only that the supplied 160 proposals passed the
tested host-gated disposable flow. It does **not** verify that any model ran,
that response metadata is authentic, that the speech makes semantic sense,
that the NPCs are individually believable, that privacy is enforced against
late entrants, or that a model cannot leak real secrets. A false fictional
toll statement can still pass as authorized *speech*, deliberately.

Future model A/B trials must supply independent measured model binary hash,
GGUF hash, exact rendered prompt digest, process/socket identity, host timing,
per-actor context/audience grants, and a human/independent semantic review.
Do not promote this report to `FORMAL_VERIFY` or production readiness.

This complements existing 12-case lexical triage in draft PR #36. It exercises
the authoritative host commit seam and is not an alternate text classifier.
