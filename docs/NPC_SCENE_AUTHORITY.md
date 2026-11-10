# Host-scoped NPC scene authority

REALM can now build a local NPC model's scene from the same authoritative
`Engine` world used to commit the NPC's action. This is an **opt-in host path**,
not an automatically running NPC scheduler or a claim that model weights have
been installed or tested.

## Authorized path

The trusted host constructs `ResidentController(engine, actor, allowed_verbs=...)`
for an already joined resident, then submits only a controller and receipt ID:

```python
host.run_scoped_tick([ScopedResidentTurn(controller, "resident-action-001")])
```

`ResidentSceneBuilder` reads one SQLite snapshot and supplies bounded JSON to
the model: canonical resident name, current room name/description, exit
directions, room items, and at most eight (configurable 1–20) recent `say`
events in that room **at or after the resident's current proven arrival**.
There is no caller-supplied `host_view` in this API. No global chat history,
other rooms, player inventories, database paths, or credentials are included.
Speech payloads are checked and the final scene has a 6,000-character limit.

The arrival floor comes from the most recent `join|leave|move` event for that
resident, as enforced by `Engine._room_entry_floor`. A newer departure, room
change, missing arrival record, or departure followed by reentry prevents
fallback to an older stay's messages. Any resulting change to room or arrival
after scene construction is checked once before inference and **again inside
`Engine.act`'s `BEGIN IMMEDIATE` writer transaction**, through the resident
controller's `writer_guard`. A stale scene fails closed, including on receipt
replay. Commands are still validated and committed by `ResidentController`;
the model never receives raw write authority.

## Compatibility versus privacy qualification

`ResidentTurnHost.run_tick([ResidentTurn(controller, host_view, action_id)])`
is retained for existing callers. **That legacy entry point accepts arbitrary
host-provided text and is not qualified by the new scene-privacy guarantee.**
Applications intending to enforce room-entry boundaries must use
`run_scoped_tick` and must keep the controller, Engine, action-ID issuer, and
model endpoint under their own authorization policy. A Python caller with
unrestricted access to the DB or private host objects remains trusted code,
not a sandboxed principal.

## Limits and falsifiers

- Events arriving **after the read snapshot** are absent from that model turn.
  Room movement/reentry is guarded at commit, but an intervening new line of
  dialogue does not invalidate the scene or roll it forward.
- Room descriptions and permitted speech are untrusted natural-language data:
  they can contain hostile instructions. The model's response remains subject
  to the controller's command grammar, verb set, and Engine authorization.
- This boundary covers the model's constructed scene and committed command,
  not provider-side model retention, wire privacy to remote endpoints, or host
  process compromise. Local loopback model endpoints should remain private.
- No inference weights or hardware deployment are involved; unit tests use a
  deterministic local HTTP fixture and real ephemeral SQLite world databases.

Run the regressions with `PYTHONPATH=src python -m unittest discover -s tests
-p 'test_npc*.py' -q`, and repeat with `python -O` to catch assert-dependent
security checks.
