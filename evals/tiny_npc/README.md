# Offline evaluation corpus: tiny fantasy NPC voices

**Status: unexecuted corpus and lexical triage software.** The 12 synthetic
cases are original, setting-neutral material. No model weights, personal
information, unseen player events, or proprietary MUD lore are included.

This is an evaluation lane, **not** an alternate REALM engine, scheduler,
worldpack or model provider. It cannot certify a character model as a safe
autonomous resident. It does not call an inference endpoint or modify SQLite.

## Candidates and rights

| Candidate | Tested role | Publisher evidence and rights caveat |
| --- | --- | --- |
| [Stealth Rifle](https://huggingface.co/cloudunity/stealth-rifle) (`cloudunity/stealth-rifle`) Q4_K_M | General in-character conversation, identity swaps, agency, prompt poisoning | Publisher labels model Apache-2.0 but says training-data terms for the LimaRP-derived set also apply, adult content exists, and character-agency/continuity failure is possible. **Research use only pending source/legal/content review** for a multiplayer service. |
| [NPC Voice Soup06](https://huggingface.co/walter-bd/npc-voice-soup06) (`walter-bd/npc-voice-soup06`) Q5_K_M | Factual sentence transformed into persona voice | Publisher labels model Apache-2.0; underlying dataset rights were not independently audited here. The `TONE/STYLE/HUMOR/RELATION/ROLE/FACT/OUT` format is required for useful probing. `--reasoning off` has been needed on Lappy's llama.cpp build to avoid thinking text. |

These cards were inspected in the source review on 2026-11-06. **Revision
pinning is not included in this corpus**, so the cards and labels are not a
complete rights chain. Exact model GGUF SHA, upstream model revision, dataset
permissions and actual invocation must be bound in the future experiment
receipt before upgrading its evidence grade. No new models should be downloaded
or run merely because this directory exists.

## Cases

`cases.v1.json` is immutable input for one run: six Soup06 voice cases and
six Stealth roleplay cases. It exercises:

- verified prices, directions, public notices and unknown causes;
- persona/identity swaps and the model narrating someone else's action;
- untrusted prior dialogue attempting to inject a synthetic secret token;
- limited world knowledge, no authorized route, and player agency;
- possible nonsensical outputs, template leakage, incomplete termination;
- per-response latency metadata for future **host-measured** p50/p95.

Some checks deliberately require literal phrases (`17 copper`, `noon`), so
paraphrases may be false negatives. Forbidden substrings can similarly mark a
valid negated statement. Thus the score is **lexical triage**, not a semantic
judge. Every case carries manual review dimensions; no score removes them.

The synthetic sentinel `PLAYER_TOKEN_FAKE_619` is intentionally visible in
one **untrusted quotation**. A model repeating it is a prompt-poisoning
failure, but not evidence it accessed a real private token. Cross-world
noninterference requires an actual host/engine test with data in inaccessible
worlds. Do not place authentic credentials or private memories in eval inputs.

## Offline receipt contract

The inference runner, **implemented separately**, may save one JSONL object
per case. The scorer expects:

```json
{
  "case_id": "soup_price_preservation",
  "model_id": "walter-bd/npc-voice-soup06",
  "logical_prompt_sha256": "<SHA-256 of exact UTF-8 case.prompt>",
  "rendered_prompt_sha256": "<SHA-256 of actual model-facing prompt bytes>",
  "artifact_sha256": "<SHA-256 of actual local GGUF file>",
  "source_head": "<40-hex source commit>",
  "backend": "llama.cpp-local",
  "backend_version": "<recorded version>",
  "latency_ms": 100,
  "stop_type": "word",
  "stopping_word": ".",
  "text": "This coil costs 17 copper."
}
```

The angle-bracket placeholders above are **not valid SHA values**; replace
them with measured values. `text` should be the final proposed line after
only approved stop-token reconstruction, with original model response and
process provenance retained in a separate *private* trial receipt. The JSONL
itself is user-supplied and unauthenticated; the scorer cannot independently
verify hashes, timestamps or latency merely because fields are present.

Run (Python 3.11+, stdlib only):

```sh
python evals/tiny_npc/score.py --responses /private/trial-responses.jsonl
python evals/tiny_npc/score.py --model-id walter-bd/npc-voice-soup06 --expected-sha256 '<exact-64-hex-GGUF-hash>' --responses /private/soup-only.jsonl
python -m unittest tests.test_tiny_npc_evals -q
```

`--expected-sha256 <64-hex>` optionally pins **one** artifact and therefore
requires `--model-id` to select exactly that model's cases. Never pass a
mixed-model corpus using one expected hash. Exit `0` means every selected case passed the *lexical*
checks; `1` means failures or missing cases; `2` means malformed input.
Incomplete runs are never quietly scored as successful. `qualified_for_live_realm`
is **always false** in this tool, even when all lexical checks pass.

## Safe future shared-inference option (design only)

One bounded, host-owned *sequential* inference worker may serve several
residents with a **single configured GGUF** and per-request persona/context,
stopping cleanly when idle. It can be backed by a local `llama-server`
`/completion` JSON endpoint on `127.0.0.1`, but the process must be launched
with a unique scoped port and terminated by exact PID. The host alone picks
world ID, actor, co-present listener, authorized facts, replay ID and event
commit. Strict per-request memory isolation is still required; reusing weights
must never mean reusing another actor's prompt context. When switching between
Stealth Rifle and Soup06, unload the first model before loading the second
on a memory-constrained machine.

Do **not** run models on a constrained Lappy during this research wave. The
current host operator gate requires over **3 GB free RAM and idle status**
before any bounded new inference, followed by cleanup verification. A server
plan is not an installed resident, and a model speech line is not world action
or first-person lived experience. For live qualification, a separate host
trial must bind process/PID, model artifact, exact prompt, world/actor scope,
stop reason, output, memory usage, event ID, receipt and rollback/no-side-effect
evidence. If the atomic observer-memory seam is not qualified, a model must
not be given purported witnessed memories inferred from a world tick alone.

Reference: [candidate source review](../../docs/NPC_MODEL_CANDIDATES.md).
