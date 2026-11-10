# REALM roadmap — living worlds without model-owned authority

REALM stands for **Roleplaying Engine for Autonomous Living MUDs**. Its target
is a text-only, self-hostable multiplayer world engine where people and
authorized neural characters co-create persistent history. The engine owns
world truth; worldpacks supply optional fiction; models propose bounded actions.
The client (including an eventual Android APK) observes and submits authorized
commands. None of those layers depends on a particular fantasy setting.

| Milestone | Current evidence/status | Qualification still required |
| --- | --- | --- |
| Persisted multiplayer text world | **Implemented:** SQLite rooms, items, players, movement, speech, event feed, transaction/replay tests | Reliable live online presence, general account sessions |
| Creator-owned worldpacks | **Implemented:** guarded v1 import and offline room/exit/item builder; isolated DB per world | Live OLC drafts, ACL, quotas, migration and rollback |
| Original showcase | **Implemented as passive content:** Siltglass Reach worldpack; test import and two Engine clients | NPCs, quests, factions, weather, PK, scripted consequences |
| Invited remote players | **Not qualified:** HTTP/Telnet bind loopback; per-player HTTP credentials, opt-in player-bound Telnet credentials, local per-player HTTP quota, bounded socket workers, absolute request-header deadline and browser overload recovery exist | TLS/private transport, unauthenticated flood controls, cross-process quotas, remote E2E |
| Host-authorized NPC principals | **Experimental code:** bounded host-invoked local HTTP model proposal bridge, controller validation and replay receipts; an opt-in same-world, room-entry-scoped scene path rechecks authorization inside the action transaction; fixture verified, no model weights installed | Replace legacy arbitrary-view callers with the scoped path, scheduler, drives/memory, real model-runtime E2E |
| Ship crew title display | **Experimental host-only code:** opt-in first-officer appointment/revocation ledger, owner-authenticated writes and stale-grant invalidation; captain titles derive from registered ship owner | Explicit host adoption, authenticated client display integration, operational tests across ownership transitions |
| Dynamic drives and goals | **Planned:** persistent, versioned state per NPC and deterministic drive updates | Tests for replay/restart, bounded priorities, conflict handling |
| Deep memory | **Planned:** scoped episodic facts anchored to source events; semantic summaries as derived hints | Retention, trust/provenance, retrieval isolation, hallucination rejection |
| Real-time evolving world | **Experimental:** explicit host-owned world-clock and durable simulation transitions, tested locally | Production cadence, backfill/cursor safety, pressure limits, event delivery |
| Android client | **Separate idea lane:** [issue #18](https://github.com/thebrazenbeard/REALM/issues/18) | Native client, remote protocol, Patrick's physical phone tests |

## Agent action contract

1. A world host provisions an NPC principal and explicit action permissions.
2. Server retrieves the NPC's authorized room view, drives and memory citations.
3. A configurable local model produces a **proposal**, never a direct database
   mutation. A deterministic model-free fixture can exercise the same path.
4. The host validates actor, target, room, world, quotas, replay ID and rules.
5. Only an authorized action commits; a durable receipt/event becomes evidence
   for player feeds and subsequent NPC memories.
6. Memory summaries may cite recorded events but cannot rewrite, erase, or
   independently certify those events.

This contract is a roadmap for additional source and runtime work, not proof
that a neural provider is running today. An NPC stating that it remembers or
performed something is not evidence that the host saved or executed it.

## World/content boundary

- `src/realm/`: generic engine, schema, validation, credentials and adapters.
- `worlds/flagship/siltglass-reach/`: optional original story data; no code
  imports from the engine or automatic injection into other people's worlds.
- User worldpacks: loaded only by an explicit host import into a fresh database.
- `clients/android/` (future): presentation and transport, never authoritative
  gameplay state or copied fictional canon.
- Local GGUF weights (optional): host-managed external files, never bundled in
  this source repository or required to boot a deterministic world.

## Falsifiers and acceptance gates

- Removing `worlds/flagship/` must not stop the generic engine booting.
- Two separately imported databases must not share player state, receipts,
  inventory, memory retrieval or credentials.
- Wrong-player and wrong-world actions must be rejected before commit.
- Retry of one action ID must not duplicate its committed effects.
- An NPC proposal must not bypass game rules, even when its model output is
  malformed, persuasive, or contains prompt-injection instructions.
- World ticks and NPC drives must survive restart and not double-apply.
- Derived memory must cite source events and never become an authority to
  mutate the ledger.
- Two real clients must witness the same authorized NPC event; server state,
  not the NPC's narration or the clients' caches, decides what occurred.

Public Internet hosting, payment services, paid inference and app-store
publication are not enabled or authorized by this roadmap.
