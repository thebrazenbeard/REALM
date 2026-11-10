# Ship-history owner response custody

Scope: `GET /api/v1/ship/events` on the loopback `FeedWaitServer`; no
change to ship command authority, credential provisioning, crew roles, or
the ship's durable hull history.

## Revocation race and mitigation

The initial scoped credential and ship-owner check occurs in
`Gateway.ship_events_remote`. That query materializes its JSON events before
the HTTP handler writes the response. Ownership can change after the read.
The principal's `ship:depart` token need not be revoked when the ship transfers.
Consequently, rechecking only the token allowed the former owner to receive
events after transfer.

The HTTP GET handler now performs its existing final bearer/credential
recheck, then asks `Gateway.ship_owner_response_current` for a **new,
query-only SQLite snapshot** of the world binding, player presence,
geography schema and current ship owner immediately before sending `200`.
An absent/non-owner ship fails with the same generic `403` used for an
initially unauthorized history read. Changed world binding fails with `401`.
The transaction is closed before any HTTP bytes are written. The check does
not increment request quotas or rewrite the DB. Header and body deadlines
and existing credential revocation remain separate protections.

`tests/test_ship_owner_response_revocation.py` uses a real loopback socket,
inserts a synthetic event, and deterministically transfers ownership in the
`ship_events_remote` return path *after the authorized read but before response*.
It verifies former owner gets only generic `403`, bearer remains active,
and new owner can read the ship's existing hull log. That historical log
following the **ship** to the new owner is intentional protocol behavior;
it is not crew-delegated access and not a separate privacy defect. The tests
also verify unchanged ownership and a changed world binding after read.

## Remaining boundary

This prevents the reproduced already-transferred-owner disclosure, but is
not an atomic transfer/send protocol. Owner or bearer can still be revoked
in the narrow interval **after the final read-only recheck and during the
HTTP header/body write**; bytes already sent cannot be retracted. Guarantees
stronger than this require a coordinated transfer/send lease or another
explicit protocol-level tradeoff. No such lease was introduced here.

## Evidence and effect

Implementation and tests are source/test evidence, not proof of deployment,
remote accessibility, adversarial network safety or formal linearizability.
This patch is restricted to branch working files; no push, merge, install or
host service mutation is part of this receipt.
