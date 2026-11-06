# Opt-in NPC duet — source-level reference

**Status: implemented and unit-tested in source, not installed as a running
service.** This is a bounded **host-triggered** conversation between two
registered residents of one locally hosted REALM. It does not download model
weights, run inference by itself, turn on a scheduler, or start a network
listener. This is deliberately separate from the game's core architecture and
from any public player connection path.

For a separately opt-in demonstration with two real local GGUF models, see
[Two local NPC voices: demo CLI](NPC_LIVE_DEMO.md). It includes explicit
artifact checks, bounded CPU inference and persistent SQLite readback, but
does not implement a scheduler or long-term semantic memory.

## Reproduce the deterministic proof without a model

From the repository root with Python 3.10+ and no package downloads:

```python
from pathlib import Path
from realm.engine import Engine
from realm.worldpack import import_worldpack
from realm.conversations import NPCIdentity, NPCDuetRunner, register_npc_pair

db = Path("local-npc-example.sqlite3")  # Use a new local file; do not overwrite saves.
import_worldpack(db, "worlds/flagship/siltglass-reach/worldpack.v1.json")
engine = Engine(db)

steward = "FerrySteward"
inspector = "CrossingInspector"
register_npc_pair(
    engine,
    (
        NPCIdentity(steward, "Ferry steward; cautious about rumors and lost wages."),
        NPCIdentity(inspector, "Crossing inspector; verifies claims against ledgers."),
    ),
    expected_world_id="siltglass-reach",
)

def steward_reply(ctx):
    return {"command": "say Let us check the crossing ledger before opening."}

def inspector_reply(ctx):
    return {"command": "say The entries disagree. We need fresh evidence."}

runner = NPCDuetRunner(
    engine, actor_a=steward, actor_b=inspector,
    providers={steward: steward_reply, inspector: inspector_reply},
)
for turn in runner.run(session_id="local_trial_01", turns=4):
    print(turn.speaker, turn.command, turn.action_id, turn.replayed)
```

If using a source checkout instead of an installed package, set `PYTHONPATH=src`.
Run `python -m unittest tests.test_conversations -q` for host-proposal,
replay, identity-collision, room, world and budget regression checks.

The room and world are **explicit opt-in**; nothing imports Siltglass Reach
automatically. Any user can host their own worldpack/database. The example
names are merely a sample roster and are unrelated to engine architecture or
canon. Choose fresh names before registration. Do not register over an
existing player, even if that player is disconnected.

## Adapter contract for a tiny model

Each provider is a trusted host-defined callable accepting a read-only
`DialogueContext` and returning a mapping with **exactly one** key:

```python
def adapter(context):
    # Host chooses the model process, prompt, timeout and max output.
    # The model's output remains untrusted text; it never receives Engine/DB.
    proposed_speech = generate_text_somewhere_local(context)
    return {"command": "say " + proposed_speech}
```

Only `say` is currently authorized for this runner. `ResidentController`
rejects additional keys (`actor`, `action_id`, `world_id`), movement, item
manipulation, terminal escape sequences, overlong or multiline responses.
The host owns identities, session ID and stable receipt IDs. It checks the
registered identities and shared room before each turn, then checks **both
positions again under the Engine's write lock** when committing. Movement by
either resident during a slow inference rejects the proposal without a speech
event or action receipt. Failed name/world/credential registration rolls back
without creating duet schema tables. Speech is durable in
the world SQLite `events` table; retries of committed turns read their action
receipts rather than generating duplicate speech. For a trial, use one model
process at a time and a bounded turn count to respect small local RAM budgets.

**Not claimed:** distinct model weights or active neural inference, emergent
goals, long-term semantic recall, human/NPC consent, moderation, background
autonomy, separate Python-process isolation, or public network safety. The
two personas and conversation receipts persist in the database; only the last
utterance is placed in each in-memory turn context. Further memory/drives need
separate design, tests and authorization.

This runner accepts arbitrary *host-owned* provider callables. A provider that
itself executes malicious Python is outside its protection boundary; keep
untrusted model generations as data and do not give them direct tools.
