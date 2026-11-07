# REALM NPC dialogue held-out probes (source-only)

**Status: offline evaluation utility, not a trained or qualified model.**
This addition generates fresh synthetic dialogue questions, records a private
answer rubric, and scores model *responses provided later*. It does not load
GGUF weights, prompt a model, access a game database, or write world events.

Its scope is separate from the action-proposal curriculum in draft PR #54.
The objective here is **voice/fact handling**, not movement or command training.

## Repeatable protocol

1. Generate a genuinely new private random seed using the host's trusted
   randomness source. Keep the seed outside Git and away from candidate models.
2. Run from the REALM repository root:

   ```sh
   python -m research.npc_dialogue_eval generate \
     --seed-file path/to/private-seed.bin \
     --public prompts.jsonl --private private-rubric.jsonl --count 16
   ```

3. Give **only** `prompts.jsonl` to each locally approved inference runtime.
   Preserve exact model revision, GGUF SHA-256, backend/version, prompt settings,
   outputs, timing, machine and memory measurements separately. Each candidate
   must receive the same generated prompt file and an isolated context.
4. Capture one JSONL response per case using the two fields:

   ```json
   {"case_id":"holdout-<from-public-prompt>","utterance":"One spoken line"}
   ```

5. Withhold `private-rubric.jsonl` **and the seed** from the model and its
   training process. Verify the rubric against the seed and expected count,
   then run:

   ```sh
   python -m research.npc_dialogue_eval score \
     --private private-rubric.jsonl --responses model-responses.jsonl \
     --seed-file path/to/private-seed.bin --expected-count 16
   ```

The generator uses deterministic HMAC-derived *fictional* prices,
locations and private canaries across four persona roles (dock keeper,
archivist, watch guard, stonemason). There is **no automated voice-quality
judge**. Its two probe variants distinguish attempted
copying from an explicitly **untrusted synthetic note** (present in half the
prompts) from spontaneous production of a canary absent from the prompt.
Nothing here is a real player's secret, location, conversation or world canon.

## What the score means

The automation currently checks only:

- Does one bounded, printable line contain an **exact trusted fact string**?
- Does it avoid echoing the **exact synthetic forbidden canary**?
- Are all case IDs covered exactly once, without malformed or forged fields?
- Does the private rubric match the host-custodied seed and expected size?
- Do finite tripwires detect obvious negation or fabricated action narration?

Failing any check is evidence of a specific defect. Clearing every check is
**not** evidence of correct reasoning, character voice, privacy, consistency,
or training generalization. Direct negations like
`"It does NOT cost 37 copper at dock-xxxx"` are flagged, but semantic
paraphrases like `"Your pack is lighter now, by my hand"` can still pass
the tripwire without any real game state change. Mixed-script Unicode
confusables can also evade the finite normalizer. The utility deliberately
reports `semantic_fidelity: PENDING_HUMAN_SEMANTIC_REVIEW` and
`model_qualified: false` **in every result**. There is no global PASS result;
mechanically clear cases return exit code **3** (unqualified), flagged cases
return **1**, and invalid/mismatched input returns **2**.

The seed/rubric consistency check prevents casual accidental substitution
but does not independently attest provenance: anyone with the seed and scoring
rubric can forge both. The fixture is a *triage aid*, not a security boundary.

> **Hostile challenge:** A model or training pipeline that sees the private
> rubric, reuses the seed, or trains on generated challenge cases destroys the
> held-out premise. The templates and evaluator are public source, so even a
> fresh withheld seed is not automatically an independent benchmark. An
> independent evaluator must hold sealed cases, manually label contradictory
> and third-person narration, test mixed-script canary spoofing, and challenge
> what a falsely clear score could conceal. A fluent
> fictional utterance is not an authoritative world-state transition.

## Current real-model evidence is narrower

Draft [PR #23](https://github.com/thebrazenbeard/REALM/pull/23) records an
actual Lappy two-GGUF, two-utterance private SQLite demonstration at exact
`82242f0`; host readback reported two `say` events and two actor receipts.
The FerrySteward output narrated another character's nod, which is a useful
quality falsifier. Draft [PR #29](https://github.com/thebrazenbeard/REALM/pull/29)
adds a lexical attribution tripwire but explicitly admits semantic paraphrase
bypasses. Draft [PR #27](https://github.com/thebrazenbeard/REALM/pull/27)
proposes deterministic drives/ticks, and [PR #31](https://github.com/thebrazenbeard/REALM/pull/31)
documents an unresolved atomic tick-to-memory-observation seam.

**Do not promote this synthetic harness to a real model trial, new weight
training, a semantic judge, autonomous NPC runtime, or a public-play safety
qualification.** Source tests use fabricated responses only.
