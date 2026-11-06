# Living-world engine: host-owned simulation, not model-owned canon

**Status:** proposed next-phase contract, **not an implemented or executed
simulation**. Source baseline for this proposal:
`16576a1e59b39c8a86f98f44b64308bf0b0e50b0` (independent local
`agent/realm-living-world-spec-20261106` branch). This document is design
review evidence only. No scheduler, faction system, durable NPC drive store,
semantic-memory service, or neural runtime is qualified by its presence.

REALM is a self-hosted, text-only multiplayer engine. Its autonomous residents
should alter the *same persistent world* human players inhabit, while the
host's validated event ledger—not an NPC's narration—determines what happened.
Each SQLite database remains one world's authority; worldpacks are optional
data, not engine dependencies. Local models are optional and must not make
the deterministic server depend on a downloaded model or paid service.

## What exists versus what is proposed

| Evidence class | Present implementation | Remaining contract |
| --- | --- | --- |
| SOURCE INSPECTION | `Engine` keeps rooms, players, items, event rows, and `(actor, action_id)` receipts in one SQLite file; each `act` serializes its transaction with `BEGIN IMMEDIATE`. | No simulation-clock or tick-intent transaction exists. |
| SOURCE INSPECTION | `ResidentController` accepts only `{"command": "..."}` and uses a host-selected actor, verb allowlist, and receipt ID. | No host drive state, memory projection, model executor, or schedule exists. |
| SOURCE INSPECTION | Events contain integer ID, room, actor, kind, payload and wall-clock occurrence. | No visibility/knowledge entitlement ledger or per-actor memory materialization exists. |
| SOURCE INSPECTION | `Engine` creates/connects to one database file and uses SQLite WAL. | SQLite serializes writes, but does **not** by itself elect one active simulation scheduler across processes. |
| PROPOSED | A bounded, persisted logical tick drives NPC intents and deterministic non-neural changes. | Requires implementation, migration, crash recovery and executed tests. |

The current event ledger is append-only **at the application layer**, not
cryptographically immutable or multi-host consensus. Never advertise it as
tamper-proof, authoritative across replica hosts, or independently verified.

## 1. Boundaries and deployment shape

```text
optional local model (untrusted suggestion)     deterministic planner (fallback)
                   |                                     |
                   +------------ proposals --------------+
                                         |
                              host policy + validator
                                         |
                               tick/action coordinator
                                         |
                              single authoritative Engine
                                         |
                              one world SQLite database
                                         |
                             audited events + receipts
                                         |
                   authorized per-actor observation/memory
                                         |
                            player clients / resident input
```

- **Host:** owns world DB choice, scheduler lifecycle, NPC identity, policy,
  drive coefficients, memory visibility, receipt generation, time budgets,
  validation and commit. No model receives direct SQL, filesystem, networking,
  token minting, admin or builder capabilities.
- **Worldpack:** supplies versioned setting data and optional initial NPC
  profiles. Engine imports into a fresh, explicitly selected DB. The flagship
  Siltglass Reach content must be removable without breaking the engine.
- **Transport:** human clients remain untrusted viewers/command submitters.
  The existing HTTP/Telnet listeners are loopback prototypes, not qualified
  Internet or LAN multiplayer endpoints. The Android client is a separate
  presentation lane.
- **Inference:** no weights in git, no startup requirement to load a model,
  no paid APIs or cloud compute. A deterministic planner must always be able
  to advance a world in a bounded, testable way with inference disabled.

## 2. Deterministic logical ticks and recovery

The scheduler is a **single active host writer per world**. A logical tick is
`(world_instance_id, tick_seq, scheduler_version, ruleset_version)`, not
`time.time()` or a model's estimate of elapsed time. Persist its cursor in the
world DB. Wall time is metadata only, not a source of nondeterministic game
outcomes. Existing `Engine._event` does stamp real UTC time; deterministic
fixture comparisons must explicitly exclude that metadata or introduce a
separate logical occurrence field. A host-controlled cadence may be
configured; backlog after downtime has a bounded catch-up limit and an
explicit paused/skipped-tick record.

Proposed host transaction and recovery rules:

1. Acquire a validated single-scheduler lease/ownership fence bound to the
   world instance and epoch before work. SQLite write locking alone is **not**
   a leader election protocol. Startup must fail closed if competing host
   ownership cannot be established. No cross-machine shared-folder SQLite
   authority is presumed safe.
2. In a short DB transaction, snapshot the authoritative room, participant,
   permission and drive versions; calculate bounded eligible residents in
   deterministic order; durably record `tick_seq` and immutable intent IDs.
   No external model call may hold a SQLite write lock.
3. Produce proposals outside the transaction from a deterministic fallback or
   an optional time-limited model. Seed any stochastic deterministic planner
   from a persisted stable seed plus tick/resident IDs (never Python's
   randomized `hash()`). Model text may vary; **only committed host effects**
   are authoritative and replayable.
4. Validate the exact intent against current state and policy immediately
   before commit. Changed room, permissions, target ownership, epoch or
   state version invalidates the proposal. Reject stale intent; do not silently
   reinterpret it as another action.
