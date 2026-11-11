# REALM local presence witness: opt-in transport integration

The local transport integration consumes `realm.local_presence` in Combat,
the authenticated HTTP gateway and player-bound Telnet. It is opt-in host
configuration, not an installed network service or authorization to publish.
Without an injected witness, default `allow_offline_targets=False` still
denies attacks. Both packaged transport CLIs now support **explicit local
host activation**; the equivalent trusted Python injection remains available:

```sh
# One-time host-only PvP policy installation for the existing world. This
# deliberately refuses a second installation and never enables offline attacks.
PYTHONPATH=src python -m realm.gateway --db realm.sqlite3 --world-id demo --scoped-credentials --install-pvp-policy

# OR, on a new world without an installed combat policy, protect an existing
# spawn room and select a host-controlled original PK announcement style:
PYTHONPATH=src python -m realm.gateway --db realm.sqlite3 --world-id demo --scoped-credentials --install-pvp-policy --pvp-safe-room atrium --pvp-announcement-style black_bell

# Issue scoped player credentials through trusted local CLI stdout. Keep the
# resulting bearer strings private; each player must opt in with `pvp on`.
PYTHONPATH=src python -m realm.gateway --db realm.sqlite3 --world-id demo --scoped-credentials --provision-scoped-player Ada
PYTHONPATH=src python -m realm.gateway --db realm.sqlite3 --world-id demo --scoped-credentials --provision-scoped-player Bob

# Run a loopback-only gateway with 10-second scoped-HTTP activity evidence.
PYTHONPATH=src python -m realm.gateway --db realm.sqlite3 --world-id demo --scoped-credentials --enable-local-presence --port 8768
```

**Important:** Without `--pvp-safe-room`, the one-time policy uses no protected
rooms; the starter atrium is *not* automatically a safe spawn. A host can
repeat `--pvp-safe-room ROOM` for existing room IDs during the one-time CLI
install, or use trusted Python `Combat.configure` with explicit worldpack room
IDs. Unknown/duplicate rooms reject the entire policy transaction. The
shortcut never overwrites a prior installation. New/returning characters still need to
join and explicitly consent (`pvp on`); merely hosting the listener does not
silently opt anyone in. None of these commands makes a public HTTPS service.

Host-owned `Combat.configure(..., announcement_style=...)` also selects one
original REALM PK broadcast style when installing the policy: `herald`
(default), `stark`, `black_bell`, `omen`, or `taunt`. The CLI shortcut defaults
to `herald` but accepts `--pvp-announcement-style`; players cannot choose or
inject announcement text. Every style
uses only the server-verified killer and victim names, stores its immutable
template ID on the fatal event, and broadcasts without the fight room. The
additional styles are new REALM writing, **not verified quotations from
Melmoth**. A historical world retains the exact text emitted at death even
if future source templates change.

Nonlethal hits appear as room-local combat messages to participants in the
current room; they do not expose action IDs or private remaining HP. Only
fatal PK announcements go to the redacted global feed.

Idempotent `pvp on/off` receipt replays never reapply a superseded decision.
When current consent differs from the original command, the response says
the old command was superseded and reports the present ON/OFF state, rather
than falsely acknowledging an old opt-out as active protection.

Optional separately launched player-bound local Telnet:

```sh
# Telnet player credentials are a distinct credential store; provision from
# the host, not through Telnet network registration.
PYTHONPATH=src python -m realm.gateway --db realm.sqlite3 --world-id demo --provision-player Ada
PYTHONPATH=src python -m realm.telnet --db realm.sqlite3 --player-credentials --enable-local-presence --port 7777
```

Standalone gateway and Telnet processes each own **different in-memory
witnesses**. They cannot attest each other's players and cross-process attacks
therefore fail closed. A combined trusted Python host must share *one* witness
between adapters if it wants cross-transport fights. Telnet is plaintext;
keep it loopback-only, never port-forward. The flag is rejected outright in
legacy shared-token mode.

Host injection is explicit:

```python
witness = LocalPresenceWitness(gateway.engine, gateway.world_id, idle_seconds=10)
http = GatewayServer(gateway, allowed_origins, port=0, presence_witness=witness)
telnet = LocalTelnetServer(Engine(gateway.engine.db_path), None, 0,
                           player_credentials=True, presence_witness=witness)
```

The two transports must use one witness in the same process and world. A
standalone HTTP-only world may pass the same witness without Telnet. These
constructors do not enable PvP policy or grant any credentials themselves.

## Exact intended claim

An admissible lease attests only that a **trusted host handler recently saw
activity on an already authenticated, principal-bound session**, then
revalidated that credential under the world's SQLite writer lock. It cannot
prove a human is at the keyboard, a remote TCP peer has no half-open failure,
or another process's socket state. Legacy shared-token Telnet does not bind
player identity and must never create one.

The helper does not persist tokens, emit a gameplay command, install tables,
write a presence row, or create a public `online` API. A host-provided
credential resolver callback **may retain a bearer in process memory** until
its lease is closed/expired; minimize retention and never log or persist it.
All leases start empty after each process start and fail closed on
missing/stale evidence.

