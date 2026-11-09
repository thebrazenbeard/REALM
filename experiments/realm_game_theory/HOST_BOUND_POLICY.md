# Host-bound economic anti-coordination proposal (source-only)

The `realm.npc_game_theory.choose_economic_site` helper is a **read-only
decision proposer** over an existing, worldpack-bound REALM SQLite file.
It is **not** a trained model, running resident, action executor, scheduled
simulation, or authenticated gateway route.

The host supplies the world DB path, exact bound `world_id`, two prejoined
actor identities, two to eight real resource nodes, an event-ID watermark
chosen *before* the strategic round, and a deterministic tie-breaking seed.
The proposer opens SQLite in `mode=ro` and `query_only`, verifies the bound
world and actors, reads actual Crafting stock and prior successful opponent
`gather` events at or before the watermark, and returns a candidate plus
a SHA-256 observation fingerprint. It never mutates SQLite or calls
`Engine.act` / `Crafting.perform`.

## Explicit game-theoretic model

For a candidate resource node with current stock `s`, batch yield `b`:

- `u_first = min(s, b)`
- `u_second = min(max(s-b, 0), b)`
- `p_same = (opponent_gather_count_at_site + 1) /
  (all_offered_site_observations + number_of_sites)` (Laplace estimate)
- `forecast = u_first - p_same * (u_first-u_second) / 2`

The factor 1/2 assumes a fair random first-gatherer when both select the
same site. REALM's current action adapters **do not enforce** such fairness.
Forecasts are rational numerator/denominator, not verified profits, learned
policy, guaranteed outcomes or equilibrium proof. High enough stock allows
both actors a full batch, so a collision need not impose any penalty.

The test uses host-owned `Engine` and `Crafting` actions in disposable
synthetic worlds to compare an estimate to actual resource events/receipts.
Those independent movement/gather calls are **not** one atomic strategic
round. The proposal itself has no action receipt or write authority.

## Security and evidence boundaries

- The event cutoff filters **opponent history only**. Other resource stock
  may change afterward. Even with the same cutoff and seed, a new proposal
  may differ after resource stock changes; the fingerprint records observed
  input state, but does not sign it or enforce replay fencing.
- Past gather events are host ledger data, not a cryptographic proof of
  opponent intention, complete choice history, or future action.
- The host MUST authenticate actor identity; player names cannot be used
  as authority. The host must hold decision ordering, cutoff provenance,
  validated movement path, action IDs, and any later model output outside
  this function, and recheck resources before committing gameplay effects.
- No simultaneous hidden-choice isolation from malicious Python code,
  no multi-agent transaction, no per-NPC durable drives/memory, no real
  background tick, and no VN2/Driftspace runtime qualification.
- The only status this API issues is `HOST_REVIEW_REQUIRED`.

This slice makes a strategy computable from **real** REALM resources and
past events while preserving the boundary between a proposal and a
host-committed action. PR #71's laboratory remains the separate baseline
for controlled two-actor, seeded trials.
