# Event visibility is a world rule, not a room-history guess

**Status:** implemented **pure standalone policy proposal** and source tests.
NOT wired into the current `Engine` event writer, `Gateway.feed`, `Gateway.room`,
CLI `history`/`listen`, Telnet, Android client or NPC memory store. These
endpoints are **not privacy-qualified** because this document exists.

REALM's promise is an evolving text world with shared history. History and
privacy are not mutually exclusive. A creator may explicitly choose a public
chronicle whose past entries latecomers can read; a private conversation or
moment witnessed in a room may instead require event-time attendance. The
engine needs a **typed, per-world, per-event audience rule**, not the blanket
assumption that every earlier `say` is either forever public or forever
private.

## Four distinct visibility classes

| Class | Authorized audience | Default and constraints |
| --- | --- | --- |
| `PUBLIC_CHRONICLE` | Authenticated member of the same world, including a later joiner | **Opt-in twice:** world policy allows public chronicles, and the specific event is explicitly classified public under a disclosed/consented rule. Cannot retroactively promote unseen private speech by changing the world's flag. |
| `WITNESSED_ROOM` | Exact principals with host-recorded visibility grants **when the event occurred**, even if they later move away | Safe default for ordinary `say`, movement and other room actions. A late arrival gets no automatic past access from their current room. |
| `PRIVATE_MESSAGE` | Sender and expressly named recipients (subject to revocation) | Default for `tell`/`whisper`/`private_message`. Bystanders and moderators do not get automatic content access. |
| `MODERATION_LEDGER` | Verified world moderator with a dedicated moderation-read capability and moderation purpose | Default for moderation/audit kinds; excluded from player feed, public chronicle and NPC memory. Administrative retention and disclosure need independent owner governance. |

`WorldVisibilityPolicy` defaults `allow_public_chronicle=False` and
`allow_npc_memory=False`. Unknown record classes and legacy events without
typed event-time grants fail closed. The policy is separate per world ID; same
numeric event IDs in two worlds never imply shared audience.

The creator may later declare that speech in a named public square is a
published chronicle—provided the rule is explicit to players/AI participants,
host-enforced *at event commit*, compatible with consent/rights, and recorded
in each event's provenance. The current v1 worldpack does not carry this
policy. An AI model, NPC dialogue or an arbitrary client field cannot mark
someone else's private event as public.

## Exact proposed authority path

```text
operator/creator sets world visibility policy + disclosure/consent rule
                           |
current world facts + verified actor + event-time room audience
                           |
          Engine transaction records event AND audience grants
                           |
           world-specific verified viewer + read purpose
                           |
                   audience/revocation check
                           |
             eligible feed/history/memory projection
```

The source module `src/realm/event_visibility.py` presently implements only
`classify_event`, `can_read_event` and `revoke_event_access` over caller-supplied
immutable records. A `ViewerGrant` can be constructed by any Python caller:
**its `world_member` flag and `moderation_read` capability are not
authentication**. Only a future host-verified session/role service may supply
those values. Likewise `witnessed_ids` must be captured from authoritative
room presence and visibility mechanics in the *same database transaction* as
the event, never from current `players.room_id` or a later `history` query.

The following fields are design candidates, not an implemented migration:
`world_id`, `event_id`, `visibility_class`, `policy_revision`, `room_id`,
`witnessed_principals`, `direct_recipients`, explicit public consent, NPC
memory consent, source-event provenance and audience revocations. Exact
constraints, indexes, version changes and rollback require independent schema
review. Existing `worldpack` import strictly fingerprints pristine schemas;
do not add tables behind its back or silently adopt legacy databases.

Viewer rights also depend on **purpose**:

- `FEED`/`HISTORY`: only events whose typed audience admits that viewer.
- `NPC_MEMORY`: requires the *same* audience entitlement **plus** explicit
  world-level and event-level memory permission. Memory summaries remain
  derived; speech about an occurrence is not proof the occurrence happened.
- `MODERATION`: requires an independently verified moderation role/capability;
  it does not make a moderator a recipient of private messages by default.

`revoke_event_access` currently returns a revised *proposal object*; it does
not mutate a database or erase a canonical event. Future enforcement must
bind revocation state to reads and memory retrieval, record audit provenance,
respect scoped retention requirements, and acknowledge that already delivered
public text cannot be un-read from another client's cache.

## Existing blocker, illustrated precisely

Current `Gateway.feed(player, after)` filters events by the player's **current
room** and cursor. `Engine.history`/`listen` also reads prior room events.
Consequently, an authenticated late arrival can currently retrieve speech
recorded before arrival. That is a release blocker **for a world/event whose
policy says `WITNESSED_ROOM` or private**; it may be intended behavior for a
clearly designated `PUBLIC_CHRONICLE`.

Draft PR #34 contains intentionally failing gateway acceptance tests for the
private/witnessed default. Those tests should keep their **explicit private
fixture policy** when the future gateway is wired; they must not become a
universal ban on reading authored public chronicles. Draft PR #39's signed
local adapter disables feed/history/map rather than claiming to resolve this
missing policy; it also strips the world-global event cursor to avoid coarse
off-room activity disclosure. Neither is a finished event-audience API.

## Offline tests and future real gates

```sh
PYTHONPATH=src python -m unittest tests.test_event_visibility -v
```

These fixtures prove only the pure predicate: default witnessed-room speech
does not leak to a late reader; a consented public chronicle **does** admit a
later world member; private messages reach sender/recipients but not bystander;
moderation needs explicit capability/purpose; NPC memory needs independent
consent; per-principal and global revocation deny future reads; wrong-world,
legacy, unknown class and incompatible policy revision fail closed.

Before *any* remote hosting or true neural NPC memory, execute and receipt:

1. **Atomic recording:** room audience and event row committed together;
   fault injection/restart/replay cannot leave an event without typed grants.
2. **Real actor security:** server, not client/model, provides player/world,
   membership, event purpose, moderator role and consent evidence.
3. **Mixed public/private stream:** same world has public chronicle, witnessed
   speech, private tell and moderation event; two independent clients receive
   exactly their entitled entries/cursors with no gap/cross-audience leakage.
4. **Late arrival:** late player reads *public* past chronicle but not prior
   witnessed-only or private messages—even via CLI, HTTP, Android, Telnet,
   `/feed`, history or any NPC memory index.
5. **Policy changes:** opt-in/out, revocation, rights deletion, migrations and
   server restart preserve the correct old event class and audit state.
6. **Side channels:** world-global event ID/cursor, response size/timing,
   map markers and presence must not expose forbidden room/private events.
7. **Owner boundary:** two local realms, same names/event IDs and different
   authors, cannot import/export each other's grants; worldpacks cannot grant
   themselves moderation or rewrite historical privacy.

All of these are proposed host/device tests, **not executed by this module**.
Real remote access, paid compute, public exposure and implicit personal-data
publication remain out of scope.