## Wave 28 integration seam, in dependency order

1. **Host ownership.** Construct exactly one `LocalPresenceWitness(engine,
   bound_world_id)` from a trusted host that owns the corresponding world
   engine. Do not use a global class-level registry or transfer a lease
   between worlds, hosts, or processes. For independent Telnet/HTTP processes,
   no shared proof exists: fail closed rather than assuming shared SQLite
   positions mean shared socket presence.
2. **Principal-bound Telnet.** Only `LocalTelnetServer(player_credentials=True)`
   may admit `telnet-player-credential` after successful credential-bound
   `join_exclusive`. Pass a resolver that SELECTs the actual player for the
   presented token digest **inside the witness-owned writer**, returning that
   principal. The legacy shared-token mode has no admissible resolver.
   Call `lease.close()` unconditionally on disconnect/exception. A trusted
   handler may periodically refresh while it actually owns and monitors the
   authenticated socket, including while the player is idle; it must stop
   on EOF/revocation/handler exit. A periodic socket read timeout is a host
   maintenance tick, not proof that a half-open peer remains reachable.
   Do not refresh from an unobserved saved
   `_active_names` flag alone, client `online=1`, or durable room location.
3. **HTTP scoped only.** A successful authenticated `/api/v1/feed`,
   `/api/v1/feed/wait` or `/api/v1/command` request may admit or refresh
   `http-scoped-request`, using
   `ScopedCredentialStore.authenticate_in_writer(db, token,
   required_capability="feed:read" or "command:write")` to return the real
   principal. Close at host shutdown; revoked or idle leases are rejected and
   removed on the next check. Refresh only on new authenticated
   client activity. The host must pool at most one lease per current scoped
   credential fingerprint for each canonical actor and bound world, capped by the witness's
   `max_sessions` (default 256); repeatedly admitting each 2-second poll
   would exhaust the cap. A bearer token's mere existence or a past `join`
   is insufficient. The accepted ~10-second recent-authenticated-activity
   window is **not continuous online presence**, and can briefly count an
   already-closed browser as recently active. Configure `idle_seconds=10`
   for that host policy; expose the tradeoff to its players.
4. **Combat writer integration.** With `allow_offline_targets=False`,
   call `require_pair_in_writer(db, actor, target)` **from the same combat
   transaction before damage or event creation**. If no witness was explicitly
   injected, deny. Recheck both player-bound sessions on every new attack.
   Existing idempotent action-ID replay must create no new damage.
5. **Cross-adapter proof.** Run authenticated Telnet↔Telnet, HTTP↔HTTP and
   cross-adapter live overlap through the same host process. A separate
   gateway process must fail closed without shared runtime authority. A
   signed interprocess broker, if later desired, is a separate design and
   requires proof against forgery/replay/revocation before use.

## Hostile qualification contract

- No socket; saved `players.room_id` still exists → attack denied.
- Telnet shared-token `Name:` impersonation → no lease.
- Token for Ada presented as Bob → denied at admission; no misleading lease.
- Bob credential rotates/revokes between admission and attack → denied in
  attack writer; old lease cannot refresh.
- Scoped HTTP combatant idle past its 10-second bound or Telnet socket
  disconnected/handler crashed → denied. An authenticated open idle Telnet
  socket is host-refreshed and remains eligible beyond that same idle bound.
- Host restart, process split, different SQLite DB path/world ID → denied.
- Two active sessions with one expired credential → remaining genuinely valid
  session permits that principal; no forged third-party status.
- Concurrent disconnect versus attack → linearize once at writer check;
  disconnect before check denies, disconnect after accepted damage does not
  retroactively erase a committed attack.
- HTTP client goes silent past the ~10s authenticated-request window or moves
  between hosts → denied; a recently closed browser may remain eligible only
  until that bound expires. Never infer literal connectivity merely from
  an active bearer.
- Browser, raw HTTP/Telnet, resident model, clock task cannot mint, renew,
  or claim a presence lease directly.
- Preexisting Python/browser suites must still pass on the exact frozen
  integration head, plus focused actual-socket/cross-process tests.
- The Wave 32 source tree `b9aec2605a81795b6d33c5f4cf4c2c0d07374045`
  (`f127fbff7f3e4ad5088ce3f177c6d2b35518f359`) passed isolated Lappy
  Windows Python **906/906** and browser/Node **65/65** on 2026-11-11.
  Any subsequent integration tree still requires its **own** exact-head
  qualification. These source tests do not prove a deployed public host,
  long-lived player-session security, or all production socket conditions.

## Known engineering tradeoff

A host-monitored Telnet socket may remain eligible during quiet user input
if its own handler refreshes the lease, but half-open TCP detection remains
imperfect and prompt disconnect cleanup is essential.
HTTP recent-request TTL supports consecutive browser polls without requiring
in-flight overlap, at the cost of an explicitly bounded stale-presence window.
Neither tradeoff should be silently hidden by setting
`allow_offline_targets=True`.
