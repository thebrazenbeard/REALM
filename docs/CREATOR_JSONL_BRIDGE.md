# In-memory provenance JSONL → Creator review candidates (Draft/HOLD)

`realm.creator_jsonl_bridge.preview_jsonl_chunks(payload)` accepts **explicitly
supplied** UTF-8 `bytes` or `str`, with one
`realm.creator.provenance_chunk.v1` JSON object per physical line. It returns the
existing `realm.creator.candidate_review.v1` shape from Creator #95/#100,
extended with `source_bindings` and `conflicts`. No file paths, network, models,
database handles, worldpack, import, approval, deployment, or automatic output
persistence are involved.

This is a **proposed public adapter contract** for synthetic tests, not a claim
that Vera A's private JSONL producer has this exact schema. A private producer
must explicitly map its own chunk format and independently review rights and
source custody. Never commit its returned private quotes or hashes to public Git.

One synthetic record (with real SHA-256 for `Ship: Lantern\n` and its quote):

```json
{
  "schema": "realm.creator.provenance_chunk.v1",
  "source": {
    "id": "synthetic-book", "revision": "r1", "parent_revision": "r0",
    "raw_sha256": "7634452793083dbf4f521c5dcefc6c13f9894fa12b40445d586c32f7f11d3112",
    "raw_bytes": 14, "custody_status": "HASH_MATCH_REPORTED"
  },
  "candidate": {"category": "ship", "name": "Lantern", "status": "PROPOSED"},
  "evidence": {
    "unit": "line", "ordinal": 1, "text": "Ship: Lantern",
    "text_sha256": "339b54692f7af7a0276500167ba6e44caa467c3bc195931c730070ecc171d5f0",
    "source_status": "SOURCE_CONFIRMED"
  },
  "conflicts": []
}
```

Each physical JSONL line is independently bounded to 4 KiB. The batch limit
is 256 KiB / 256 records, 128 candidates, eight evidence entries per
candidate, 64 source revisions, and 256 retained conflicts. Input fields are
strictly enumerated. Evidence quote SHA is recomputed from the exact UTF-8
quote; malformed, duplicate-key, invalid-UTF-8, duplicate provenance position,
raw-hash identity collision, control/bidi, unexpected status and over-limit
records fail closed, never silently truncate.

The `source.raw_sha256` digest is copied **verbatim as supplied** into the
review lineage, never recomputed from unavailable originals. The JSONL
transport's own digest and exact supplied quote digest are independently
computed; the quote digest does **not** prove that the original source contains
those bytes. Optional `source.parent_revision` is retained as a *reported*
lineage edge, not a verified custody chain. Accordingly:

- `source.originals_verified_by_bridge` is **always false**.
- Every candidate remains `status=PROPOSED`, `review_status=UNREVIEWED`.
- Evidence `reported_source_status` preserves the sender's claim; effective
  `source_status=SUPPLIED_NOT_REVERIFIED` does **not** repeat it as verified.
- Conflicting source revisions, supplied parent-revision cycles *within the
  batch*, name aliases (including whitespace/case/Unicode-normalized spellings)
  and evidence assignments are surfaced alongside explicitly reported
  unresolved conflicts. Parent links outside the batch remain unknown, not
  silently accepted as a verified chain. No conflict is auto-resolved into canon.
- Rights remain `UNVERIFIED`, canon `UNAPPROVED`, worldpack release `HELD`, and
  effect `REVIEW_ONLY_NO_IMPORT`. Metadata-only records do not gain source
  authority by entering this adapter.

Vera A owns private Driftspace source/canon interpretation. REALM owns this
setting-neutral review schema. Patrick retains exact author/canon approval and
any separately governed game-world release. See [REALM Creator #60](https://github.com/thebrazenbeard/REALM/issues/60)
and [Driftspace coordination #2](https://github.com/thebrazenbeard/driftspace/issues/2).
