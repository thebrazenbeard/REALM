# Opt-in event-grounded actor memory (prototype)

**Status: implemented local host adapter, not a complete NPC memory service.**
`realm.actor_memory.ActorMemory` stores host-issued observation grants and
optional **unverified** one-line summaries for source events. A model cannot
declare its own witness status, select another world, grant itself older room
history, or turn a character's speech into verified world truth through this
adapter. No local model, scheduler, autonomous loop or remote service is
connected here.

## Minimal host-driven flow

Import a worldpack into a fresh database **before** enabling optional memory
tables, or deliberately use the built-in demo database. The first-time importer
validates an exact pristine SQLite schema and rejects extra tables. Ensure a
durable matching `world_id` binding through the importer or `Gateway` first.

```python
from realm.actor_memory import ActorMemory
from realm.gateway import Gateway

host = Gateway("my-world.sqlite3", world_id="demo")
host.join("Moss")
host.join("Ember")
memory = ActorMemory(host.engine, world_id="demo")

committed = host.command("Moss", "say The passage is safe.", "host-turn-1")
memory.grant_latest_event("Ember", committed["cursor"])
print(memory.recall("Ember"))
# Speech is attributed to Moss, not proof that the passage is safe.
```

The host must issue a grant **immediately after the committed event, before
the next unrelated action**. The source must be the event-ledger head, except
for the immediately preceding `leave` event in a validated atomic `leave/move`
pair. A late arrival or later replay of `Engine.history` cannot produce a
retrospective grant. Missing, rolled-back, unknown/private-shaped or tampered
events are rejected or hidden. The adapter validates that the observer's last
recorded `join`/`move` placed them in the source event's room **at event time**.

The host must additionally decide **real session presence, sensory access,
consent, privacy class, and retention** before calling the grant method. A
saved room position is not evidence that somebody was online or heard speech.
These policies are not implemented by this adapter, and the source `events`
schema does not yet carry explicit private-message recipient grants.
Consequently only known public core event shapes (`join`, `move`, `leave`,
`say`, `take`, `drop`) can be granted; arbitrary `whisper`, expanded `say`
payloads, and future kinds fail closed until specifically reviewed.

`add_summary(observer, event_id, text_unverified)` may append **one** bounded
derived note after an active grant. Retrieval always presents original event
evidence separately, with `evidence_scope` set to either `committed_event` or
`utterance_only_not_claim_truth`. The note is labelled
`derived_summary_unverified`; it is never an event receipt, observation proof
or authority for world mutation. The host can revoke a grant, and subsequent
recall (including after restart) suppresses both the source and derived note.
Revoked grants cannot be reissued. This does not erase transcripts already
shown to readers or grant the model control over revocation.

## Integration frontier and limitations

- The module is **host trusted and in-process**. `grant_latest_event`,
  `add_summary`, and `revoke` must never be exposed to untrusted model JSON or
  a public endpoint. Model adapters may receive the bounded output of
  `recall`, not a live database connection or write API.
- `NPCDuetRunner`'s future integration can call `grant_latest_event` for each
  committed `say` and pass `recall(speaker)` as clearly attributed context.
  Duet registration currently writes two `join` events in one transaction;
  this adapter does **not** claim bulk historical grant reconstruction for
  the earlier registration event.
- The separate deterministic scheduler prototype can commit **multiple**
  intents before returning. Calling `grant_latest_event` only after a whole
  tick is therefore insufficient: earlier events will be ineligible by
  design. Integration needs host-owned observation materialization inside
  each event/receipt transaction, or an explicitly reviewed one-intent commit
  step with grants issued before the next effect. That integration is **not
  implemented or qualified** by this standalone module.
- `Engine.listen`/`history` and `Gateway.feed` still expose room-event history
  using their own policies. This adapter does **not** retrofit privacy into
  those routes, and no global secrecy guarantee follows from its tests.
- Only observation eligibility for the current ledger head or its committed
  movement pair is implemented. No durable presence roster, private whisper
  ACL, event-time consent receipts, retention expiry, semantic vector search,
  derived-fact verifier, event backfill or cryptographic anti-tamper proof is
  present. An administrative writer controlling the SQLite database can edit
  the source and the matching memory hash; SQLite is not immutable authority.
- Multiple residents can have grants to the same event, scoped by their own
  canonical actor and database-bound world identity. Cross-world IDs cannot
  be substituted. Revocation is actor-and-event specific; there is no
  automatic player-wide privacy deletion workflow yet.

Run the focused model-free tests:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p test_actor_memory.py -v
```

Those tests cover latest-event grants, event-time room mismatch, late arrival,
movement pair, forged private payload, unknown actor/source, rollback,
duplicate grants, derived-note revocation, restart, source mutation and
two-world isolation. They are **unit/integration tests over local SQLite**,
not evidence of a real neural model, online presence, consensual observation,
public multiplayer privacy or broad NPC memory qualification.
