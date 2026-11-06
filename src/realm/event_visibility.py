"""Pure, world-bound event visibility policy PROPOSAL; not wired to Gateway.

REALM's current events table does not persist event-time audience/consent
grants. Never infer them from current room, `history`, or model narration.
This module operates only on explicit host-supplied snapshots. It performs
no I/O and does NOT authenticate player/administrator identities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re


_WORLD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class Visibility(str, Enum):
    PUBLIC_CHRONICLE = "public_chronicle"
    WITNESSED_ROOM = "witnessed_room"
    PRIVATE_MESSAGE = "private_message"
    MODERATION_LEDGER = "moderation_ledger"


class ReadPurpose(str, Enum):
    FEED = "feed"
    HISTORY = "history"
    NPC_MEMORY = "npc_memory"
    MODERATION = "moderation"


class VisibilityPolicyError(ValueError):
    """An author-proposed event audience cannot be accepted safely."""


@dataclass(frozen=True)
class WorldVisibilityPolicy:
    world_id: str
    revision: str = "realm.visibility.v1"
    allow_public_chronicle: bool = False
    allow_npc_memory: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.world_id, str) or not _WORLD_ID.fullmatch(self.world_id):
            raise VisibilityPolicyError("World visibility policy needs an exact world ID")
        if not isinstance(self.revision, str) or not self.revision:
            raise VisibilityPolicyError("Policy revision required")
        if (type(self.allow_public_chronicle) is not bool
                or type(self.allow_npc_memory) is not bool):
            raise VisibilityPolicyError("Visibility switches must be actual booleans")


@dataclass(frozen=True)
class EventAudience:
    """Host-recorded event-time audience, not a retrospective room query."""

    world_id: str
    event_id: int
    kind: str
    actor: str
    visibility: Visibility
    policy_revision: str
    room_id: str | None = None
    witnessed_ids: frozenset[str] = field(default_factory=frozenset)
    recipient_ids: frozenset[str] = field(default_factory=frozenset)
    explicitly_public: bool = False
    memory_consent: bool = False
    revoked_ids: frozenset[str] = field(default_factory=frozenset)
    revoked_all: bool = False


@dataclass(frozen=True)
class ViewerGrant:
    """Snapshot supplied by verified host account/policy code, NOT client JSON."""

    world_id: str
    principal: str
    purpose: ReadPurpose
    world_member: bool
    capabilities: frozenset[str] = field(default_factory=frozenset)


def classify_event(
    policy: WorldVisibilityPolicy, *, event_id: int, kind: str, actor: str,
    room_id: str | None = None, witnessed_ids: frozenset[str] = frozenset(),
    recipient_ids: frozenset[str] = frozenset(),
    visibility: Visibility | None = None, explicit_public_consent: bool = False,
    memory_consent: bool = False,
) -> EventAudience:
    """Validate proposed audience before a host transaction records an event.

    The caller must obtain event-time witnesses and recipients from trusted
    host state *in the same future transaction* as the underlying event.
    Current Engine has no such atomic API; this is a model-free policy fixture.
    """
    if (type(event_id) is not int or event_id <= 0
            or not isinstance(kind, str) or not _KIND.fullmatch(kind)
            or not isinstance(actor, str) or not actor
            or (room_id is not None and (not isinstance(room_id, str) or not room_id))
            or type(explicit_public_consent) is not bool
            or type(memory_consent) is not bool
            or not isinstance(witnessed_ids, frozenset)
            or not isinstance(recipient_ids, frozenset)
            or any(not isinstance(x, str) or not x for x in witnessed_ids | recipient_ids)):
        raise VisibilityPolicyError("Invalid host event-audience metadata")
    if visibility is None:
        if kind in {"private_message", "whisper", "tell"}:
            visibility = Visibility.PRIVATE_MESSAGE
        elif kind in {"moderation", "moderation_note", "ban_audit"}:
            visibility = Visibility.MODERATION_LEDGER
        else:
            visibility = Visibility.WITNESSED_ROOM
    if not isinstance(visibility, Visibility):
        raise VisibilityPolicyError("Unknown audience class fails closed")
    if kind in {"moderation", "moderation_note", "ban_audit"} and visibility is not Visibility.MODERATION_LEDGER:
        raise VisibilityPolicyError("Moderation records cannot be reclassified as public gameplay")
    if kind in {"private_message", "whisper", "tell"} and visibility is not Visibility.PRIVATE_MESSAGE:
        raise VisibilityPolicyError("Private messages cannot be reclassified as public gameplay")
    if visibility is Visibility.PUBLIC_CHRONICLE:
        if not policy.allow_public_chronicle or explicit_public_consent is not True or recipient_ids:
            raise VisibilityPolicyError("Public chronicle requires explicit per-world and event consent")
    elif explicit_public_consent:
        raise VisibilityPolicyError("Public consent flag without a public event class")
    if visibility is Visibility.WITNESSED_ROOM:
        if room_id is None or not witnessed_ids:
            raise VisibilityPolicyError("Witnessed event needs event-time room and witnesses")
    if visibility is Visibility.PRIVATE_MESSAGE and not recipient_ids:
        raise VisibilityPolicyError("Private event needs explicit recipients")
    if visibility is Visibility.MODERATION_LEDGER and memory_consent:
        raise VisibilityPolicyError("Moderation ledger is not NPC-memory material")
    return EventAudience(
        world_id=policy.world_id, event_id=event_id, kind=kind,
        actor=actor, visibility=visibility, policy_revision=policy.revision,
        room_id=room_id, witnessed_ids=witnessed_ids,
        recipient_ids=recipient_ids, explicitly_public=explicit_public_consent,
        memory_consent=memory_consent,
    )


def can_read_event(
    policy: WorldVisibilityPolicy, record: EventAudience | None,
    viewer: ViewerGrant,
) -> bool:
    """Read-only decision; no existing Gateway endpoint invokes this yet."""
    if (not isinstance(record, EventAudience)
            or not isinstance(viewer, ViewerGrant)
            or record.world_id != policy.world_id
            or viewer.world_id != policy.world_id
            or record.policy_revision != policy.revision
            or type(record.event_id) is not int or record.event_id <= 0
            or not isinstance(record.actor, str) or not record.actor
            or not isinstance(viewer.principal, str) or not viewer.principal
            or viewer.world_member is not True
            or not isinstance(viewer.purpose, ReadPurpose)
            or not isinstance(record.visibility, Visibility)
            or not isinstance(record.witnessed_ids, frozenset)
            or not isinstance(record.recipient_ids, frozenset)
            or any(not isinstance(x, str) or not x
                   for x in record.witnessed_ids | record.recipient_ids)
            or (record.kind in {"moderation", "moderation_note", "ban_audit"}
                and record.visibility is not Visibility.MODERATION_LEDGER)
            or (record.kind in {"private_message", "whisper", "tell"}
                and record.visibility is not Visibility.PRIVATE_MESSAGE)
            or record.revoked_all is not False
            or not isinstance(record.revoked_ids, frozenset)
            or not isinstance(viewer.capabilities, frozenset)):
        return False
    viewer_id = viewer.principal.casefold()
    if viewer_id in {x.casefold() for x in record.revoked_ids if isinstance(x, str)}:
        return False

    if viewer.purpose is ReadPurpose.MODERATION:
        return (record.visibility is Visibility.MODERATION_LEDGER
                and "moderation_read" in viewer.capabilities)
    if record.visibility is Visibility.MODERATION_LEDGER:
        return False

    if viewer.purpose is ReadPurpose.NPC_MEMORY:
        if not policy.allow_npc_memory or record.memory_consent is not True:
            return False
    elif viewer.purpose not in {ReadPurpose.FEED, ReadPurpose.HISTORY}:
        return False

    if record.visibility is Visibility.PUBLIC_CHRONICLE:
        return (policy.allow_public_chronicle
                and record.explicitly_public is True)
    if record.visibility is Visibility.WITNESSED_ROOM:
        # Current position is intentionally absent: event-time grant only.
        return viewer_id in {
            x.casefold() for x in record.witnessed_ids if isinstance(x, str)
        }
    if record.visibility is Visibility.PRIVATE_MESSAGE:
        return viewer_id == record.actor.casefold() or viewer_id in {
            x.casefold() for x in record.recipient_ids if isinstance(x, str)
        }
    return False


def revoke_event_access(record: EventAudience, *, principal: str | None = None) -> EventAudience:
    """Produce a candidate revocation projection; caller must persist it.

    A None principal means a world/operator-approved full content redaction.
    A per-principal revocation does not delete the underlying event ledger.
    """
    from dataclasses import replace

    if principal is None:
        return replace(record, revoked_all=True)
    if not isinstance(principal, str) or not principal:
        raise VisibilityPolicyError("Revocation principal required")
    return replace(record, revoked_ids=record.revoked_ids | {principal.casefold()})
