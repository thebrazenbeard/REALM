# REALM synthetic-host NPC UTC gathering policy (source-only)

**Scope:** opt-in, foreground, one-world NPC **gather** ticks. This is not
player/owner consent, not a neural NPC, not an autonomous provider, and not an
installed service. It consumes the corrected core gameplay / UTC clock and the
read-only economic proposal + in-transaction verifier.

## Execution contract

1. A trusted host (outside the queued event and model output) owns a secret
   symmetric key and registers existing NPC player IDs, a bounded economic
   policy and an exact `WorldClock` event ID. The host must keep the key out
   of event payloads, prompts, logs and source control. HMAC possession proves
   only this synthetic host-policy capability, **not Patrick's permission**.
2. The immutable `HostNpcGatherPolicy.signing_bytes()` binds the world ID,
   specific actor and opponent, permitted resource sites, random seed,
   `NPC_GATHER_ONLY` scope, exact event ID, event kind, and host UTC window.
   HMAC-SHA256 uses domain `REALM/NPC-HOST-PERMIT/v1\0`. Host policy creation
   alone is not authorization; the action requires a valid HMAC and a
   caller-supplied trustworthy host UTC instant within the window.
3. The host enables an imported world's `WorldClock` and schedules that exact
   event with kind `npc_economic_gather` and payload **only**
   `{"policy_id":"...","world_id":"..."}`. Actor, command, tool/provider
   identity, sites and permission are deliberately excluded from the queue.
4. `make_host_npc_gather_handler(...)` snapshots the host event-ID registry,
   and returns a callback for `run_foreground(...)` under the precise event
   kind. No daemon, executor, timer, model runtime or network is launched.
5. The callback checks host HMAC, UTC bounds, the exact durable pending
   `main.clock_events` row, world-scoped SQLite connection, existing player
   IDs, and replay/action receipts. It calls `choose_economic_site` on the
   current world, revalidates under the same writer transaction, then performs
   `gather MATERIAL` through `clock_gameplay.perform_in_transaction`. The
   gameplay action, resource stock/inventory, and WorldClock completion
   receipt commit atomically; exceptions roll back the batch.

### Host-only integration sketch (not deployed)

```python
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from realm.npc_game_theory import ResourceSite
from realm.npc_utc_policy import HostNpcGatherPolicy, make_host_npc_gather_handler
from realm.clock_runner import run_foreground

# Bind these to the same initialized/imported world DB; do not take the
# following IDs/key from a queued event, NPC text, ChatGPT plugin, or prompt.
host_key = host_key_from_secure_host_store()  # Provided by the host, not REALM
due = datetime.now(timezone.utc)
policy = HostNpcGatherPolicy(
    policy_id="round_0001", world_id="host_world_id", actor="Ada",
    opponent="Ben", sites=(ResourceSite("atrium", "iron"),
                            ResourceSite("atrium", "copper")), seed=7,
    event_id="npc_ada_round_0001", not_before_utc=due,
    expires_at_utc=due + timedelta(minutes=5),
)
mac = hmac.new(host_key, b"REALM/NPC-HOST-PERMIT/v1\x00" + policy.signing_bytes(),
               hashlib.sha256).hexdigest()
clock.schedule(policy.event_id, due_at=due, kind="npc_economic_gather",
               payload={"policy_id": policy.policy_id, "world_id": policy.world_id})
handler = make_host_npc_gather_handler(
    engine=engine, crafting=crafting, host_key=host_key,
    grants_by_event_id={policy.event_id: (policy, mac)},
    now=lambda: datetime.now(timezone.utc),
)
report = run_foreground(clock, {"npc_economic_gather": handler}, pulses=1)
```

Host controls the key, registration, event creation, scheduling and foreground
execution. There is no public route that lets an NPC mint its own host grant.
An unregistered event is **HELD** (raises and preserves the pending queue row).

## Honest limits

- The scheduler advances only when a host calls it; **UTC is simulation time
  authority**, not proof of continuous host uptime or realtime latency.
- SQLite gives serializable one-world writes, not cross-world consensus.
  Restoring an old entire DB file can also restore older receipts; external
  witness/anti-rollback is a separate release gate.
- The strategy's anti-coordination forecast is an exact deterministic score
  of a Laplace-smoothed *past-gather frequency*, not a guarantee of equitable
  real-time access, opponent intention, economic optimality or learning.
- Tested here: two competing actors with real scarce inventory and receipts,
  concurrent foreground pulses, restart replay, forged event/actor/world/seed,
  expired host UTC grant, missing permit, missing actor, TEMP schema spoof,
  writer lock contention, interrupted host, unknown event kind.
- The current policy supports **gather only**. Crafting or navigation require
  separate host-scoped action and material rules. No ALQS/QSD, atmosphere,
  decompression or canonical Driftspace content is changed by this slice.
- Integration depends on the exact corrected REALM PR80 gameplay, PR81
  strategy, and PR84 foreground clock runner ancestors. The draft branch is
  review evidence, not authorization to merge, deploy, or start VN2 workers.
