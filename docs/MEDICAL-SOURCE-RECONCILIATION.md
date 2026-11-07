# REALM medical source reconciliation (review-only)

This source-only bridge combines already-existing draft primitives; it is **not** a treatment, medical prognosis, or authority mechanism.

- Pure UTC game-balance injury windows: [Draft #69](https://github.com/thebrazenbeard/REALM/pull/69).
- Atomic kit spend + injury CAS in a separate medical SQLite sidecar: [Draft #74](https://github.com/thebrazenbeard/REALM/pull/74).
- Hardcore finality admission held behind independent witness: [Draft #79](https://github.com/thebrazenbeard/REALM/pull/79).
- Bound per-world Casual/Hardcore policy: [Draft #85](https://github.com/thebrazenbeard/REALM/pull/85).
- Atmosphere-to-injury proposals, never persistence: [Draft #87](https://github.com/thebrazenbeard/REALM/pull/87).

`inspect_medical_source_in_transaction` requires a caller-owned REALM world SQLite transaction and checks the **main** world/mode binding, policy hash/version, UTC start time, actual atmosphere revision and exact separate medical sidecar world/mode/policy metadata. The sidecar uses read-only SQLite. Forged TEMP mode tables and foreign medical views are rejected; missing files are not created. The review additionally scans `PRAGMA main.foreign_key_check` to reject orphan receipts and checks stored supply quantities and injury versions explicitly, because a fresh read-only SQLite `integrity_check` may not detect rows inserted with CHECK constraints disabled in all runtime versions. It also requests `PRAGMA main.integrity_check`. All scans are read-only and can cost O(medical rows); the bridge fails closed above **64 MiB** of logical SQLite page storage, so large medical ledgers need a separately qualified review strategy. These checks are **not** a complete row-semantic or data-integrity proof, a cross-file atomic snapshot, a rollback witness, or permission to apply medical effects. The return is always `HELD_HOST_REVIEW_REQUIRED`, with both write-authority flags false.

**Hard limitations:** The sidecar snapshot is not atomic with the world database. A separate medical ledger has no authenticated principal/proximity witness, independent irreversible tombstone, downtime fairness proof, immutable policy custody, per-species lore authority, or authorized VN2 installation. A consistent snapshot is an aid to human review **only**. Neither this bridge nor the upstream source grants player treatment, kit spending, injury registration, final death, casualty recovery, or live service activation.

Rescue windows are configurable fictional game rules, not real survival predictions. In particular, no universal gunshot, stab, cardiac arrest or decompression timing is encoded here. ALQS remains a proper-name QSD relation, not a medical or atmospheric constant.

All tests are on isolated, disposable synthetic worlds. $0 compute; no Wreckforge original text, private worldpack, canon release, merge or VN2 effect.
