# REALM Creator generation — source-bound region proposals

**Status:** synthetic, review-only follow-up stacked on Draft PR #61
(`a431e391e872a09899453ebe63690a8a4044243c`). This directory is
disjoint from PR #61's `experiments/realm_creator/` contract.

This is a deliberately small proof that uploaded source data can drive
**generation** without becoming source canon or modifying an existing world.

## Contract

`propose_region(draft, sources)` takes:

- An unchanged `realm.creator.draft.v1` from PR #61's `prepare_draft`.
- The exact original `{basename: bytes}` source bundle used to create it.

It reconstructs the intake draft and rejects mismatched bytes, filenames,
hashes, sections, or identity. It recognizes **only explicit headings** like
`## Location: Copper Harbor` or `### Location: Glasswood`. Generic
chapter titles do not silently become places. It validates the *actual*
Markdown heading at the recorded source SHA-256 and line, excluding headings
inside backtick/tilde fenced code (including unclosed/longer fences). PR #61
can chunk long sections; continuation labels are not new rooms. Unsafe bidi
or invisible formatting in world display names is rejected here.

For 1–16 unambiguous ASCII location headings, it deterministically proposes:

- A stable room ID and name carrying an unreviewed source-section citation.
- A bounded source excerpt clearly labeled `UNREVIEWED_SOURCE_EXCERPT`.
- A generic description labeled `GAMEPLAY_PROPOSAL`.
- Adjacent east/west links labeled `GAMEPLAY_PROPOSAL`, with region-level
  `topology_evidence: NOT_SOURCE_ESTABLISHED`.

This is **not evidence of actual geographical adjacency**. A creator must
review, modify and authenticate a separate approval before converting
proposed rooms into a validated `realm.worldpack.v1` through PR #61's
`compile_approved_worldpack` contract. Even PR #61 compilation remains
unimported until an authorized host performs a distinct fresh-world import.

The proposal is `REVIEW_REQUIRED`/`UNREVIEWED`, with
`approval_authenticated: false`, `worldpack: null`,
`world_imported: false`, `live_world_modified: false` and a deterministic
SHA-256 digest. No model, database, network or filesystem writes occur here.

## Synthetic example

```python
from experiments.realm_creator import prepare_draft
from experiments.realm_creator_generation import propose_region

sources = {"atlas.md": b"## Location: Copper Harbor\nA tidal pier.\n"}
draft = prepare_draft("demo", "Demo", sources)
blueprint = propose_region(draft, sources)
assert blueprint["rooms"][0]["name"] == "Copper Harbor"
assert blueprint["approval_authenticated"] is False
```

## Tests

Run from the REALM worktree root with `src` and root on `PYTHONPATH`:

```sh
python -m unittest discover -s experiments/realm_creator_generation -p 'test_*.py' -q
python -O -m unittest discover -s experiments/realm_creator_generation -p 'test_*.py' -q
```

## Not implemented

No web upload, PDF/ZIP extraction, semantic entailment, automatic canon
approval, arbitrary-prose or model generation, live-world expansion, persistent
world import, or VN2 deployment. Source text remains untrusted. This plugin
does not ingest Driftspace or Wreckforge, and does not publish any lore.
Future upload storage adapters must use opaque storage IDs, never raw source
basenames; the upstream in-memory intake does not normalize Windows reserved
names or case-insensitive filename aliases for persistent filesystems.
