# REALM Creator: offline source-candidate review (draft)

This is a **bounded local extraction experiment**, not world generation, canon
approval, training data acquisition, or an installed/active Creator service.

The optional command reads an explicitly chosen local UTF-8 TXT/MD or DOCX file.
It recognizes **only explicitly labeled mentions** such as:

- Planet: New Dawn
- ## Station: Relay Nine
- Species: Migrants
- Ship: Lantern
- Faction: Observatory
- Item: Prism
- Technology: Slip Engine

Run in the isolated source tree with Python installed:

~~~powershell
$env:PYTHONPATH = "src"
py -3 -m realm.creator_offline --private-local-preview ".\synthetic-notes.md"
~~~

The result is a private/local JSON *review proposal*. It carries exact source
byte SHA-256, source format, candidate labels, deterministic IDs and bounded
evidence line/paragraph positions with exact quote hashes. A quote marked
SOURCE_CONFIRMED means its **text** was read from those bytes; the entity
it seems to describe is always PROPOSED, UNREVIEWED. Contradictory canon,
mapping, gameplay state, species rules and rights remain UNKNOWN/UNVERIFIED.

No worldpack is produced. No rooms, routes, item placements, atmosphere hatches,
QSD constants, medical rules, faction permissions, clocks or NPC actions are
inferred. No database is opened. No network/model is contacted. No canon,
rights, source, owner release or deployment gate is satisfied. Even a matching
SHA does **not** authenticate Patrick's publication/release approval.

Important privacy boundary: The JSON contains small original-source excerpts.
It is meant only for private on-device owner review. **Do not commit or post
the output if source content is unpublished, proprietary, personal, or otherwise
restricted.** This module does not read Wreckforge sources autonomously.

Input constraints: 1 MiB maximum source bytes; no .zip generic ingestion or
PDF, no wildcard/directory crawl; DOCX uses only word/document.xml, checks
archive member count/paths/compression ratio/size, disallows XML DOCTYPE/ENTITY,
and parses text without executing instructions found in it. Labeled candidate
count and repeated evidence are bounded. Plain-file reads may change between
stat/read; the output always hashes the bytes actually read, not the filename.

Before any eventual gameplay import, a separate system must provide reviewed
source binding, rights/owner release, conflict adjudication, navigation and
pressure topology validation, and authenticated runtime approval. Open draft
Creator PRs #77/#78 remain separate preview/staging lanes, not this module.
