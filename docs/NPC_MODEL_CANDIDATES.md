# Small local model candidates for REALM residents

**Status: research only — 2026-11-06.** The links and model-card claims below
were inspected from public Hugging Face pages. No weights were downloaded,
installed, run, timed, or qualified by this review. No model is currently
connected to REALM's `ResidentController`, and no neural resident is alive
in a running world merely because a candidate appears here.

REALM's goal is many different persistent characters sharing an optional
**local inference runtime**. Their identities, room access, durable events,
drives and future memories belong to the **world host**, not to one copy of
weights per character. The model may propose speech or an action; the trusted
host validates and commits through `realm.residents.ResidentController`.
Human multiplayer must still work with no model installed.

## Patrick's Hugging Face discovery searches

These are **searches, not vetted model IDs**. Hugging Face rankings and results
can change, and a matching `npc` or `character` name does not establish
fitness, license integrity or actual size.

1. [Character-related text-generation models, 0–1B parameters, sorted by likes](https://huggingface.co/models?pipeline_tag=text-generation&num_parameters=min:0,max:1B&sort=likes&search=character)
2. [NPC-related text-generation models, 0–1B parameters, sorted by downloads](https://huggingface.co/models?pipeline_tag=text-generation&num_parameters=min:0,max:1B&sort=downloads&search=npc)

The short list below adds one useful general-purpose baseline alongside
roleplay-specific and NPC-voice models. The A/B/C/D labels are **role
categories**, not a ranking or experiment order. The JSON registry separately
records a **research-triage order** (soup06 first, Qwen3.5 sixth); neither is
an independently measured quality ranking or an automatic model selection.

## Source-inspected shortlist

| Role category | Model card and published facts | Why test it / what it cannot prove |
| --- | --- | --- |
| **A — structured action baseline** | [ggml-org/Qwen3.5-0.8B-GGUF](https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF), Qwen3.5 0.8B, Apache-2.0 tag; Q4_0 GGUF **563 MB**, Q8_0 834 MB on the publisher page. | Plausible common **text action proposal** baseline. Not an NPC-specialized fine-tune. A current llama.cpp/KoboldCPP build and strict JSON-command tests must establish compatibility; multimodal model metadata is not evidence of a working text-only REALM adapter. |
| **B — expressive character dialogue** | [cloudunity/stealth-rifle](https://huggingface.co/cloudunity/stealth-rifle), Qwen2.5-0.5B-Instruct roleplay LoRA, Apache-2.0 tag; author lists Q4_K_M around **380 MB** and CPU-focused deployment. | Test for distinct character voice and natural scene replies. The author's reported ~30–37 tokens/s on two threads and roleplay craft scores are **not Lappy measurements**. Card warns of continuity/agency slips and mature content; no public/minor-safe deployment without moderation. |
| **C — fact-preserving NPC voice layer** | [walter-bd/npc-voice-soup06](https://huggingface.co/walter-bd/npc-voice-soup06), Qwen3-0.6B SFT/DPO blend, Apache-2.0 tag; GGUF Q5_K_M offered. | A **postprocessor** that turns a *host-supplied fact* into an innkeeper/guard/merchant voice using explicit tone/style/relation/role fields. Author reports **61.5% pass** and **28.5% hallucination failure** on its own evaluation; neither is independent host evidence. A wrong price, quest fact or identity must be rejected, not committed. |
| **C-control — same voice task** | [walter-bd/npc-voice-v5-sft](https://huggingface.co/walter-bd/npc-voice-v5-sft), Qwen3-0.6B LoRA SFT, Apache-2.0 tag, GGUF Q5_K_M offered. | Useful **ablation against soup06** using the same prompts. The author reports 60.0% pass and 29.6% hallucination failure. Not a standalone world planner; model card documents training mixtures including game-dialogue sources whose rights merit separate review. |
| **D — extremely small dialogue research** | [Felladrin/LittleLamb-293M-RP](https://huggingface.co/Felladrin/LittleLamb-293M-RP), **293M**, GGUF-only, Apache-2.0 model tag; Q4_0 **212 MB**, Q8_0 **318 MB**. | Fits tiny runtime experiments. Crucially, **the author says character separation and long-turn consistency are weak**. Their license section says the exact non-PIPPA corpus composition was lost and commercial rights cannot be fully enumerated. **Rights-hold** for product use; research reference only. |
| **D-control — compact general instruct** | [ramankrishna10/npc-nano-0.5b-sft-gguf](https://huggingface.co/ramankrishna10/npc-nano-0.5b-sft-gguf), ~502M Llama-style model, Apache-2.0 tag; GGUF Q4_K_M **333 MB**, Q5_K_M **379 MB**. | Despite its name, the [underlying SFT card](https://huggingface.co/ramankrishna10/npc-nano-0.5b-sft) documents a mixed general-instruction/math/code/identity corpus, **not fantasy NPC training**. A 2,048-token context and modest reasoning make it a budget/identity **control**, not an assumed fantasy actor. |

All numbers in the table are **publisher/model-card statements**, not measured
installation footprint, GPU VRAM use, generation speed or accuracy on Lappy.
Quantized weight file size omits process memory, runtime buffers, KV cache,
multiple active contexts and model-server overhead. An Apache-2.0 repository
tag is not a complete independent audit of training-data rights.

> **Hostile challenge:** A character model fluent enough to tell a convincing
> story may be *worse* at reliable gameplay: it can invent a room, alter a
> quoted price, reveal another player's private events, speak for the human
> player, or fabricate memories. REALM should measure those errors directly,
> never infer correctness from its roleplay style or a model-card score.

## Proposed deployment architecture — not implemented

1. The **host** chooses one approved, locally available and hash-verified
   model artifact. Store weights in the operator's own cache (for Patrick,
   authorized storage under `D:\VERA`), **outside Git** and outside
   published worldpacks. Never download weights just by opening a worldpack.
2. A single local inference server handles requests for multiple resident
   identities. Separate *persona, policy, drives, event-grounded memory
   retrieval and world/room scope* per character; do not duplicate a 0.5B
   model file for every blacksmith, guard or creature.
3. A deterministic host scheduler (future) provides a *limited, authorized*
   current-world view and prompt to the shared runtime. A roleplay/voice
   model may turn **approved facts** into dialogue; a separate action model
   may emit only the strict `{"command": "..."}` proposal envelope.
4. **Host PROPOSE → VALIDATE → COMMIT:** run
   `ResidentController.validate` / `commit` with a host-provisioned actor,
   verb allowlist and host-owned replay ID. No model-controlled SQL, actor
   selection, file access or success certification. The committed event/receipt
   is the record of what happened—not the generated narrative.
5. Future episodic memory references specific authorized event IDs; derived
   summaries cannot silently turn into canonical history or cross a world
   boundary. Model absence/failure degrades to deterministic simulation,
   never a corrupted SQLite world.

This design is compatible in principle with KoboldCPP/llama.cpp and a local
GGUF, but **no runtime/backend interoperability test has been executed here**.
The same tool API should accommodate later providers without coupling the
engine to Hugging Face, Qwen or one fantasy setting.

## Reproducible selection gate

Before saying that any model is a *REALM-qualified NPC*, preserve a receipt
with **exact Hugging Face repo revision, original file name, independently
computed SHA-256, model license and relevant dataset restrictions, model
backend/version and launch arguments, host identity/specs, runtime memory,
context length, exact evaluation source revision, prompts/seeds and outputs**.
Separate measurements from card claims and reviewer opinions.

Run the **same** scripted test corpus for all candidates:

1. Valid/invalid JSON proposal format and injection of extra `actor`,
   `action_id`, `world_id` or `sql` fields (all must be denied).
2. Host-verified action success versus model narration; replay one action ID
   12 times, verify exactly one mutation and receipt.
3. Distinct voices for at least eight original fantasy personas over 20 turns,
   with character swaps and repeated identical world facts.
4. Fact preservation (prices, exits, inventory, reputation), refusal to invent
   unknown information, and response to a genuinely unwinnable world request.
5. World/player privacy: secret neighboring-room events and private dialogue
   must not leak across identity or world switches.
6. Restart recovery: saved actor state and evidence-cited memory survive host
   restart; no prompt text becomes unreviewed persistent authority.
7. Per-turn p50/p95 latency, throughput, peak RAM/VRAM, context growth and
   fairness across many resident requests on the actual $0 host hardware.
8. Content/rights checks for unsolicited adult/violent output, protected
   third-party fictional characters, training-data provenance and host policy.

Only upgrade status **source-inspected → artifact-verified → host-benchmarked
→ integration-qualified** on evidence. None of these six candidates has yet
reached the last three statuses in REALM.

See [Resident action proposals](RESIDENT_PROPOSALS.md) and
[autonomous resident issue #20](https://github.com/thebrazenbeard/REALM/issues/20)
for the implementation seam and next engineering tasks.
