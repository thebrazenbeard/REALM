# Opt-in local foreground host

REALM's browser/HTTP and local Telnet clients can share one SQLite world. The
UTC scheduler does **not** advance from HTTP requests, Telnet commands, or a
browser timer. The host explicitly opts in to a bounded foreground pump.

`realm.local_host_runner.run_local_host` composes the existing loopback
`GatewayServer`, `LocalTelnetServer`, and transactional `WorldClock` with the
existing `clock_runner.run_foreground` policy. It does not install a service,
run at import, enroll an NPC, create credentials, load a worldpack, invent a
clock speed, expose the host, or turn on atmospheric/ship/death mechanics.

## Host integration example

This is an embedding example, **not** a public server startup command. Only
use an already authorized, initialized one-world database. The host supplies
its own credential provisioning, origin policy and approved event handlers:

```python
import os
import threading
from pathlib import Path

from realm.engine import Engine
from realm.gateway import Gateway, GatewayServer
from realm.local_host_runner import run_local_host
from realm.telnet import LocalTelnetServer
from realm.world_clock import WorldClock

world_db = Path("my-explicitly-authorized-world.sqlite3")
gateway = Gateway(world_db, world_id="my_world")
clock = WorldClock(world_db)
clock.enable()  # host choice, AFTER any chosen worldpack import/binding

# The host owns these existing credentials; never log or check them into git.
local_telnet_token = os.environ["REALM_LOCAL_TELNET_TOKEN"]
http = GatewayServer(gateway, ("http://127.0.0.1:8000",), port=8768)
telnet = LocalTelnetServer(Engine(world_db), local_telnet_token, port=7777)

def approved_host_event(db, event):
    # This example explicitly authorizes NO gameplay effect. Real handlers
    # must validate actor/command/resource rights before using supplied db.
    # Never commit/rollback/close db or execute untrusted payload as commands.
    return None

stop = threading.Event()
try:
    report = run_local_host(
        http, telnet, clock, {"approved_noop": approved_host_event},
        pulses=60, interval_seconds=1.0, batch_limit=25,
        stop_event=stop,
    )
except KeyboardInterrupt:
    stop.set()  # listeners also close in the runner's finally block
```

The host schedules an event separately using
`clock.schedule(event_id, due_at=<aware UTC datetime>, kind="approved_noop", payload={})`.
Unknown kinds fail closed. HTTP uses separately provisioned, player-bound
bearer credentials; the Telnet access token is a **shared local gate**, not a
player identity guarantee. Both transports bind `127.0.0.1` only, and the
runner verifies that both point at the same SQLite database as the clock.

The static HTML client (`docs/index.html`) is a separate frontend asset; the
runner does not silently start a third static-content server. For local testing
only, a host can explicitly serve it in another foreground terminal:

```sh
python -m http.server 8000 --bind 127.0.0.1 --directory docs
```

Then open `http://127.0.0.1:8000/` and provide the matching gateway URL and
the already provisioned player bearer in the client. The example HTTP allowed
origin above matches this local static server. Stop that separate process
explicitly; this is not a deployment recipe.

## Lifecycle and failure behavior

- The call blocks in the invoking thread. It starts two request-loop threads,
  runs **1–1000** pulses at **0.01–3600** seconds per pulse, batch size **1–1000**,
  and caps a single call at **100,000** potential event dispatches.
- The scheduler uses host UTC for event deadlines/receipts; monotonic seconds
  only pace pulses. UTC rewind/large-forward-jump protections still apply.
- Stop via the supplied `threading.Event`, a bounded run's completion, or an
  exception. The runner joins both request-loop threads, closes both listening
  sockets, interrupts existing HTTP keep-alive and Telnet sessions, and waits
  for their handler workers to exit before returning. This prevents previously
  accepted sessions from writing to the world after the host has stopped.
  A fresh invocation needs **freshly constructed** server instances.
- The number of UTC pulses is bounded, but shutdown time is **not guaranteed
  bounded** when a trusted handler is already running noninterruptible Python
  or SQLite code. The runner waits for that handler to exit before reporting
  stopped; an indefinitely hung handler can therefore block the foreground
  process indefinitely. A hard kill/deadline requires separately authorized
  isolated-process containment, which this module does not install.
- An event handler's exception rolls back the whole scheduler batch, including
  receipts and in-database effects. A later foreground call can retry the
  pending event; it must not replay an already acknowledged event.
- The host owns server construction, explicit `clock.enable()` and handler
  admission. Passing the already-bound servers to `run_local_host` transfers
  listener cleanup ownership immediately: even rejected preflight arguments
  close both sockets so the host can construct fresh servers and retry.

This demonstrates local foreground ticking, not a persistent simulation,
multi-host consensus, remote/public security, model inference, ship-pressure
physics, or a playable production deployment. Telnet is plaintext even on
loopback; do not port-forward it. No permanent-death rules are activated.

Run the deterministic regression and full suites from the repository root:

```sh
python -m unittest discover -s tests -p test_local_host_runner.py -v
python -m unittest discover -s tests -q
python -O -m unittest discover -s tests -q
node --test tests/browser/test-client.cjs tests/browser/test-map.cjs
```
