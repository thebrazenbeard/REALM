# REALM Creator — offline intake contract spike

**Status:** isolated research spike; source-only; unmerged; no local or remote
world deployment. No AI model is invoked. No content is approved as canon.

`prepare_draft(world_id, name, {filename: utf8_bytes})` accepts at most eight
UTF-8 `.txt`, `.md` or `.markdown` files (256 KiB each; 1 MiB total). It returns
`realm.creator.draft.v1` with SHA-256 source provenance, section candidates and
line references. Headings are *sections*, **not assumed rooms or factual lore**.
Every section remains `UNREVIEWED`, `room_candidate=false`; long excerpts are
flagged as truncated. Unicode bidi-format and invisible control characters
are rejected at intake to limit display/name spoofing. This is not
PDF/DOCX/ZIP ingestion or open-ended
AI world generation.

`compile_approved_worldpack(draft, approval)` requires a matching draft hash,
explicit review acknowledgement and host-authored rooms/exits linked to source
proposal IDs. It validates with the existing REALM `worldpack.v1` validator
and returns a **non-imported** worldpack plus sidecar proposal-ID lineage for
every approved room/item, with a content hash of the exact approval. It does
**not** authenticate the
approver, contact the database, call `worldpack.import_worldpack`, schedule a
model, or publish source data. Caller must enforce user identity and authority.

The compilation explicitly marks its `lineage.source_link_status` as
`UNVERIFIED_CITATION` and `lineage.semantic_relation` as `NOT_ENTAILMENT`.
**A link from a room or item to a source excerpt does not mean the excerpt
supports the asserted room description, item, geography, or even the fictional
setting.** The contract does not perform semantic entailment checking.

The compiler's `sha256` detects accidental draft changes, but is not a MAC or
host authentication mechanism. User files and excerpts may contain private
fiction, secrets, protected third-party material or adversarial instructions;
keep all intake/output outside the public REALM repository and model-training
corpora. No source has permission to grant game actions or alter world canon.

The first planned private consumer is **Driftspace**. The actual Driftspace
canon is externally owned and **not present in this spike**. Generated
geography/rooms must be reviewed against that canon before import, and played
history thereafter remains a separate world-local ledger.

## Tests

From REALM root:

```sh
PYTHONPATH=src:. python -m unittest discover -s experiments/realm_creator -p 'test_*.py' -q
PYTHONPATH=src:. python -O -m unittest discover -s experiments/realm_creator -p 'test_*.py' -q
```

## Next product boundaries

1. Private authenticated upload/UI and source custody (content hashes, rights,
   prompt-injection isolation, type-specific parsing, virus/ZIP limits).
2. Optional local model extracts **proposals** with citations/confidence and
   contradictions; deterministic fallback must remain usable at $0.
3. Creator reviews the graph, names, permissions, content privacy and IP rights.
4. One explicit guarded `worldpack.v1` import into a fresh Driftspace SQLite
   database, followed by player provisioning and independent host qualification.
5. Later expansions to a *played* world need a new versioned host-governed
   patch/event contract. Re-running pristine-only `worldpack.v1` import on a
   populated world is forbidden; generation must never overwrite player events.

## Hostile challenge

> An upload-to-automatic-canon pipeline would launder model guesses and
> copyrighted/private raw text into persistent world truth. A generated
> candidate is not a source fact, owner approval or permission to publish.

> A syntactically valid citation can point to an unrelated paragraph. The
> compiler verifies reference **shape and identity only**, not whether a
> proposed Driftspace location or object is actually present in the source.
