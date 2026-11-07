# Disposable REALM engine integration receipt (2026-11-06)

## Current follow-up — atomic receipts and restartable policy (2026-11-06)

The original **28-test** receipt below remains historical evidence against
REALM main `09fc7cf6`. It is superseded for current implementation claims by
the subsequent draft source patch: `Engine.act_with_receipt` and
`ResidentController.commit_with_receipt` return transaction-coupled receipt
creation status. A concurrent pair of same-actor calls under SQLite's
`BEGIN IMMEDIATE` lock cannot both report a newly inserted receipt; the NPC
adapter trains only on `receipt_created=True`. Legacy precommit witnesses
are not sufficient and no longer enable learner updates.

New bounded source tests: REALM core **111/111**, original NPC **47/47**,
and Node/browser **20/20** passed, including real disposable-world atomic
race, two-turn goal completion and explicit policy save/reload tests. This
is **not** a running Lappy service or evidence of unseen-game proficiency.
The host's actor/world binding, event-time visibility, private checkpoint
ACLs, and tick timing remain to be integrated and qualified.

**Evidence class:** test against source-exact REALM Python modules, not a
running Lappy REALM service and not a production world qualification.

## Exact inputs

- REALM main commit:
  `09fc7cf6fcd8f7826c315d69c045467f27a7c8c7`
- `src/realm/engine.py` Git blob:
  `fb343c114561d6dccc1a68027ddc0bd767dc1c8e`
- `src/realm/residents.py` Git blob:
  `e6409a061d2018146e48963c488d368669f4bfd5`
- `src/realm/__init__.py` Git blob:
  `64b9547482e210dcca0c0609638cef977032bbd3`

All three were fetched from the commit via the GitHub connector and reproduced
byte-for-byte into a temporary import-only source tree. `git hash-object`
on each local file matched its listed Git blob SHA.

## Actual execution

Command (with `REALM_SRC_ROOT` pointing to the fetched source tree):

```sh
REALM_SRC_ROOT=/path/to/exact/REALM/src \
  python3 -B -m unittest discover -s realm_npc_build/tests -p 'test_*.py' -v
```

Outcome after the receipt-witness extension: **28/28 PASS** in both normal
and `python -O` runs, including 3 tests that run real `Engine` and
`ResidentController` against disposable local SQLite databases. The tests
assert actual room movement, engine-generated event and action receipts,
host-owned actor isolation, duplicate receipt behavior, post-commit policy
learning, restart persistence of game state and receipts, and separation of
two different SQLite worlds. The third real-engine regression creates a new
learner after host restart, sees an already committed speech receipt through
an actor-scoped SQLite witness, and confirms the replay creates neither an
extra event nor an extra learning update.

**Witness qualification boundary:** the precommit receipt-existence query is
host-owned and actor/world-scoped. The replay test confirms one serialized
writer in a disposable database; it does not prove a read-before-commit check
is atomic under concurrent separate writers. A production host must serialize
same-actor turns or use a transaction-coupled receipt witness. The adapter
distinguishes observed-new learning (`learned_with_host_witness`) from replay
(`replayed_receipt`) and no-witness (`receipt_unverified`).

**No model weights were used.** Learned behavior is a tiny tabular original
action-policy with observed room transitions. It did not run a real autonomous
REALM session or train on an unseen game world. Game rewards in these tests
were supplied by a simple host evaluator; they are not evidence that the
resident understands game objectives or social semantics.

## Reproduction within the REALM repository

Once this experiment is under `experiments/original_npc/`, run from the REALM
repository root:

```sh
python -m unittest discover -s experiments/original_npc/tests -v
```

The tests automatically locate the REALM repository's `src/realm` module.
They create and tear down their own SQLite databases in temporary directories
and do not touch configured live-world paths.

## Still unqualified

- Model behavior on unseen worldpacks, quest structure, players, economy,
  multi-character interactions and long-run reward curves.
- Host event-time privacy, actor authorization and scheduling in an actual
  live service; the test observer is trusted fixture code with direct SQL
  access, not a runtime observer API.
- Live-host use of checkpoint persistence and recovery of pending in-flight
  action decisions; the new checkpoint API itself has offline save/reload tests.
- Windows/Lappy-specific SQLite handle lifetime or service deployment.
