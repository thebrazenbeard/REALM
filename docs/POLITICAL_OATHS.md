# Optional political allegiances — development source

Status: **Draft/review hold.** Not merged into REALM main or installed in a
running world. No private creative canon or Driftspace content is included.

## Purpose

REALM can preserve host-defined factions, player pledges and public oath
breaking. This supplies persistent choices and reputation history across
sessions, without implying a political regime, earned leadership or automatic
trust. It is a first playable foundation, not a complete political simulation.

## Host-only configuration

An authorized local host can configure a NEW or approved isolated world,
without starting any HTTP or Telnet listener:

    PYTHONPATH=src python -m realm.gateway --db ./data/politics.sqlite3 --world-id sandbox --install-politics --political-faction "north=Northern League" --political-faction "south=Southern Compact"

Windows PowerShell: set $env:PYTHONPATH = "src", then use py -3 instead of
python. The host API WorldPolitics(path, world_id=...).install() and
.create_faction(id, display) provides the same explicit configuration.
Neither politics nor factions are installed by an ordinary game command or
a read of a world. Backup and verify any existing played-world DB before
a host elects to change its schema.

## Player actions

- factions — host-defined names and current pledge counts
- allegiance — inspect your own current oath and number of broken oaths
- allegiance Player — inspect another joined player's public status
- pledge faction_id — swear exactly one active, public oath
- renounce — break your active oath; its historical trace persists

Political pledge and break records join normal room event history and
authenticated room feeds. Legacy shared-token/player-chosen-name Telnet
sessions cannot author oaths; player-bound clients can. Old action IDs
do not reapply stale affiliation after a break; competing writes are
serialized in the world writer transaction.

## Boundaries

A pledge never grants faction-chat, ship control, host roles, credentials,
territory, monetary benefits, resurrection or NPC identity. Those have
separate host authority rules. Political state is per SQLite world.
No model owns the ledger, and no autonomous NPC political mind is claimed.
Privileged malicious SQLite triggers are not fully qualified against
counterfeit post-receipt effects. No external listener/TLS deployment follows.

The future objective from REALM issue #185 is emergent conflict:
leverage, rival alliances, consequential treaties, difficult loyalties
and contested succession grounded in actual game events—not cosmetic
faction tags or borrowed fictional lore. Those mechanics are **not yet
implemented**.

Driftspace integration/ownership boundary: public-safe coordination on
https://github.com/thebrazenbeard/driftspace/issues/2. REALM owns
neutral rules; Driftspace owns its private characters, rights and canon.
