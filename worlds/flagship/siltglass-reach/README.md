# Siltglass Reach — original REALM showcase

Siltglass Reach is a **24-room, 14-item, original fantasy worldpack**
for testing REALM's persistent multiplayer text-world engine. The harbor town
depends on a crossing whose maps and measurements disagree. Two civic groups
argue over whether to keep the route open. Players investigate by moving,
examining rooms, carrying physical clues, and speaking to one another.

It is not a prewritten plot, an imported D&D campaign, Driftspace canon, or
Wreckforge material. All descriptions and props in this pack were newly written
for this repository.

## Start a fresh local world

Use Python 3.11+ from the REALM repository root:

```sh
PYTHONPATH=src python -m realm.worldpack \
  --db data/siltglass-reach.sqlite3 \
  --file worlds/flagship/siltglass-reach/worldpack.v1.json

PYTHONPATH=src python -m realm --db data/siltglass-reach.sqlite3 --player Explorer
```

Join the **same** database in a second terminal with a different player name:

```sh
PYTHONPATH=src python -m realm --db data/siltglass-reach.sqlite3 --player Witness
```

Try `look`, `east`, `south`, `go archway`, `take charred_receipt`,
`inventory`, `drop charred_receipt`, `say I found a conflicting tally`,
`listen`, `who`, and `help`. The two players can inspect and alter the same
world state, and their saved locations survive restarts. The worldpack importer
will not replace a played world with a different declaration.

New portable clues make the setting more interactive with REALM's **actual**
commands: walk east from the Tidegate to the jetty and `take crossing_token`;
the net yard holds `mended_net_tag`; the market has `market_counter`; the
Ranger's Stand holds `weather_log_leaf`; Refuge Hall holds
`refuge_claim_slip`; Boundary Cairn holds `waymark_chalk`. These remain
ordinary inventory items; the engine does not calculate tolls, trade value,
weather, reputation or quest completion.

**Already-imported databases retain their original worldpack hash.** Expanding
this source file does not migrate or overwrite them: import the expanded pack
into a fresh SQLite path for a new playthrough. Do not delete an existing world
to force the new declaration into it.

## World layout

The twenty-four rooms form a six-row, four-column conceptual arrangement.
The northern two rows are the **Quay**, the middle two the **Civic Quarter**,
and the southern two the **Wild Edge**.

| District row | West | Mid-west | Mid-east | East |
| --- | --- | --- | --- | --- |
| Quay, upper | Tidegate | Lantern Jetty | Net Yard | Old Spillway |
| Quay, lower | Knotted Bell | Cargo Ledger Office | Common Forge | Quarantine Steps |
| Civic, upper | Risen Path | Second Bell | Testimony Court | Archive Lobby |
| Civic, lower | Market Crossing | Siltglass Kiln | Refuge Hall | Wind Bridge |
| Wild, upper | Boundary Cairn | Pale Lichen Grove | Fallow Orchard | Ranger's Stand |
| Wild, lower | Quarry Cut | Thread Well | Old Cistern | Fault Arch |

All neighbors inside each two-row district connect through ordinary reciprocal
cardinal exits. There are two crossings between Quay and Civic, and two between
Civic and Wild. A **fixed** special exit labeled `archway` links Old Spillway
and Fault Arch in both directions; this is not a random or conditional portal.
Every location is accessible from the Tidegate.

## Living-world reference — deliberately separate from the engine

[`living_lore.v1.json`](living_lore.v1.json) is an optional, original,
**non-executable design companion**, not part of the `realm.worldpack.v1`
import schema. It describes six potential resident voices (with proposed actor
IDs and bounded persona seeds), three factions,
five disputed barter offers, four atmospheric weather states and four
player-directed quest seeds. Each NPC role has a starting room, limited
knowledge and conflicting motivations. This gives a host or future, separately
authorized neural-agent layer grounded material to work with without making
the model an omniscient source of truth.

| Layer | What players can do today | What this lore does *not* implement |
| --- | --- | --- |
| Geography | Traverse 24 persistent rooms and fixed exits | Procedural areas, conditional passage or weather hazards |
| Clues | Examine, take and drop 14 named portable objects | Shops, currency, resource consumption, crafting or object-use effects |
| Community | Join one SQLite world and speak in room history | Autonomous NPCs, faction membership, reputation or PvP rules |
| Narrative | Compare conflicting evidence and make roleplayed choices | Quest state, scripted outcomes, factual adjudication or NPC memory |