5. Commit through the Engine's transactional command path using a host-derived,
   immutable `(actor, action_id)`. Existing receipts deduplicate identical
   actions; a mismatched retry must fail, including after process restart.
   Record a durable tick disposition (`committed`, `rejected`, `expired`,
   `no-op`, `provider-error`) so the coordinator cannot regenerate a *different*
   action under the same intent ID.
6. Advance the durable tick cursor **only after** all required intents have
   terminal dispositions. Recovery must resume outstanding intents with their
   original IDs and proposals, not recompute a new plan. Prefer an atomic
   coordinator/Engine write path for cursor and effect. Where existing
   `Engine.act` creates a separate transaction, the design must demonstrate
   crash-safe reconciliation using its action receipt before declaring the
   tick complete.

Keep concurrency budgets small and explicit: maximum residents per tick,
proposals per resident, prompt/output bytes, model wall-clock timeout, SQLite
busy timeout and total catch-up ticks. A slow model becomes a recorded no-op,
not a frozen world or an unbounded queue. Human commands continue to use
ordinary Engine transactions and can invalidate stale NPC proposals.

## 3. Drives, decisions and emergent behavior

Each host-provisioned resident has a persistent, versioned drive vector:
`survival`, `shelter`, `hunger`, `social`, `curiosity`, `duty` and
`faction_loyalty` are illustrative **schema candidates**, not claims of
biological experience. The host defines bounded numeric ranges, decay and
replenishment per ruleset. Drive updates must be deterministic functions of
tick ID, validated observable event IDs, current resources and ruleset
version; model prose cannot directly update them.

The planner considers only feasible, host-authorized actions: room topology,
line-of-sight/knowledge, current item ownership, cooldowns, conflict policy,
per-resident action budget and safety/moderation constraints. Stable ordering
breaks equal scores. A model may write dialogue or suggest one candidate but
cannot choose its own weight, goal, privileges, actor, target visibility or
authoritative outcome. Keep model-free fixtures for all behavior tests.

Meaningful autonomy should emerge through **persistent consequences** and
conflicting drives, not through an NPC repeatedly asserting that it is alive.
Measure initiative, diversity, consistency, memory fidelity and failure
recovery against fixed scenarios before using words like "living ecosystem"
as a verified runtime property.

## 4. Event-grounded memory with per-actor visibility

Use two distinct layers:

- **Immutable source reference:** canonical `events.id` plus DB/world identity,
  event kind, observed actor, room, ruleset and observation/visibility version.
  Store the provenance and the resident's observation entitlement at the time
  of capture. An NPC must not learn events from another room merely because a
  global event table contains them.
- **Derived memory:** bounded per-resident episodic records citing one or
  more eligible events, optional confidence/summary, and retention policy.
  Retrieval filters by resident/world/visibility and returns source IDs with
  snippets; summaries are search aids, never ledger replacements or proof of
  knowledge. Missing/revoked source means do not assert the remembered fact.

Explicitly separate direct observation, attributed speech, rumor, inference
and model-generated embellishment. A player saying "I killed the king" is
evidence that the player **said it**, not proof of the king's death. No memory
summary may create quests, kill actors, reveal private player information or
backfill unseen events. Player chat and personal material require scoped
retention and opt-out policies before broad summarization/training. Never
ship server memories or player transcripts inside public worldpacks or model
weights without specific rights and authorization.

## 5. Factions, guilds and stakes

Faction/guild state belongs in the authoritative world database with explicit
roles, membership grants, faction resources, bounded reputation changes and
event-derived treaties/rivalries. Define invariants before adding verbs:

- A character may not grant themselves guild rank, ownership or privileges.
- An NPC cannot move player-owned resources, impose membership, transfer
  territory, start player-versus-player combat or destroy a world asset by
  narrative declaration.
- Faction transitions require explicit policy, sufficient source state,
  permission, resource balance, cooldown and durable receipts. Simultaneous
  bids/claims resolve in serialized transactions, never "whoever narrated
  first."
- Rivalries and reputations may evolve from actual committed events. Rumors
  may affect *beliefs* under a separate rule, not rewrite factual outcomes.
- Irreversible effects such as permanent death, player loss, guild expulsion,
  territory seizure, world destruction or economy minting are **off by
  default** pending feature-specific owner controls, consent and migration
  tests. Historical preferences for PvP MUDs are design inspiration, not
  standing consent for PvP on every self-hosted world.

Keep creator-owned world rules explicit. The engine must not import
proprietary White Wolf/World of Darkness or other MUD setting assets without
appropriate rights. A fantasy flagship may borrow broad mechanical lessons
without copying protected material.

## 6. Irreversible-action proposal gate

The current `ResidentController` is intentionally restrictive. Extend the
host-controlled PROPOSE → VALIDATE → COMMIT contract with an effect tier:

