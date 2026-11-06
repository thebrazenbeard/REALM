# NPC speech fidelity: bounded host-owned checks

Status: **experimental source guard**, not a claim of reliable character
behavior. This document belongs to REALM's engine lane, independent of the
Siltglass Reach worldpack and any particular GGUF model.

## Contract

`dialogue_prompt_parts(context)` returns model-neutral prompt *content* with:

- an explicit speaker, listener, persona and single-sentence/25-word request;
- the **verified host-provided room** name and description;
- the **previous listener speech** as an explicitly unverified quotation;
- no implicit authority for a model to move, create, rename, or mutate actors.

Adapters must apply the selected model's own supported chat template or raw
prompt convention. `dialogue_prompt_parts` does **not** emit ChatML or claim
that a prompt makes model output truthful. Character output is an untrusted
proposal to the host-owned `NPCDuetRunner`.

After `ResidentController.validate`, `validate_npc_speech` uses narrow,
deterministic checks **before** `ResidentController.commit`:

- a `say` command only, at most 25 whitespace-delimited words;
- no leading `*action*`, `[action]`, or `(action)` stage direction;
- no sampled model-control token fragments;
- no obvious `My name is John` / `Call me John` / `I'm John` identity
  substitution when the authoritative speaker has another name;
- no recognized third-person **physical** action narration about the listener,
  such as `The crossing inspector quietly nodded.` or `He nodded.`

Reportive speech (`The crossing inspector said that yesterday.`) is not a
physical action; it remains eligible. Rejected proposals produce no speech
event or receipt. Previously committed receipts replay without re-running a
new provider or retroactively revalidating old speech.

## Deliberately unproved

> **Hostile counterexample:** `The crossing inspector made a motion of
> agreement.` bypasses the lexical guard. `I hear you nodded` could likewise
> refer to a real prior event or fabricate one. General narration, identity,
> factual truth, emotion, intent and believable dialogue require stronger
> evaluation; a regex does not establish them.

> **Speech-versus-world boundary:** a model claiming `I closed the gate`
> cannot itself close the gate. The statement is still a claim, potentially
> false, and should not be promoted to a world event without a host-authorized
> action and committed receipt. Future memory should preserve that provenance.

The grammar of `say ...` is verified by existing engine/controller checks.
The checks here guard only some wording failures; they are not a content
moderation system or a truth oracle. Stop delimiters, token budget, model
identity and clean structured response boundaries belong to the external
model adapter, not the world engine.

## Model-probe observations (2026-11-06)

An earlier Lappy read-only inference probe used separate, existing, privately
installed GGUF files rather than a repository download:

- `cloudunity/stealth-rifle` Q4_K_M (under 1B): with a verbose roleplay prompt,
  the model narrated traveler actions and invented a travel plan. A shorter
  ChatML prompt with first-person assistant prefill produced a first-person
  sentence followed by unsupported items (map, compass, flashlight). This
  output did **not** meet a full autonomous NPC quality threshold.
- `walter-bd/npc-voice-soup06` Q5_K_M (under 1B): using publisher-style
  `TONE` / `STYLE` / `HUMOR` / `RELATION` / `ROLE` / `FACT` / `OUT` prompts and
  `--reasoning off`, it paraphrased the given public slate fact but often
  returned two sentences or wrapping quotes. Its controlled voice-renderer
  behavior is not interchangeable with a general conversational agent.

Those are **model-inference observations**, not a new engine+adapter+DB
integration proof for these source changes. The earlier integrated PR23
two-model host demonstration successfully committed two `say` receipts, but
one was third-person narration; that demonstration predates this guard. Do
not overwrite or reinterpret its database as a post-fix trial.

## Qualification to claim behavioral improvement

1. Pin exact source head, model artifact hash and adapter revision.
2. Use a **new**, private world database and a fresh duet session ID.
3. Confirm both distinct models generate one bounded utterance, and capture
   original structured outputs/stop reasons without treating text as authority.
4. Confirm speech events and replay receipts match, including no commits for
   rejected narration and no created world actions.
5. Evaluate adversarial paraphrases and false positives independently. If
   semantic drift persists, report it instead of relaxing the host boundary.
6. Stop and verify exact temporary local model-server process IDs/listeners;
   do not leave an always-on server as a side effect of a trial.

Tests: `python -m unittest discover -s tests -q` (no external models needed).
