# REALM engineering instructions

## Product intent

**REALM = Roleplaying Engine for Autonomous Living MUDs.** Build a reusable,
text-only, self-hostable multiplayer engine in which people and optional
model-driven characters can co-create durable history. Never describe planned
neural NPCs, dynamic drives, deep memory, remote public hosting, or real-time
push as shipped merely because the README names them as the product vision.

## Hard architecture boundaries

- `src/realm/` is setting-neutral engine code. No imports from `worlds/`, no
  hard-coded flagship fiction, and no dependency on a particular model vendor.
- `worlds/flagship/` contains **optional passive content**. A host explicitly
  imports a chosen validated worldpack into a fresh database; do not inject it
  into generic engine startup or replace an existing played world.
- One SQLite world instance has its own players, events, receipts, credentials
  and future NPC memories. Cross-world reads and writes are a test failure.
- Human/AI actors submit **proposals** to host-side validation and transaction
  commit. Model output and derived memory are not authority to write SQLite.
- The future Android app is a separate client surface; no embedded server,
  authoritative world state, private canon or remote plaintext credentials.

## Security, cost, rights

- Preserve the $0 local-first design. No paid compute, paid inference or cloud
  resources without explicit authorization. Core tests need no network/models.
- Current HTTP and Telnet adapters bind loopback. Do not publish, port-forward,
  tunnel or expose them beyond a controlled host without separate transport,
  authentication, abuse and deployment qualification.
- Do not commit model weights, credentials, player tokens, real personal data,
  or the owner's unpublished creative files.
- User-supplied tabletop notes may inspire general mechanics, but may contain
  third-party Forgotten Realms/White Wolf/D&D and private stories. Public
  showcase content must be newly written, rights-aware and separate from canon.

## Work and evidence

1. Inspect live `main`, issues/PRs and exact source before substantive work.
2. Use isolated branches/directories when parallel agents edit the repo.
3. Prefer bounded regression: demonstrate the defect, fix it, run focused and
   full tests. Cover Windows SQLite handle lifetime as well as Linux semantics.
4. Keep source inspection, unit test, actual host test, model review, and human
   review as separate evidence. Do not claim a real neural inference run from
   a deterministic fixture or from a model card.
5. Keep draft PRs until integration tests and hostile review support merge.
   Preserve material dissent and unresolved limitations in the PR.

Run from the repository root:

```sh
python -m unittest discover -s tests -q
node --test tests/browser/test-client.cjs tests/browser/test-map.cjs
```

See `docs/ROADMAP.md`, `WORLDPACKS.md`, and the flagship content's separate
`SOURCE_BOUNDARY.md` for current capability and provenance limits.
