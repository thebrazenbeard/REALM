# Host-signed NPC roster UTC gathering (source-only Draft)

This opt-in handler consumes a host-reviewed RosterProposal and performs each
authorized gather in the SAME SQLite WorldClock.tick transaction. Neither the
queued event nor a model can issue a host HMAC grant.

## Host setup

1. Initialize a bound world, joined NPCs, crafting nodes and WorldClock.
2. Review propose_roster_allocation; its status stays HOST_REVIEW_REQUIRED.
3. Bind complete proposal, sites/utilities, world, exact event ID and UTC
   window of at most one hour in HostNpcRosterPolicy.
4. A trusted host signs the policy signing_bytes under the versioned roster
   permit domain with an out-of-band 32+ byte key.
5. Queue npc_roster_gather with only policy_id and world_id payload fields.
6. Register make_host_roster_gather_handler with an explicit UTC clock.

The handler checks signature, event, physical SQLite main file, world,
NPC actor locations, stock/event evidence, and all receipt IDs before writing.
Non-WAIT actors execute through the WorldClock-owned SQLite writer transaction.
Deterministic per-actor receipts, effects and UTC completion commit or roll
back together. Nested savepoint also rolls back partial writes on caught faults.

## Important holds

- No NPC autonomy, player consent, private lore/canon release or VN2 deployment.
- Host permit keys do not grant Patrick owner/release authority.
- SQLite snapshot rollback needs a separate protected external witness.
- Host callbacks must propagate failures, or a zero-effect clock completion
  could be acknowledged despite the nested roster rollback.
- Welfare allocation is not Nash equilibrium or actor-fair; WAIT-order skew
  remains observed across seeded multiplayer trials.
