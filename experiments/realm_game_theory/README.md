# REALM experimental anti-coordination trial

**Source-only research harness.** This does not implement strategic or neural
residents, autonomous ticks, multiplayer matchmaking, a production policy,
private setting lore, or a live world feature.

## Why this is game-theoretic

Two actors independently choose between gathering at A (atrium/stone) and B
(corridor/wood). Both nodes contain three units per round. If they split, both
receive three units; if they collide, only the first gatherer receives three.
With fair alternating priority the expected payoff matrix is:

| Ada / Ben | A | B |
| --- | --- | --- |
| A | (1.5, 1.5) expected | (3, 3) |
| B | (3, 3) | (1.5, 1.5) expected |

The two split-site combinations are pure anti-coordination equilibria under
this simplified game. The harness collects **both choices before either
world action**, then uses REALM Engine for movement and Crafting for actual
resource inventory, consumption, events, and action receipts.

## Run on Windows Lappy

From this isolated REALM worktree:

~~~powershell
Set-Location 'D:\VERA\REALM-game-theory-20261107'
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
py -3 -m unittest experiments.realm_game_theory.test_trial -q
~~~

Compare three deterministic **host-owned policies** against an always-A opponent:

~~~powershell
py -3 -c "from experiments.realm_game_theory.trial import run_trial, always, avoid_last, seeded_random; controls={'fixed':always('A'),'seeded_random':seeded_random(11),'prior_choice_reaction':avoid_last}; [(lambda r: print(name, r.total_payoffs, r.collision_count, r.efficiency))(run_trial(policy, always('A'), rounds=6, seed=3)) for name,policy in controls.items()]"
~~~

Each round uses a fresh disposable SQLite world database. This yields bounded,
isolated research, **not** a continuous world or a memoryful live character.

## Evidence boundaries

- always: fixed choice; seeded_random: seeded independent choice;
  avoid_last: reaction to the opponent's *previous* completed choice.
- avoid_last is a transparent hand-authored heuristic, not a trained or
  inferred strategy. Success against always-A does not establish learning.
- Policy callables must be **trusted host code**. The harness stages both
  choices before mutating SQLite, and supplies no current opponent choice in
  Observation. It does **not** cryptographically isolate Python closures or
  guarantee sealed simultaneous commits between malicious policies. Untrusted
  model output should only supply bounded A/B data to a separately authenticated
  host validator; do not execute model-generated Python.
- Game payoff comes from actual REALM material inventory, not a model's claimed
  reward. The harness commits a whole-round choice/order record before
  Engine movement, so conflicting retries fail before effects; same-choice
  receipts replay safely after reopening SQLite.
- The prevalidated commitment is **not** an all-or-nothing transaction across
  every movement and gather in a crash. No production-grade round recovery or
  external anti-rollback authority is established.
- Alternating first gatherer avoids permanent mover advantage when both choose
  one node; order is seeded and deterministic.
- Multi-round payoff comparisons are against controls and should additionally
  be challenged with random/alternating/adversarial opponents and held-out seeds.
- No neural model was run; no learned policy, general equilibrium behavior, or
  production integration has been verified. This lab calls no paid APIs.
