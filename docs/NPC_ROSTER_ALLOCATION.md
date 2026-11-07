# REALM full-roster capacity proposal (source-only)

Status: **HOST_REVIEW_REQUIRED**. This module never authenticates an actor,
performs a move/gather, reserves resources, writes a receipt, or approves
live gameplay. It does not prove a Nash equilibrium or strategic learning.

## Tested problem

PR #96 correctly prevents correlated-seed collisions for one symmetric
pair, but a pairwise chain of 3/5/8 actors was measured at 2:1, 4:1 and
7:1 site-choice ratios. That algorithm has no full-roster capacity input.

propose_roster_allocation offers a distinct, opt-in **host welfare**
policy for a bounded, explicitly host-provided complete roster (2..8 joined
actors), 2..8 registered resource sites, a durable world ID and event
watermark, and a bounded host seed. It reads a consistent SQLite snapshot
from main tables using read-only mode and never mutates world state.

For each site, the k-th sequential harvest is at most
min(batch_yield, max(remaining - k*batch_yield, 0)).
A memoized finite search maximizes the total value of available harvest
across the complete offered roster. Optional bounded positive integer
site_values represent host-supplied utility per resource unit. Ties are
broken deterministically per actor/seed; input tuple order does not
change the outcome. Actors beyond stock capacity receive site=None
(WAIT) rather than a fictitious successful harvest.

## Authority and replay boundaries

- The host, **not a player**, must determine eligible actors, site set,
  utility policy, real action permits and round timing. Passing a roster
  to this read-only API does **not** attest its completeness or consent.
- The supplied event watermark excludes future history, but current stock
  is read at call time: a repeated cutoff after depletion is not a
  historical replay. evidence_sha256 records observed resources,
  canonical actors, utilities, event maxima and host seed.
- No HMAC permit, actor authorization or action ledger is created here.
  Any actual action must be independently reauthorized and revalidated
  under the authoritative gameplay writer transaction.
- Two databases, concurrent writers, NPC learning, factions, path cost,
  trade, player manipulation, private Wreckforge lore and released
  Driftspace ship/pressure topology remain outside this scope.
- This welfare objective may diverge from an independent actor's
  best response. No global fairness, Nash or no-starvation claim follows.
- All proposed effects, irreversible Hardcore death, canon and VN2
  deployment remain **HELD** pending exact owner authority.
