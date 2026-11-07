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
`time.time()` or a model's estimate of elapsed time. Persist two separate
watermarks: `planned_through` (all intent rows durably allocated through this
tick) and `finalized_through` (all intents for every tick through this tick
have terminal dispositions). `finalized_through` advances **contiguously**;
it cannot skip an incomplete earlier tick even when later ticks are done.
Wall time is metadata only, not a source of nondeterministic game
outcomes. Existing `Engine._event` does stamp real UTC time; deterministic
fixture comparisons must explicitly exclude that metadata or introduce a
separate logical occurrence field. A host-controlled cadence may be
configured; backlog after downtime has a bounded catch-up limit and an
explicit paused/skipped-tick record.

Proposed host transaction and recovery rules:

1. Acquire a validated single-scheduler lease/ownership fence bound to the
   world instance, scheduler owner and monotonically increasing epoch before
   work. SQLite write locking alone is **not** a leader election protocol.
   Startup must fail closed if competing host ownership cannot be established.
   **Critically, lease prechecking in the coordinator is insufficient:** the
   current `Engine.act` has no scheduler-fence argument or in-transaction
   comparison. The future fenced commit API **must check `(world_id, owner,
   epoch, lease_validity)` inside the very same `BEGIN IMMEDIATE` transaction
   that applies the game action and receipt**. If a newer owner stole the
   lease, the old owner cannot commit—even if it checked the lease milliseconds
   earlier. No cross-machine shared-folder SQLite authority is presumed safe.
2. In a short DB transaction, snapshot the authoritative room, participant,
   permission and drive versions; calculate bounded eligible residents in
   deterministic order; durably allocate immutable intent IDs in state
   `PLANNED`, then advance `planned_through`. No external model call may hold
   a SQLite write lock. The initial plan binds actor, world, tick, ruleset,
   model/strategy revision, observation snapshot and host-owned action ID.
3. Produce proposals outside the transaction from a deterministic fallback or
   an optional time-limited model. Seed any deterministic planner from a
   persisted stable seed plus tick/resident IDs (never Python's randomized
   `hash()`). **Before any effect attempt**, durably transition the intent to
   `PROPOSED` with the exact, bounded proposal bytes, encoding, hash and
   validator/strategy versions. A model's unpersisted partial answer is not
   evidence of an accepted proposal. If a process crashes while an intent is
   still `PLANNED`, **never regenerate stochastic output and pretend it is the
   same intent**: terminate as `provider-error`/`no-proposal` with no action,
   or replay only a specifically proven deterministic, pinned planner under
   a separately versioned policy. A stochastic retry, if wanted, needs a
   **new** intent ID and authorization/quota decision.
4. Validate the exact intent against current state and policy immediately
   before commit. Changed room, permissions, target ownership, epoch or
   state version invalidates the proposal. Reject stale intent; do not silently
   reinterpret it as another action.
5. Transition `PROPOSED → APPLYING` durably before effect application and
   commit via a **new fenced Engine transaction path**, using the host-derived,
   immutable `(actor, action_id)` plus exact recorded proposal bytes/hash.
   The fence, state-version revalidation, action effect and action receipt
   must execute under one database write transaction; an unfenced call to
   current `Engine.act` is **not a substitute**. Existing receipts illustrate
   deduplication, but this future path must preserve it while adding fencing.
6. Transition `APPLYING → TERMINAL` with a durable disposition (`committed`,
   `rejected`, `expired`, `no-op`, `provider-error`) and effect receipt link.
   At recovery, an `APPLYING` row must be reconciled by checking the *exact*
   actor/action ID, command, hash, epoch and authoritative Engine receipt: if
   already committed, finalize without reapplying; if absent, revalidate and
   apply only the **identical persisted** proposal under a valid new fence,
   or terminate rejected. Any disagreement fails closed for review.
7. Advance `finalized_through` only across a continuous prefix of ticks whose
   every intent is `TERMINAL`, including explicit no-action dispositions.
   Reconciliation must survive crashes between Engine effect, disposition and
   cursor advancement. Where the future API cannot atomically combine all
   three, receipt-driven recovery must prove equivalent outcomes with fault
   injection; the present `Engine.act` does **not** yet satisfy this contract.

Allowed durable intent transitions are `PLANNED → PROPOSED(bytes/hash) →
APPLYING → TERMINAL`; `PLANNED → TERMINAL(no-proposal)` and `PROPOSED →
TERMINAL(rejected/expired)` are explicit fail-closed exits. Do not collapse
"planned" with "proposed", or use a completed later tick to hide an unresolved
earlier one. Model text may vary; **only committed host effects** are
authoritative and replayable.

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

- **Source event reference:** canonical `events.id` plus DB/world identity,
  event kind, observed actor, room, ruleset and observation/visibility version.
  A **host-issued, event-time observation grant** must prove that this specific
  resident was entitled to learn that specific fact when it happened, under
  then-current room, sensory, relationship, privacy and consent rules. Neither
  `events(room_id=...)` nor the engine's `history` text is an observation-grant
  database: a resident arriving later must not inherit earlier local secrets.
  The current events table lacks grants; replay or reconstruction from room
  membership alone cannot repair their absence. No historical memory is
  backfilled without explicit evidence of event-time entitlement.
