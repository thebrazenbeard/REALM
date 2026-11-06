# Two real local GGUF voices in one persistent Siltglass instance

This is an **explicit, single-run, local owner experiment**, not a public
server, resident scheduler, or authority to change an existing game save.
Every player host can import their own worldpack; this example's setting and
names are **optional content**, not part of REALM's generic architecture.

The entry point `scripts/run_siltglass_npc_duet.py`:

1. Checks both previously downloaded **off-Git** GGUF file sizes and SHA-256
   against the pinned research record. It never downloads anything.
2. Requires an **entirely new**, user-selected SQLite database path outside
   the checkout. Never overwrites or reuses an existing save or identity.
3. Imports only the optional, validated Siltglass Reach worldpack and reserves
   two fresh resident names in a transaction.
4. Calls the owner's `llama-cli` locally, CPU-only, **sequentially**, with a
   hard 60-second timeout per invocation and at most four total turns. The
   Soup06 role uses `--reasoning off` and its model-specific TONE/FACT/OUT
   prompt. Both model responses are untrusted, one-line speech proposals.
5. Lets REALM's `NPCDuetRunner` enforce registered identities, current room,
   host-owned action receipts and `say`-only `ResidentController` commits.
6. Reopens SQLite and independently checks the number, order, actor, room,
   text and receipt count for committed `say` events. It prints a JSON report.

From an up-to-date source checkout on Windows Lappy, choose a **fresh path**:

```powershell
py -3 .\scripts\run_siltglass_npc_duet.py `
  --db 'D:\VERA\realm-worlds\siltglass-reach\npc-duet-unique-trial.sqlite3' `
  --llama-cli 'C:\Users\patri\AppData\Local\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe\llama-cli.exe' `
  --stealth-model 'D:\VERA\models\realm-npc\stealth-rifle-Q4_K_M.gguf' `
  --soup-model 'D:\VERA\models\realm-npc\npc-voice-soup06.Q5_K_M.gguf' `
  --session local_trial_01 `
  --turns 2
```

This command is a **recipe**, not proof it has run; check separately dated
host receipts before claiming live installation. Use a unique database name
for every fresh experiment. The script refuses existing DBs, including
partially completed ones, because a live model timeout or unexpected text
must never trigger blind re-execution against saved state.

For source-only regression with no models or downloads:

```sh
python -m unittest tests.test_siltglass_npc_duet_cli -q
python -m unittest discover -s tests -q
```

## What it does not establish

- An NPC's **speech is not canonical fact**. The underlying worldpack owns
  locations and initial objects; the host ledger only certifies that a
  particular NPC *said* text, not that its prediction or promise is true.
- The two weights are author-published roleplay/voice models, not qualified
  game planners; their raw outputs can hallucinate or reveal thinking tokens.
  Reasoning/template leakage and malformed speech fail closed. This is not a
  substitute for moderation, rights clearance, or multi-turn evaluation.
- This does not maintain goals, learned drives, approved memory summaries,
  background autonomous ticks, social consent, or cross-session natural-language
  recall. It persists **actor profiles, event history and replay receipts**.
- No Telnet/HTTP listener is started, no phone connects, no shared account is
  provisioned, and no endpoint is exposed through Tailscale or the Internet.
- Partial failure can leave an honestly partial database with some committed
  speech, not a magically rolled-back conversation. Inspect before deciding
  whether to preserve it; don't just rerun a new session blindly.

> **Hostile review:** A convincing sentence from a small model may invent a
> new debt, an open crossing, or a guild decision. A durable SQLite receipt
> proves only which words were committed by the host. It cannot transform
> those words into verified truth or independent agency.