The `Channel Guild` is divided about safe trade, the `Crossing Committee`
about when to act without certainty, and the `Reedward Collective` about
surveying versus repairing damaged livelihoods. Their interests can collide
without a pre-selected villain or automatic combat. The archetypes are
original and **not registered in any live REALM database** by this file.

### Story sparks — player-directed, not scripted quests

1. **Two bells, one warning.** Two meeting places disagree about what the
   crossing means for trade and safety. Have players present a joint warning
   at both places using `say` and the durable room history.
2. **The unbalanced ledger.** Collect the charred receipt, chipped seal,
   fractured tile, and other clues. Debate what each demonstrates and what
   remains uncertain. No automatic verdict or inventory hand-in occurs.
3. **A route worth testing.** Survey the northern quay, the two civic
   crossings, and the southern arch. Describe observations in-game and compare
   competing routes. Ordinary exit navigation is deterministic.
4. **Refuge before certainty.** Gather at the Refuge Hall to agree on a
   cooperative response without pretending the cause of the hazards is known.

The Channel Guild and Crossing Committee are **background civic viewpoints** in
room descriptions, not programmed NPCs or authenticated institutional actors.
An AI participant may eventually propose actions through a separately
validated agent interface; none is included or running here.

The lore companion is not a source of private witness grants. A public room
chronicle, an NPC's remembered observation, a private message and a moderator
audit log would need separate explicit policies. In-character testimony is
**attributed speech**, not independently verified fact.

## Future human–neural co-creation hooks (design only)

REALM's intended direction is a living MUD in which human and neural actors
co-create persistent history. This v1 JSON supports the **setting substrate**,
not the autonomous behavior. Distinctive future NPC roles could include:

- A ferry steward trying to protect trade access while remembering player
  promises about cargo; their reports should identify observed versus hearsay.
- A crossing inspector trying to reduce risk, with memory of route evidence
  and unresolved contradictions, not omniscient knowledge of the passage.
- A refuge keeper balancing food and shelter against uncertain arrivals,
  able to negotiate with multiple players over time.
- A field surveyor recording changing observations, whose drive is to test
  hypotheses and correct earlier mistakes.

These are **proposals for separate schema and engine work**. No NPCs, drive
systems, model calls, autonomous ticks, or long-term agent memories are
instantiated by this pack. Any future actions must go through host-side
identity, capability, validation, and durable commit gates.

## Research influences without importing third-party canon

I used published, first-party MUD architecture descriptions as *design
comparators*, not as source content or permission to copy another setting:

- [Evennia Rooms and Exits](https://www.evennia.com/docs/latest/Components/Rooms.html)
  separates room/exits as world objects; REALM's currently imported representation
  is intentionally smaller: rooms, fixed directional exits, items.
- [Ranvier Bundles](https://ranviermud.com/bundles/) separates game content and
  behaviors; the separate passive Siltglass lore follows that architectural
  *boundary* without importing Ranvier code or behaviors.
- [Achaea's player-killing guidance](https://www.achaea.com/game-help?what=player-killing-pk-or-pvp)
  illustrates why player conflict needs explicit policy. REALM has no combat
  or PK rules here; faction disagreement is roleplay, never a hidden PK flag.

Historical Melmoth, Astaria, GodWars, Vampire Wars and White Wolf references
inform Patrick's desired *experience* (rich text exploration, building,
social friction, persistent history) but are not content licenses, shared
canon, or claims that a specific historical server feature is documented.

## Capability boundary

REALM worldpack v1 defines room names, descriptions, exits, the starting room,
and portable objects. It does **not** interpret dialogue, item significance,
traps, quests, votes, reputation, sickness, time progression, or conditional
travel. This pack deliberately describes interesting uncertainty without
inventing corresponding mechanics. Use a new local SQLite database for each
independent playthrough; public hosting and cloud deployment are not qualified.

For source/rights limits, see [SOURCE_BOUNDARY.md](SOURCE_BOUNDARY.md).
