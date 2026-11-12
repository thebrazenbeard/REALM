# REALM political sites: opt-in resource-backed contest

**Status:** experimental draft, not merged or deployed. Builds on the optional
political-oath module, which must be enabled first. This is a setting-neutral
gameplay rule: factions contest host-defined sites by committing rare items.
It includes no Driftspace private names, worldpack, lore or rights assumptions.

## Trusted host setup (offline, separate invocations)

For an authorized disposable or reviewed world, first configure factions:

    PYTHONPATH=src python -m realm.gateway --db ./data/example.sqlite3 --world-id example --install-politics --political-faction "north=Northern League" --political-faction "south=Southern Compact"

Then define an existing room as a political site and explicitly designate an
existing physical item as an expendable political resource:

    PYTHONPATH=src python -m realm.gateway --db ./data/example.sqlite3 --world-id example --install-leverage --political-site "council@atrium=Council Chamber" --political-resource "token=1"

On Windows PowerShell use the PYTHONPATH environment variable and py -3.
Both operator commands exit without starting the server. Each site is bound to
an existing room; each resource is an **exact item ID**, awarded 1–5 support
points by the trusted host. This is a *setup API*, not a player/model command.
Hosts should back up existing played worlds before installing optional tables.

The host API WorldLeverage(path, world_id).install(), .define_site(...),
.authorize_resource(...) and .configure_batch(sites=..., resources=...)
provides explicit configuration in application code. configure_batch performs
one site/resource transaction: if any declaration fails, all declarations
in that batch roll back. Schema installation remains a separate explicit step.
Do not reprice or relocate previously declared sites and resource IDs.

## In-game, through authenticated player sessions

- sites: list configured public objectives by their IDs and display names
- control council: reveal current control and each faction's resource support
- pledge north: join the existing political-oath system (separate command)
- take token: obtain a designated item through normal inventory custody
- offer token to council: permanently commit the held item **at that room**

The item disappears from active inventory and is preserved as a typed,
world-bound political offering record with the original item name, item
description, actor, faction, site and points. The offer and public room event
are one SQLite writer transaction, alongside the action receipt. If the
event insert fails, the item retirement and offering roll back together.

Control is derived from all **actual** committed faction support:
unclaimed (zero), controlled by a sole leader, or contested (highest-score
tie). Scores are durable through restart. A later oath-break or faction
switch does **not** move historical support or refund an offered item; the
contribution belongs to the faction the actor supported when it was made. An item ID can be offered only
once, including if an authorized host subsequently recreates an item using
that same ID. A replayed action ID never grants new influence.

The offer reply attests only the historical contribution. It deliberately
does not claim present control, since rivals can change control later.
Inspect with control to fetch the current host-derived result. After creating
an action receipt, the writer performs a **final pre-COMMIT readback** of the
spent item, offering, event, and matching receipt. This specifically detects
suppressed receipt insertion, receipt-triggered event deletion, or resurrection
of consumed items. Successful-looking **historical** action-receipt replays
also require the original spent item, scoring row, source event, and matching
receipt to remain consistent. A past contribution is still verifiable after
newer offers, movement, or an oath change; forged future receipts and
noncanonical success strings are rejected. This check prevents the demonstrated
false ACKs but cannot prove safety against privileged post-COMMIT writes,
synthetic event/receipt forgery by a database administrator, or an untrusted
SQLite operator that can rewrite the authoritative ledger itself.

For an executable three-process demonstration (separate local HTTP player
processes and host process, real rival resource offers, betrayal, restart and
cross-actor denial), run `py -3 examples/synthetic_political_rivalry.py` from
the repository root. The script uses only a temporary world and loopback
sockets, closes every client, and deletes its world after verification.

## Authority and fairness limits

- Only existing host-designated item IDs are eligible. A player must have a
  current oath, physically hold the exact item, and stand in the site's room.
- Shared-token/player-chosen-name Telnet sessions cannot mutate site influence.
  Host-owned player-bound HTTP and Telnet use the existing writer-guard model.
- No member automatically receives private chat, ship, weapon, player/NPC,
  territory, wealth, leadership, credential or admin permissions.
- This first slice is resource-backed **control status**: it does NOT yet
  change which rooms a player may enter, grant tax flows, unlock spells or
  offices, confer historical canon, or validate secret alliances.
- Site names and score tallies are public. Room events use existing
  entry/room history filtering; source data never claims global private feed.
- The database owner / privileged SQLite DDL and trigger boundary is not
  a validated hostile-host trust boundary. An installed malicious trigger can
  still forge, rewrite or suppress economic evidence after statements.
- Host replenishment of **new** item IDs affects scarcity. REALM does not
  supply anti-farming policy or a balanced game economy in this slice.
- No remote network exposure, TLS, public transport or VN2 deployment is
  authorized or implied by these tests.

Design target: original resource-based political rivalry and difficult
loyalty choices, not a copy of any copyrighted fictional setting.
See REALM issue #185 and Driftspace's public-safe handoff issue #2.