| Tier | Illustrative action | Default rule |
| --- | --- | --- |
| Observe | look, inspect local visible context | Allowed to authorized resident; no material mutation. |
| Express | say within content/rate limits | Host validates; preserve as attributed speech. |
| Reversible | move along open exit, consume replenishable resource | Validate world/actor/version/quotas; commit with receipt. |
| Scarce/contested | take or trade unique item, change guild resource | Explicit resident-specific capability and serial transaction; denied by default. |
| Irreversible | kill, permanent exile, burn a settlement, irreversible faction transfer | Default-deny; require human/owner rule or separately governed approval and independent qualification. |

The tier is assigned by engine semantics, **not** proposed by the model.
Reject extra model fields such as `actor`, `world_id`, `sql`, `approval`,
`override`, `action_id`, or `verified`. A valid sentence is not a valid
authorization. Logging rejection must not itself alter game canon; emit
administrative audit separate from in-world historical events.

## 7. Schema and versioning proposal

Add new typed, explicitly migrated schema after tests justify the smallest
necessary design: `simulation_clock`, `tick_intents`, `npc_profiles`,
`npc_drives`, `memory_observations`, `memory_summaries`, `factions`,
`faction_memberships`, and `faction_events` are **candidate entities** only.
Persist world ID, schema/ruleset versions and provenance relationships, and
enforce foreign keys, uniqueness, bounded payload sizes and scoped deletion.
Avoid a database table whose content directly encodes executable model output.

Any migration must use the engine's guarded schema/worldpack checks,
prefer a new scratch database for qualification, reject ambiguous legacy
schemas, and have a measured rollback/backup strategy for owner data. Never
automatically rewrite existing player worlds on package import or process
startup. A self-hosted server can operate with its model adapter entirely
disabled and all optional worldpacks absent.

## 8. Falsification and acceptance matrix (all **UNEXECUTED**)

| Gate | Required hostile test | Passing evidence |
| --- | --- | --- |
| T1 ownership | Launch two coordinators against one world; race/fail lease epoch. | Exactly one eligible writer; stale owner cannot advance a tick. |
| T2 restart | Kill coordinator before intent, after intent, after Engine commit, before cursor advance. | No duplicated effect, changed replay action, lost receipt or skipped intention. |
| T3 clocks | Pause host for hours; modify wall clock forward/back; resume. | Bounded deterministic catch-up and no uncontrolled wall-clock action flood. |
| T4 determinism | Replay a pinned seed/ruleset/world event fixture with models disabled. | Identical ordered intents, drive transitions, action receipts and canonical game-state hash excluding real-time metadata. |
| T5 model adversary | Inject actor/SQL/approval metadata, prompt injections and oversized/slow output. | Strict parse failure/no privileges; bounded timeout/no world mutation. |
| T6 stale observation | Have a human move or take target while NPC inference is pending. | Old proposal rejected against fresh state; no retargeting/duplicate take. |
| T7 memory boundaries | Put secrets/chat in other room/world; generate rumors and fake model summaries. | No invisible facts in NPC recall; speech not promoted to event truth; citations check out. |
| T8 world isolation | Clone seed into two DBs, same resident names/action IDs and different events. | No cross-world state, memory, faction or receipt leakage. |
| T9 factions | Concurrent resource claims, rank escalation and hostile NPC expulsion proposal. | Single serialized resource outcome; unauthorized rank/expulsion denied. |
| T10 irrevocable | Model suggests permanent death, territory destruction or economy mint. | Default-deny with audit; no ledger/economy mutation. |
| T11 $0 fallback | Remove model runtime/network, exhaust time/CPU/RAM budget. | Deterministic host continues safely or pauses explicitly; no cloud request/spend. |
| T12 two clients | Two actual clients watch one NPC commit, reconnect over missed events. | Same authoritative event IDs/room views; no invented client or NPC effect. |

Tests must record exact source revision, Python/SQLite version, ruleset and
fixture hashes, DB-before/after hashes or canonical SQL snapshots, command,
exit status and observed output. Distinguish unit simulations from host probes,
real inference execution, independent review and full multi-client integration.
An internal model review or passing mock response does not prove a living
autonomous NPC or safe remote service.

## 9. Recommended implementation slices

1. **Deterministic host clock** behind an opt-in CLI with scratch DB and
   model-disabled fixture; implement T1–T4 before adding model calls.
2. **Versioned drive projection** from a bounded subset of canonical events;
   prove deterministic restart, no cross-world leakage and human/AI parity.
3. **Observation ledger and memory retrieval** with strictly checked source
   event IDs, visibility provenance and falsification T7–T8.
4. **Faction rules + capabilities** with default-deny scarce/irreversible
   actions, permission and contention tests T9–T10.
5. **Optional local model adapter** for dialogue/intent; register exact model
   file hash, quantization, license, tokenizer/template, runtime, context,
   generation settings and latency; run paired model-free/model tests T5–T6.
6. **Network/player acceptance** only after separate TLS/session qualification
   and two-real-client T12; keep Android source and client concerns separate.

The release claim graduates only with executed receipts for each relevant
gate. A persuasive AI conversation, proposed test plan, or merged README
does not count as operational qualification.
