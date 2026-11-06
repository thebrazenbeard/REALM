# Optional neural NPC duet → deterministic host ticks

Status: **source-only draft integration**, stacked on PR #33 (atomic memory),
PR #27 (simulation clock), PR #28 (governed actor memory), and PR #23
(host-registered NPC identity/duet). No model weights, background services,
network exposure, or production migration is included.

`ModelNeutralDuetBridge` accepts two **host-selected** `DialogueProvider`
callables and turns their single untrusted `say` proposals into independently
fenced, persisted simulation ticks. The bridge never gives a model SQL, player
principal, receipt IDs, listener choice, worldpack editing, or event-grant
capabilities. Model inference occurs outside a database write lock.

1. A host explicitly imports a **new** private worldpack SQLite database,
   installs `SimulationClock`, registers two distinct names, and obtains a
   local writer lease.
2. The host constructs `ActorMemory` for exactly that database/world ID.
   Each speaker receives only the latest **granted, non-revoked speech from
   its selected listener** as `DialogueContext.previous_speech`; it never
   consumes `Gateway.feed` or unrestricted room history.
3. A provider returns `{"command": "say ..."}`. The bridge rejects other
   verbs, extra fields, non-printable/multiline content, known thinking/role
   delimiters and basic stage directions. **The lexical gate does not prove
   factuality, intent, narrative quality, privacy, or absence of hidden text.**
4. `SimulationClock.plan_next_tick` revalidates the exact host snapshot under
   its SQLite writer lock, including both actors' **last join/move event IDs**,
   and durably records the accepted proposal with a
   host-owned action ID. `apply_pending_tick` rechecks lease, current room,
   both position-event witnesses, listener co-location, and drives;
   event/receipt/memory grants/drive updates
   share one transaction. A crash after planning resumes stored text, never
   reruns a nondeterministic provider for that action.
5. A session ID is bound to the exact world instance and ordered NPC pair.
   Replays read terminal receipts instead of producing extra speech.

## Local GGUF candidate adapter

For the **Siltglass Reach optional example only**, an authorized host can
adapt the existing verified-model experiment in
`scripts/run_siltglass_npc_duet.py`: `local_provider("FerrySteward", ...),`
`local_provider("CrossingInspector", ...)`. They implement the same
`DialogueProvider` signature consumed by `ModelNeutralDuetBridge`. The
existing adapter has model-specific role/stop parsing and is sequential,
offline and local-only; it does not download weights. Its hardcoded actor
names and Tidegate room constrain it to that example. No dependency on these
model files is introduced into the REALM core.

**Host trust boundary:** `ModelNeutralDuetBridge` does not authenticate an
arbitrary provider callable, GGUF file, executable path, inference process,
or completion source. `verify_artifact` checks pinned example model bytes
when explicitly invoked; the existing `local_provider` constructor alone
does not. The existing demo accepts an executable *filename*, not a signed
runtime identity; an operator must independently establish runtime binary
provenance and protect against path replacement before treating the result
as verified local model execution. This draft does **not** claim actual GGUF
inference was run or qualified on Lappy.

## Deterministic qualification (no weights)

```
python -m unittest tests.test_model_bridge tests.test_simulation -v
python -m unittest discover -s tests -q
```

`tests/test_model_bridge.py` exercises two fake providers, durable replay,
crash-after-plan recovery, interleaved third-party speech attribution,
precommit rejection of role/thinking markers, actor spoofing, actor/listener
movement, lease preemption, private-room-history exclusion, revocation, and
session binding. It uses unique temporary worldpack databases.

### Remaining constraints

- First release requires **fresh**, opt-in simulation schemas; the added
  nullable `required_listener` and position-witness columns are not an
  in-place migration of
  existing experimental SQLite saves.
- A committed NPC utterance is evidence **only that the actor uttered those
  words**, not that their claims are true or ethically permissible.
- Real local-GGUF quality/latency, runtime executable provenance, multiplayer
  privacy, offline player consent, host failure under actual hardware load,
  and worldpack-scale simulation remain unqualified.
- Host must reconcile and resolve a rejected pending tick before starting
  more turns. No automatic autonomous loop or self-authorization exists.
