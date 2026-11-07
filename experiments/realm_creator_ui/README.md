# REALM Creator — local preview and review

A standalone **loopback-only, $0** experimental Creator interface. It turns explicitly marked locations in bounded text sources into **unreviewed** room suggestions, lets a person edit them, and exports a validated but **unimported** `realm.worldpack.v1` file.

This is not the player gateway, a public upload service, a canonical approval authority, or a world deployment tool.

## Start on Lappy (Windows PowerShell)

From the isolated REALM worktree:

```powershell
Set-Location 'D:\VERA\REALM-creator-ui-20261107'
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
py -3 -m experiments.realm_creator_ui.server --port 8771
```

Open the URL printed by the process (normally `http://127.0.0.1:8771/`). Copy the **ephemeral operator token** printed in the same terminal into the page's password field. Do not use a REALM player token or disclose the Creator token. Stop the process with Ctrl+C.

The server binds only `127.0.0.1`. It requires exact Host/Origin checks and its independent token on API requests; it does not provide host identity proof, canonical approval, or qualified remote access.

## Workflow

1. Choose a world ID and display name; select **1–8 UTF-8** `.txt`, `.md`, or `.markdown` files (maximum **256 KiB per file**, **1 MiB combined**).
2. Provide explicit headings such as `## Location: Copper Harbor`. Ordinary chapter headings and fenced Markdown examples are not silently promoted to rooms. Press **Generate preview**.
3. Inspect each source excerpt and provenance reference. Supply an authored room name and scene description; optional proposed exits are **unchecked** until you opt in.
4. Acknowledge that you reviewed the visible proposals, then **Compile reviewed draft**. Download the server's exact canonical JSON bytes without reformatting them. The file remains **unimported and unauthenticated**.
5. Changing upload inputs invalidates the previous preview. Changing reviewed details requires fresh acknowledgement.

**Existing worldpack route:** If you already have an authored `realm.worldpack.v1` JSON
file, use the separate **Preview authored worldpack** form and the same ephemeral
token. This accepts one UTF-8 `.json` file up to **512 KiB** and shows exact
validated room/item/directed-exit counts, authored room descriptions and links.
The `/api/v1/creator/existing-preview` endpoint reuses the strict lossless
in-memory validator; it never regenerates rooms, rewrites exits, imports SQLite,
or authenticates author/canon/release. For inspection, the browser replaces
invisible Unicode format controls in displayed names/descriptions with explicit
`\uXXXX` (or `\u{XXXXXX}`) escapes and counts these in a visible warning;
**the original authored worldpack bytes, hashes and server response stay unchanged**.
The resulting preview is **UNVERIFIED** and **UNREVIEWED**, and it has no
compile/deploy button. Keep source rights evidence
and any game-world release approval in a separate private, host-authenticated
workflow.

### Host-custodied exact-candidate staging (source-only)

The `release_gate.stage_exact_worldpack` Python function is an **offline
staging-only** seam, not a browser route or world importer. It accepts exact
authored `realm.worldpack.v1` bytes, a bounded JSON source manifest, a
canonical UTF-8 release-authorization payload and a **separately held** HMAC
over `b"REALM/CREATOR-RELEASE-STAGING/v1\x00" + authorization_bytes`.
The host supplies a purpose-specific secret (at least 32 bytes) and fixed
`expected_owner_id`, `expected_key_id` and `expected_ledger_id`; neither player
credentials nor the Creator loopback token are valid substitutes.

The exact authorization keys are `schema` (`realm.creator.release-authorization.v1`),
`scope` (`STAGE_ONLY`), `release_id`, `owner_id`, `key_id`, `ledger_id`,
`world_id`, `revision`, `raw_sha256`, `semantic_sha256`, and
`source_manifest_sha256`. The source manifest is
`{"schema":"realm.creator.source-manifest.v1","sources":[{"source_id":"worldpack","sha256":"<raw pack hash>"},...]}`
with unique bounded source IDs and lowercase SHA-256 digests. The format binds
source identifiers and bytes, **not** the truth, rights or canon of source lore.

The dedicated SQLite staging ledger serializes one `(world_id, revision)` and
`release_id` in a single write transaction. Exact retries are readback-only;
changed IDs or content are rejected. The receipt always says
`STAGED_NOT_IMPORTED`, `owner_authority=HELD_UNVERIFIED`,
`release_permitted=false`, and `world_imported=false`. Key possession proves
only the host key is present, **not that Patrick authorized anything**. A
separate authenticated owner decision, source-rights review, independent
custody/witness and real-world isolation/backup qualification are required
before building an actual importer/release path. This rollbackable SQLite
ledger cannot provide global replay immunity against copying/restoring the
ledger file; do not treat it as an immutable release witness.

The browser and server process source bytes in memory only; there is no upload directory, database write, external API call or telemetry. **Your manual worldpack download is an intentional local file write.** No private fiction is included in this repository.

## Trust and release limits

- Source headings, excerpts, and generated exits are *proposals*, not verified canon. Uploaded instructions are treated as inert data.
- The review checkbox is **not authenticated Patrick approval**, a signature, permission to import, or a binding to production credentials.
- Exports are limited to the existing worldpack importer's **524,288-byte** cap and checked for unsafe Unicode; the compiler and importer remain separate operations.
- The API does **not** import SQLite, merge into played worlds, install a service, register users, or expose the app outside loopback. Do **not** port-forward, Tailscale Funnel, or publish this process.
- Any Driftspace/Wreckforge production import needs separate private source-rights review, exact `world_id` + pack-hash authorization, isolated host/credentials/backups, and deployment qualification.

## Checks (from repository root)

```powershell
$env:PYTHONPATH = Join-Path (Get-Location).Path 'src'
py -3 -m unittest experiments.realm_creator_ui.test_server experiments.realm_creator_ui.test_workflow experiments.realm_creator_generation.test_generation experiments.realm_creator_generation.test_integration experiments.realm_creator.test_draft -q
py -3 -O -m unittest experiments.realm_creator_ui.test_server experiments.realm_creator_ui.test_workflow experiments.realm_creator_generation.test_generation experiments.realm_creator_generation.test_integration experiments.realm_creator.test_draft -q
py -3 -m unittest discover -s tests -q
py -3 -m unittest tests.test_creator_release_gate -q
$env:REALM_PYTHON = (py -3 -c 'import sys; print(sys.executable)').Trim()
node --test tests/browser/test-client.cjs tests/browser/test-map.cjs experiments/realm_creator_ui/test_ui.cjs
```
