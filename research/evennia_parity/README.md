# REALM / Evennia parity — executable smoke result, 2026-11-07 UTC

This is a **disposable comparison fixture**, not a production service, REALM
change, or finished engine-parity certification. All work used free software in
an isolated workspace; no Lappy, NAS, GitHub branch, cloud service, or existing
player world was modified.

## Versions and currentness

- REALM GitHub `main` checked with `git ls-remote`:
  `09fc7cf6fcd8f7826c315d69c045467f27a7c8c7`.
- Python: `3.12.14` (isolated virtual environment).
- Evennia: pinned `6.1.0`; Django `6.0.10`, Twisted `24.11.0`.
- `python -m pip index versions evennia --timeout 4 --retries 0` confirmed
  6.1.0 available; `pip install 'evennia==6.1.0'` exited 0.
- Shared venv footprint: `193M`; each generated game dir: `5.5M`, containing
  a separate `server/evennia.db3` (~684K at initialization).

## Reproduction

The following steps already ran successfully in this scratch workspace:

```sh
python -m venv .venv
.venv/bin/python -m pip install 'evennia==6.1.0'
.venv/bin/evennia --init world_a
.venv/bin/evennia --init world_b
(cd world_a && ../.venv/bin/evennia migrate --noinput)
(cd world_b && ../.venv/bin/evennia migrate --noinput)
python probe.py --sequential
```

Each game's `server/conf/settings.py` was modified to restrict all enabled
listeners to loopback, disable web and WebSocket listeners, and give each game
unique Telnet and AMP ports: A=41400/41406 and B=42400/42406.

The game DBs contain a **synthetic, random-password** first owner account,
`realm_spike_a` or `realm_spike_b` respectively, created using Django ORM.
Their passwords were not logged or retained externally. No real identities
or private story material entered the fixture.

## Observed results

| Check | Result | Evidence class |
| --- | --- | --- |
| Two fresh game dirs initialize and migrate | PASS | Host-executed local test |
| Account `realm_spike_a` visible in world A | PASS (1 account) | Django ORM query |
| Account A visible in world B | NO (B has its own one account) | Django ORM query |
| Account B visible in world A | NO | Django ORM query |
| World A starts and responds on loopback Telnet | PASS: 41400, 27 bytes | Actual socket probe |
| World B starts and responds on loopback Telnet | PASS: 42400, 27 bytes | Actual socket probe |
| Sequential cold launcher startup | 6.125s A / 6.167s B | Measured in fixture |
| Sequential stop | Exit 0 for both | Actual Evennia launcher |
| Post-test server/portal status | NOT RUNNING for both | Actual launcher |
| **Both worlds running concurrently** | **FAIL / UNVERIFIED** | Second portal timed out |
| Process RSS | UNKNOWN | PID status inaccessible in sandbox |

The concurrent attempt `python probe.py` consistently reached A's startup but
B returned:

```text
Portal starting ...
Connection to Evennia timed out. Try again.
```

Different Telnet/AMP ports were confirmed by active config, and stale
fixture-owned PID files were cleared before both startups, so neither simple
duplicate-port settings nor stale-PID files alone explain the failure. A
launcher/global-process/sandbox interaction is **hypothesis**, not root-cause
proof. Subsequent sequential startup/teardown succeeded for both.

The probe automatically shuts down attempted worlds in `finally`; current
`evennia status` checks return `Portal: NOT RUNNING` and `Server: NOT RUNNING`
for both. No persistent processes were intentionally left.

For scale context only, REALM's disposable local CLI `--once look` cold command
exited 0 and took ~0.078s. **Do not compare this directly** with Evennia's
complete Portal+Server service start; the workloads and lifecycle differ.

## Hostile assessment / gaps

> Two isolated databases **in two separate Evennia game installations** are
> not evidence that one Evennia runtime natively hosts REALM-style multiworld
> tenancy. Two installations duplicate Portal/Server lifecycle and account
> namespaces. Concurrent operation was **not demonstrated** here.

Evennia's default settings bind enabled Telnet and web interfaces to
`0.0.0.0`; local fixtures were explicitly restricted before any startup.
The documentation also supports customizing Django databases, but that does
not show cross-database account, object or session authority separation in a
single Evennia game. Such a design must prove exact REALM invariants, not
assume Django routers solve it.

Not tested: simultaneous user reconnection, session lifecycle, event-time
private witness grants, command idempotency/replay, model proposal host
validation, cross-process storage fencing, migration safety, recovery/online
backup, full Windows deployment, multiplayer throughput or p95 latency.

**Architectural implication:** Evennia is installable and can host two separate
isolated games sequentially in this environment, but it is not yet a proved
drop-in replacement for REALM's per-world single-host transactional semantics.
Retain the REALM deterministic kernel provisionally and require a bounded
**concurrent** parity test, including memory/operability costs, before migration.
