# Opt-in first-officer crew ledger (source-only)

`realm.crew_roster.CrewRoster` adds a **durable, world-bound, host-only
first-officer appointment** to an existing REALM world. It does not create
player accounts, validate a real-world purchase, authorize ship control,
provision a server, publish a route, or install itself automatically.

The generic engine deliberately contains no proprietary ship, station,
character, or fictional canon. A world owner decides which registered ship
represents a particular story asset in their private world.

## Installation and host authorization

A trusted host first creates and joins players, binds one world ID, installs
Geography and registers a ship with its canonical owner. The host must
explicitly install the crew schema on that world:

```python
from realm.crew_roster import CrewRoster

roster = CrewRoster("world.sqlite3", world_id="my_world")
roster.install()
```

The module creates three `crew_role_*` SQLite tables and one trigger that
**deletes the active crew assignment whenever `geo_ships.owner` changes**.
It never modifies a played world merely by importing this module or creating
a `CrewRoster` object. Installation executes within one SQLite writer
transaction; a failed installation is rolled back.

Only a host-provisioned, world-local `CredentialStore` token for the
ship's **current registered owner** can appoint or revoke a first officer:

```python
roster.appoint(
    owner_token,
    ship_id="vessel1",
    officer="JoinedCrewMember",
    action_id="role-0001",
    now_utc=1800000000,
)
roster.revoke(
    owner_token,
    ship_id="vessel1",
    action_id="role-0002",
    now_utc=1800000001,
)
```

Both operations acquire `BEGIN IMMEDIATE` before resolving the bearer token,
current ship owner, joined officer and action receipt. A token rotated or
revoked before that writer transaction cannot authorize an appointment.
Each `action_id` is bound to a particular owner/ship/command; replays return
the historical receipt and **current** `active` status without reapplying
effects. Reusing an ID for a different command is rejected. A different
officer cannot be silently appointed over an existing officer: the current
owner must explicitly revoke first. Concurrent contenders serialize.

Owner change deletes the current role inside that ownership transaction.
If ship ownership later returns to an earlier owner (ABA), the old role does
not resurrect. Historical event rows and action receipts are retained.

## Display semantics

`title_flags.ship_roster_display_name(db, *, actor, ship_id, world_id)`
reads the current same-world crew assignment. It returns `Capt. <name>`
for the currently registered ship owner, `First Officer <name>` only for
the active, owner-matched officer, or the canonical plain player name
otherwise. It is **presentation only**.

The existing short-lived HMAC `OfficerGrant` path remains compatible for
worlds that have *not* installed a crew ledger. Once the ledger is installed,
grant issuance and display must additionally match its active assignment.
Revoking the ledger role immediately invalidates even an otherwise unexpired
signed title. No display flag grants ship departure, admin, chat or economy
privileges.

## Limits and release boundary

> This module is a source-only host seam, **not connected to the existing
> browser, Gateway, Engine look text, Telnet or public login**. The host must
> bind the player identity at each view/use boundary and must not expose
> raw role mutations as unauthenticated client commands.

- Existing `geo_ships.owner` is a host-registered owner, **not proof of
  acquiring a ship through gameplay**. An acquisition/transfer transaction
  requires its own design and approval.
- Only one first officer per ship is supported. No lasting crew history
  outside the event ledger, other ranks, contracts, ship privileges,
  payroll or cross-world role delegation are added.
- Current credentials are long-lived host-provisioned bearers, not short-lived
  capability sessions. Never paste or publish them; local API access is an
  administrative trust boundary, not a secure remotely callable endpoint.
- A privileged process with unrestricted SQLite write access can alter the
  roster, drop triggers, or rewrite evidence. This does not sandbox the host.
- Calling `ship_roster_display_name` is a read of the world database. A
  client-side string must never select which authenticated actor it represents.
- `now_utc` is an explicitly supplied host timestamp, not a trusted
  monotonic clock or proof of causal event order. An external owner change
  performed by direct SQLite administrative mutation is not itself recorded
  as a crew event; the invalidation trigger prevents stale active roles.

Test with real disposable SQLite worlds:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_crew_roster.py -v
PYTHONPATH=src python -O -m unittest discover -s tests -p test_crew_roster.py -q
```