- **Derived memory:** bounded per-resident episodic records citing one or
  more eligible events, optional confidence/summary, and retention policy.
  Retrieval filters by resident/world, **issued observation grants and their
  current revocation/retention state**, then returns source IDs with snippets;
  summaries are search aids, never ledger replacements or proof of knowledge.
  Consent revocation, player deletion/retention expiry or stricter privacy
  policy may remove a memory from future NPC retrieval even if the append-only
  game event remains for legitimate audit/gameplay. Do not treat an old grant
  as perpetual consent to disclose a private chat. Missing/revoked source or
  grant means do not assert the remembered fact.

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

**First-time worldpack import currently enforces an exact SQLite schema
fingerprint.** For a not-yet-imported world, `import_worldpack` checks
`_schema_snapshot(db) == _pristine_schema(...)`, including the optional
existing `player_credentials` schema. Adding any simulation table **before
first import** would cause an otherwise pristine demo import to be rejected.
An already imported worldpack with the **same semantic hash** is an
idempotent early return *before* that fingerprint check, so later optional
simulation tables do not by themselves break same-hash re-import. A different
hash is still rejected. This distinction is intentional protection, not a
reason to weaken first-import checks to a permissive subset. Choose and test
a versioned, explicit path:
import a worldpack into a pristine supported engine schema **first**, then
apply the opt-in living-world migration; or update the canonical pristine
fingerprint/schema-version contract and corresponding importer tests
atomically. The two versions must never silently treat extra SQLite objects
as safe. Preserve `world_id`, `worldpack_sha256`, existing credentials, ledger
rows and receipts; reject unknown schema versions and ambiguous legacy DBs.

Any migration must use these guarded schema/worldpack checks, prefer a fresh
scratch DB for qualification, and have a measured rollback/backup strategy
for owner data. Never automatically rewrite existing player worlds on package
import or process startup. A self-hosted server can operate with its model
adapter entirely disabled and all optional worldpacks absent.

## 8. Falsification and acceptance matrix (all **UNEXECUTED**)

| Gate | Required hostile test | Passing evidence |
| --- | --- | --- |
| T1 ownership | Launch two coordinators against one world; precheck epoch, then steal lease immediately before the old owner enters Engine commit. | Atomic in-`Engine.act`-equivalent fence rejects old epoch with **zero** effect/receipt; exactly one eligible writer. |
| T2 restart | Crash during model output before `PROPOSED`, after proposal persistence, after `APPLYING`, after Engine effect, before terminal/cursor advancement. | Exact stored proposal/hash replayed or fail-closed no-proposal; no stochastic regeneration, duplicate effect, changed replay action or lost receipt. |
| T2b watermark | Finalize tick 12 while tick 11 is incomplete; restart and repair tick 11. | `planned_through` can exceed `finalized_through`; no holes in finalized range; advancement only after tick 11 terminal. |
| T3 clocks | Pause host for hours; modify wall clock forward/back; resume. | Bounded deterministic catch-up and no uncontrolled wall-clock action flood. |
| T4 determinism | Replay a pinned seed/ruleset/world event fixture with models disabled. | Identical ordered intents, drive transitions, action receipts and canonical game-state hash excluding real-time metadata. |
| T5 model adversary | Inject actor/SQL/approval metadata, prompt injections and oversized/slow output. | Strict parse failure/no privileges; bounded timeout/no world mutation. |
| T6 stale observation | Have a human move or take target while NPC inference is pending. | Old proposal rejected against fresh state; no retargeting/duplicate take. |
| T7 memory boundaries | Put secrets/chat in other room/world, move an NPC into the room later, then revoke observation consent; generate rumors and fake model summaries. | Event-time grant required; no retrospective room-history visibility; revoked private recall denied; speech not promoted to event truth. |
| T8 world isolation | Clone seed into two DBs, same resident names/action IDs and different events. | No cross-world state, memory, faction or receipt leakage. |
| T9 factions | Concurrent resource claims, rank escalation and hostile NPC expulsion proposal. | Single serialized resource outcome; unauthorized rank/expulsion denied. |
| T10 irrevocable | Model suggests permanent death, territory destruction or economy mint. | Default-deny with audit; no ledger/economy mutation. |
| T11 $0 fallback | Remove model runtime/network, exhaust time/CPU/RAM budget. | Deterministic host continues safely or pauses explicitly; no cloud request/spend. |
| T12 two clients | Two actual clients watch one NPC commit, reconnect over missed events. | Same authoritative event IDs/room views; no invented client or NPC effect. |
| T13 schema | Import worldpack before and after proposed schema migration in fresh/legacy DBs; alter one constraint or insert an unknown table. | Supported versioned order succeeds without weakening exact fingerprint; unsupported/mutated schema is rejected with no content loss. |

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
