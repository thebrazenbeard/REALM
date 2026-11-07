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
$env:REALM_PYTHON = (py -3 -c 'import sys; print(sys.executable)').Trim()
node --test tests/browser/test-client.cjs tests/browser/test-map.cjs experiments/realm_creator_ui/test_ui.cjs
```
