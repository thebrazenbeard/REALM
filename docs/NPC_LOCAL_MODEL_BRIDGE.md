# Optional local NPC model proposal bridge

`realm.npc_local_model.LocalChatProposer` offers a narrow, **host-invoked**
HTTP chat-completions client for an already-running, independently approved
local model. It does not install weights, train a model, start a service,
schedule characters, retrieve memory, or grant authority over REALM.

```python
from realm.npc_local_model import LocalChatConfig, LocalChatProposer
from realm.residents import ResidentController

model = LocalChatProposer(LocalChatConfig(
    endpoint="http://127.0.0.1:5001/v1/chat/completions",
    model="operator-approved-local-model",
    timeout_seconds=5.0,
    max_tokens=96,
))
controller = ResidentController(engine, "ExampleNpc")  # prejoined by host
proposal = model.propose("Host-selected, actor-authorized room view")
response = controller.commit(proposal, action_id="host-owned-tick-00001")
```

Endpoint validation accepts only credential-free literal HTTP loopback
`127.0.0.1` or `::1` with an explicit port and path. No DNS hostname, proxy,
redirect, arbitrary URL, or remote provider can be supplied through this
client. The request contains only a bounded host view, a bounded model name,
the system output contract, and capped generation parameters. The response
must fit 8,192 bytes and strictly decode as a single chat choice whose
message contains one JSON object with only `{ "command": "..." }`.
Duplicate JSON fields, actor/receipt injection, malformed commands, and
model-authored authority claims are rejected. The host-side
`ResidentController` and `Engine` validate and commit every proposed action.

`LocalChatProposer.commit` checks the host-owned action receipt before model
inference. A committed retry revalidates the current controller's command
policy and optional transaction `writer_guard`; mutations return their
original idempotent response, while read-only actions observe current world
state. Neither path asks the model again. An ambiguous historical actor-keyed
receipt (which could belong to a player) fails closed before inference. A
new action racing with another writer remains protected by the definitive
receipt check inside its commit transaction. Host-view validation still
applies to every retry. The preliminary probe always rolls back its SQLite
transaction and sets that connection `query_only` after acquiring its lock.
A guard attempting DML in preflight fails closed instead of making a ghost
observation from uncommitted state; only the later definitive command
transaction can commit gameplay effects.

The response body is consumed incrementally against a monotonic deadline
starting at the outbound request. This closes the previous slow-trickle body
gap where a model endpoint could keep sending a byte at a time without
ever triggering its socket inactivity timeout. Header parsing and request
connection still use the socket timeout, not a separately proven absolute
headers deadline; this is not a public-network HTTP client or model
CPU/GPU cancellation mechanism.

The caller must construct an independently authorized scene: the bridge does
**not** know which rooms, messages, history, secret items, or other players
are visible to the NPC. It does not prevent a trusted caller from placing
private text in the host view. One synchronous call may still consume local
CPU/GPU work before the resulting proposal is rejected.

Evidence: `tests/test_npc_local_model.py` exercises a real loopback HTTP
fixture, SQLite, strict JSON rejection, authorization, and response limits.
The slow-response regression uses a real socket fixture in
`tests/test_npc_local_response_deadline.py`.
It is **fixture evidence**, not a real GGUF/model inference benchmark.
