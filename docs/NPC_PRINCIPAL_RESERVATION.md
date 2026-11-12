# Host-only NPC principal reservation (opt-in)

REALM's generic resident controller and player bearer stores are separate
authority lanes. Without a persistent role reservation, a host could issue
a normal browser/Telnet bearer for a resident character's name. This
source-only mechanism lets the trusted host explicitly reserve an existing
world character as an NPC, then denies its use as a player bearer.

## Local host operation

Use this **only** from a trusted, world-bound Python host process:

```python
from realm.gateway import Gateway
from realm.principal_roles import reserve_npc
from realm.residents import ResidentController

gateway = Gateway("world.sqlite3", world_id="local_world")
gateway.join("Moss")  # host-selected character, not network registration
assert reserve_npc(gateway.engine, "Moss")
resident = ResidentController(gateway.engine, "Moss")
# The host may submit an approved proposal via resident.commit(...).
```

The reservation is not automatic. It requires the existing canonical
player row and a bound `world_id`; it never joins a player or supplies
a model, token, room, action, permission, or public API. A second
reservation is idempotent. The persisted `main.reserved_npc_principals`
table is installed only by an explicit reservation on that SQLite world.

When active for a name, both normal and scoped player-credential stores
reject new bearer provisioning or rotation and deny already-issued tokens.
Legacy and scoped command writer guards re-check the reservation inside
the same world transaction, so a delayed POST cannot commit with a
re-reserved principal. Existing unrelated human players remain unchanged.
Case-insensitive aliases resolve to the same reserved principal. Host-only
`ResidentController` actions still obey the standard Engine rules and
single-action receipts.

## Authority and operational limits

A reservation intentionally **does not delete** preexisting token rows:
their admission is denied, and an explicit host-only recovery/migration
procedure is required to change a character's role. There is no HTTP,
Telnet or model-proposal route to reserve or release a name. Do not
manually delete a reservation and thereby reactivate an old bearer.

This is a locally trusted-host boundary, not a proof against a privileged
database administrator rewriting tables or stealing host authority.
Concurrent in-flight *read* responses may have been admitted before a
role reservation; network ingress still requires locally bound credentials
and a fresh request, and public hosting remains unqualified. An
unreserved `ResidentController` remains a host model adapter, not evidence
that its actor has exclusive NPC status. Future integration can require
explicit reservation for all resident controllers after migration.

Test: `python -m unittest discover -s tests -p
test_reserved_npc_principals.py -q`, plus the full Python and browser
regressions. No private Driftspace/Wreckforge characters or world assets
are included.
