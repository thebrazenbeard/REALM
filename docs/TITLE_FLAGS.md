# Ship-derived display labels (source-only, setting-neutral)

This is an **opt-in local presentation policy**, not a crew-management feature,
ship-control credential, NPC runtime, character creation route, world migration,
or playable Driftspace deployment.

## Contract

- `ship_display_name(db, *, actor, ship_id, world_id, ...)` reads existing
  `main.players`, bound `main.world_meta`, and optional `main.geo_ships`.
  Without an exact same-world, canonical joined player and current persisted
  ship-owner match, the result is the ordinary player name.
- The registered current ship owner renders `Capt. <name>`. Ship
  registration is a **host-admin operation**, not proof of winning/purchasing
  a ship in gameplay. A world must implement and verify the acquisition
  transaction before claiming a rank was *earned*.
- A non-owner can render `First Officer <name>` only with a short-lived
  `OfficerGrant` signed by a separate host-only key (minimum 32 random
  bytes), bound to that world's ID, ship, *current* owner, canonical player
  and UTC expiry. The HMAC-SHA256 signature is verified at rendering. Grants
  last no more than one hour and become unusable after ship owner changes.
- The module makes no SQL writes, creates no crew table, and cannot grant
  `ship:depart` or any other gameplay capability. Client-supplied rank text,
  `actor` IDs, or raw grant payloads are never proof of identity.
- A generic world without Geography installed remains operable and shows
  plain player names.

## Host wiring required before gameplay

1. Authenticate the requester to the actual persisted, same-world principal.
   Pass that **server-resolved** principal, never an unauthenticated client
   claim, to the formatter.
2. Create a real crew assignment/role-revocation ledger and corresponding
   host-only, transactional authorization for rank appointment. This module
   deliberately does **not** choose who may appoint the first officer.
3. Only *after* that commit, the trusted host may call
   `issue_first_officer_grant`; keep `signing_key` outside world snapshots,
   client assets, and repository. Do not put the grant issuance API on HTTP
   or Telnet without independent authentication and authorization.
4. Derive `now_utc` from the host's trusted clock, never a player request.
   For immediate crew removal, rotate/invalidate the external signing key or
   add a durable revocation epoch checked against the authoritative roster.
   A grant cannot be instantly revoked merely by changing client UI state.
5. Render labels in HTML as text nodes, not injected markup. Treat every title
   as presentation: action handlers must revalidate world, identity,
   ownership and capability in their own SQLite writer transaction.

## Deliberate limits

- **Not integrated** into `Engine._look`, Gateway, Telnet or browser UI.
  Tests prove the isolated policy only; they do not prove in-game title display.
- Current `geo_ships.owner` stores a host-assigned principal, not a separately
  verified acquisition deed or owner-transfer receipt.
- Officer grants are short-lived bearer presentation proofs, not persistent
  roster records. A server clock rollback or full-world and external-key
  rollback can extend old proofs; the live platform needs a monotonic UTC
  authority or revisioned revocation state before treating this as durable
  crew policy. Key material must remain host-private.
- No fictional setting, copyrighted world text, proprietary ship, or character
  canon is embedded in this public REALM engine component.

Run:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p test_title_flags.py -v
PYTHONPATH=src python -O -m unittest discover -s tests -p test_title_flags.py -q
```
