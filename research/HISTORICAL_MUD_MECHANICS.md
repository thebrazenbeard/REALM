# Historical MUD mechanics relevant to REALM

**Status:** historical research and proposed test specifications, **not** implemented
features or evidence of live REALM behavior. Researched 2026-11-06. This document
belongs to the engine's research lane; it neither defines Siltglass Reach canon
nor grants a worldpack, player, or NPC any engine authority.

REALM's independent target is a locally hosted, multiplayer, text-only engine
where authorized humans and autonomous NPCs contribute to persistent history.
Melmoth, Astaria, GodWars and Vampire Wars are *separate reference subjects*.
Different revisions and derivatives of one MUD are not interchangeable.

## Evidence ledger

The strength column describes **evidence for a documented historical mechanic**,
not proof that the mechanic worked as advertised, still works, or was present in
the exact server Patrick played. All findings are paraphrased rather than
reproducing third-party code, room descriptions, lore, or artwork.

| Reference | Source URL and date/type | Scope and supported finding | Strength and caveat |
| --- | --- | --- | --- |
| Melmoth (early) | [Contemporary operator advertisement](https://groups.google.com/g/rec.games.mud.diku/c/MFsHHq4PdU0), **1997-03-24**, archived Usenet post | 80+ implemented areas and online creation (OLC); distinct class guild/town headquarters; player-run clans; racial skills; remort; weather affecting sight, combat, movement, and healing; player-killing above a level threshold with a peaceful-clan exception. | **High for the advertised design**; not independently executed. OLC by builders does **not** demonstrate that every player could edit rooms. |
| Melmoth (later) | [Archived game listing](https://www.topmudsites.com/forums/mudinfo-starkalien.html), publication date not established; read-only Top Mud Sites record consulted **2026-11-06** | Heavily modified ROM-based fantasy world; large ASCII wilderness linking areas; remort/rebirth and custom vampirism in the operator-supplied description. | **Medium**: self-description in an undated listing. Reported room counts and operational status cannot be promoted to current facts. |
| Astaria (official) | [Game website](https://astaria.net/), current public page consulted **2026-11-06**; [guild guide](https://astariamud.com/wiki/Guilds), wiki page last edited **2026-02-14** | Text-based multiplayer play through telnet/web/MUD clients; guilds with shared halls, separate equipment vaults, and group communication; classes tied to professions. | **High for documented systems**, not an independent live gameplay probe. The wiki is partly community-maintained. |
| Astaria (estates and events) | [A3 historical page](https://astariamud.com/wiki/A3), retrospective wiki article consulted **2026-11-06**; [outpost guide](https://astariamud.com/wiki/Outpost_Info), wiki consulted **2026-11-06** | A3 replaced earlier player housing/clan abodes with estates; outposts can fall to timed invasions, be retaken, and gate a travel convenience. | **Medium–high for described design**. Estate implementation and invasion timing are not current runtime measurements. |
| Astaria (PK) | [Player-versus-player wiki](https://astariamud.com/wiki/Player-versus-player), last edited **2016-04-14** | Historically documented level-gap, minimum-level and in-world-justification restrictions. | **Historical only**; old or contested text is not evidence of live 2026 rules. |
| Vampire Wars Classic | [Operator-written game listing](https://www.topmudsites.com/forums/muddisplay.php?mudid=vwclassic), undated archival entry consulted **2026-11-06** | GodWars/Vampire Wars codebase line, vampire-clan competition, classless/levelless description, unrestricted PK and status gained through PK. Self-described White Wolf influence. | **Medium**: particular Classic listing; not proof Patrick played this edition or that all Vampire Wars derivatives shared its rules. |
| GodWars derivative | [Um...Godwars source repository README](https://github.com/borlak/umgw), first-party derivative code and retrospective README consulted **2026-11-06**; authors say they ran it starting around **1998** | Two-dimensional ASCII maps, kingdoms, tournaments, votes, bounties, global quest-token announcements, bulletin boards, and reboot-oriented area randomization. | **High for the documented derivative**, **not** proof these were all in KaVir's original GodWars. Do not transplant this source code. |
| GodWars lineage | [God Wars: The Apocalypse credits](https://www.godwars.net/~apoc/credits.html), maintained project credits consulted **2026-11-06** | Explicitly credits GodWars Deluxe and KaVir plus successive maintainers/builders. | **High for lineage acknowledgment only**. Different branches have different owners, features and licenses. |
| World of Darkness rights | [Paradox Dark Pack Agreement](https://www.paradoxinteractive.com/games/world-of-darkness/community/dark-pack-agreement), official policy consulted **2026-11-06** | Rights holder identifies protected settings, factions, characters, Storyteller System, trademarks and art; qualified noncommercial fan online roleplay can be allowed under conditions. | **Authoritative policy at retrieval**, not blanket permission for REALM's distributable default world or unrestricted future use. Recheck before any WoD-specific publication. |
| Codebase licensing | [GodWars 2000 release archive](https://www.godwars.net/~mudbytes/preview/1337.html), release documented **2000-03-30** | Its authors describe multiple layered Diku/Merc/GodWars and derivative license obligations. | **High for that release's stated restrictions**; not a license audit of every separate repository. Reimplement generic mechanics without copying code. |

## What to extract—and what not to infer

1. **Player-controlled institutions outlast scripted quests.** Guild halls,
   communal stores, charters, ranks, property and rivalries can evolve through
   committed events. Melmoth's player-run clans and Astaria's shared guild
   assets suggest separate, complementary systems.
2. **Geography must have consequences.** Weather and blocked crossings should
   affect known movement, visibility, availability and resource flows under
   deterministic, inspectable rules. AI narration alone must never change them.
3. **Builder access is not owner access.** Melmoth's OLC and Astaria's history
   justify an in-world editing workflow, but neither proves all players had
   unrestricted content-editing rights. In REALM, creation is permissioned and
   world-local, with validation and reversible versioning.
4. **PK requires an explicit host policy.** Melmoth's peace exemption,
   Astaria's historical restrictions and Vampire Wars' unrestricted approach
   conflict. That is evidence for *configurability*, not evidence that any
   single PvP policy suits every REALM server.
5. **Do not equate a remembered name with a proven edition.** The historical
   Vampire Wars family has multiple branches, and archived listings cannot
   identify a particular user's former character/server. Melmoth and Vampire
   Wars are distinct game subjects despite overlaps in fantasy or vampirism.

## Five falsifiable engine candidates

Each candidate is engine functionality, **not** a Siltglass-specific rule.
An independently authored worldpack may opt in without introducing an engine
dependency on the flagship world. All persistent changes must follow a
server-owned **PROPOSE → VALIDATE → COMMIT** path; AI models never grant their
own permissions or certify their own tests.

### 1. Online world creation with bounded builder authority

**Proposal:** A realm owner grants a named builder the right to propose
room/exit/object/NPC changes limited to that realm. Validate references,
ownership, schema and traversal before an atomic world revision commit.

**Acceptance tests:** A builder's valid new room and reciprocal exit appear
after restart and export/import; an invalid exit is rejected without mutation;
a builder cannot write another realm or execute arbitrary host code; a failed
proposal leaves the previous revision available for rollback.

### 2. Player guild assets and inventory transactions

**Proposal:** Guild charters, member roles, shared vaults, guild chat and
properties with an engine-owned transaction ledger.

**Acceptance tests:** Concurrent withdrawal of one item produces exactly one
successful receipt; unauthorized/nonmember withdrawal fails; ownership and
audit trail survive restart; exporting a world does not leak another world's
private guild assets.

### 3. Weather as a reproducible environment rule

**Proposal:** Host-owned weather state modifies declared travel costs,
visibility and perhaps healing/combat, independently of NPC generated text.

**Acceptance tests:** Given the same seed, world revision and ordered events,
identical weather outcomes and movement rules replay; changing a descriptive
NPC sentence cannot affect weather; edge cases do not strand players in an
inaccessible location without an explicit world rule.

### 4. Faction governance and contested holdings

**Proposal:** Factions own bounded holdings and can propose charters, votes,
alliances, economic changes or defense commitments. NPCs can suggest actions
but only eligible authorized actors can commit them.

**Acceptance tests:** Conflicting proposals are resolved by a version check;
unauthorized votes and self-certified AI decisions fail; a timed siege produces
one reproducible state transition and a durable historical event visible to
both returning players and authorized NPC contexts.

### 5. Per-realm player-killing contract

**Proposal:** The server owner chooses among `disabled`, `consensual`,
`restricted` and `unrestricted` PK modes, with explicit safe-zone, level-gap,
loot/loss and harassment-mitigation policy fields.

**Acceptance tests:** Each rule mode rejects prohibited attacks before combat;
disabled and consensual modes do not silently permit theft or retaliation;
policy edits require owner authority and version checks; independent realms
can choose different modes without cross-contamination; failed kills produce
no damage or durable kill receipt.

## Hostile review and rights boundary

> A convincing NPC speech transcript is not evidence of a living world. A
> meaningful demonstration needs a host-committed event, authorized NPC
> response, deterministic downstream effect, subsequent memory retrieval,
> restart readback and falsification tests.

> “Players can build” is dangerously ambiguous: designing a room, placing an
> item, editing persistent scripts, changing engine rules and administering a
> host are five different authorities. Do not collapse them into one command.

> PK advertised as unrestricted is a historical fact about a particular game,
> not a product recommendation. REALM must permit realm-local policies while
> protecting users from rule surprises and privilege escalation.

Recreate only general gameplay ideas through original code and original names.
Do **not** place Melmoth locations, Astaria race/guild lore, GodWars source or
World of Darkness factions/Disciplines/Storyteller content into Siltglass
Reach or a redistributable starter pack. The Dark Pack is a conditional fan
policy, not a general engine embedding license. Preserve citation/provenance
when documenting inspiration; require a fresh rights review if a separate
fan-world pack is later contemplated.

**Evidence ceiling:** source/archival inspection and independent design
proposal only. No historical game login, old server runtime test, implementation,
host deployment, license clearance or REALM feature qualification was performed
by this research lane.
