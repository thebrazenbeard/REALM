"""Host-only deterministic NPC gathering from REALM UTC ticks.

An event payload is NOT authorization. A trusted host provides an immutable
policy outside the event and registers this function for an exact event kind.
Constructing the policy dataclass does not prove player consent/permission.
Effects use the caller-owned SQLite WorldClock.tick transaction.
No network, model inference, daemon or private-setting mechanics are used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Mapping

from .clock_gameplay import perform_in_transaction
from .crafting import Crafting
from .engine import Engine
from .npc_game_theory import (
    ResourceChoice, ResourceSite, choose_economic_site,
    verify_economic_choice_in_transaction,
)
from .world_clock import DueEvent

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_KIND = "npc_economic_gather"
_DOMAIN = b"REALM/NPC-HOST-PERMIT/v1\x00"
_MAX_GRANT_WINDOW = timedelta(hours=1)


def _utc(value: datetime, label: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware UTC instant")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class HostNpcGatherPolicy:
    policy_id: str
    world_id: str
    actor: str
    opponent: str
    sites: tuple[ResourceSite, ...]
    seed: int
    event_id: str
    not_before_utc: datetime
    expires_at_utc: datetime

    def __post_init__(self):
        if (type(self.policy_id) is not str or not _ID.fullmatch(self.policy_id)
                or type(self.world_id) is not str or not self.world_id):
            raise ValueError("A host-bound policy ID and world ID are required")
        if (type(self.actor) is not str or not self.actor
                or type(self.opponent) is not str or not self.opponent
                or self.actor.casefold() == self.opponent.casefold()):
            raise ValueError("Host actor and opponent must be distinct")
        if (type(self.sites) is not tuple or not 2 <= len(self.sites) <= 8
                or any(type(s) is not ResourceSite for s in self.sites)
                or len(set(self.sites)) != len(self.sites)):
            raise ValueError("Host policy requires two to eight distinct resource sites")
        if type(self.seed) is not int or self.seed.bit_length() > 64:
            raise ValueError("Host policy seed must be a bounded integer")
        if type(self.event_id) is not str or not _ID.fullmatch(self.event_id):
            raise ValueError("A bounded exact host event ID is required")
        start = _utc(self.not_before_utc, "Permit start")
        stop = _utc(self.expires_at_utc, "Permit expiry")
        if not start < stop or stop - start > _MAX_GRANT_WINDOW:
            raise ValueError("Host grant UTC window must be positive and at most one hour")
        for site in self.sites:
            if not site.room_id or not site.material:
                raise ValueError("Host grant sites require room and material")

    def signing_bytes(self) -> bytes:
        """Canonical scope for a *synthetic* host policy, not player consent."""
        data = {
            "schema": "REALM_NPC_GATHER_POLICY_V1", "kind": _KIND,
            "scope": "NPC_GATHER_ONLY", "event_id": self.event_id,
            "policy_id": self.policy_id, "world_id": self.world_id,
            "actor": self.actor, "opponent": self.opponent,
            "sites": [[site.room_id, site.material] for site in self.sites],
            "seed": self.seed,
            "not_before_utc": _utc(self.not_before_utc, "Permit start").isoformat(),
            "expires_at_utc": _utc(self.expires_at_utc, "Permit expiry").isoformat(),
        }
        return json.dumps(data, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class NpcGatherResult:
    event_id: str
    choice: ResourceChoice
    response: str


def perform_host_npc_gather_in_transaction(
    db: sqlite3.Connection,
    event: DueEvent,
    *,
    engine: Engine,
    crafting: Crafting,
    policy: HostNpcGatherPolicy,
    host_key: bytes,
    grant_mac_hex: str,
    now_utc: datetime,
) -> NpcGatherResult:
    """Select, revalidate and gather once through a trusted host policy.

    The event contains an opaque pre-registered policy ID and world ID,
    never actor, command, sites or seed. Queue contents grant no authority.
    The caller owns clock receipt, transaction and commit.
    """
    if type(policy) is not HostNpcGatherPolicy:
        raise ValueError("A host-provisioned NPC gather policy is required")
    if type(host_key) is not bytes or len(host_key) < 32:
        raise ValueError("Host NPC grant requires a non-queued host key")
    if type(grant_mac_hex) is not str or not re.fullmatch(r"[0-9a-f]{64}", grant_mac_hex):
        raise ValueError("Host NPC grant signature missing or malformed")
    expected_mac = hmac.new(host_key, _DOMAIN + policy.signing_bytes(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_mac, grant_mac_hex):
        raise ValueError("Host NPC grant does not authorize proposed actor/world/action")
    now = _utc(now_utc, "Host tick time")
    if not _utc(policy.not_before_utc, "Permit start") <= now <= _utc(policy.expires_at_utc, "Permit expiry"):
        raise ValueError("Host NPC grant is outside its bounded UTC window")
    if type(event) is not DueEvent or event.kind != _KIND or event.processed_at is not None:
        raise ValueError("A pending host NPC economic UTC event is required")
    if policy.event_id != event.event_id or not (
        _utc(policy.not_before_utc, "Permit start") <= _utc(event.due_at, "Event due time")
        <= _utc(policy.expires_at_utc, "Permit expiry")
    ) or _utc(event.due_at, "Event due time") > now:
        raise ValueError("Host NPC grant exact event ID/due time mismatch")
    if type(event.payload) is not dict or event.payload != {
        "policy_id": policy.policy_id, "world_id": policy.world_id,
    }:
        raise ValueError("Queued payload is not bound to this host policy")
    if type(event.event_id) is not str or not _ID.fullmatch(event.event_id):
        raise ValueError("A bounded host-issued event ID is required")
    if (type(db) is not sqlite3.Connection or not db.in_transaction
            or db.row_factory is not sqlite3.Row):
        raise ValueError("NPC action requires the host's SQLite transaction")
    if not isinstance(crafting, Crafting) or not isinstance(engine, Engine):
        raise ValueError("Initialized same-world game services are required")
    if crafting.db_path.resolve() != engine.db_path.resolve():
        raise ValueError("NPC policy services reference different world databases")
    files = db.execute("PRAGMA database_list").fetchall()
    main = next((row[2] for row in files if row[1] == "main"), None)
    if not main or Path(main).resolve() != engine.db_path.resolve():
        raise ValueError("NPC event connection belongs to another world")
    if db.execute(
        "SELECT 1 FROM main.action_receipts WHERE action_id=?",
        (event.event_id,),
    ).fetchone():
        raise ValueError("Host NPC grant event ID has already executed")
    try:
        queued = db.execute(
            "SELECT due_utc,kind,payload_json,processed_utc "
            "FROM main.clock_events WHERE event_id=?", (event.event_id,),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError("Host NPC event is not registered in an enabled clock") from exc
    if (queued is None or queued["processed_utc"] is not None
            or queued["kind"] != event.kind
            or queued["due_utc"] != _utc(event.due_at, "Event due time").isoformat(timespec="microseconds")
            or json.loads(queued["payload_json"]) != event.payload):
        raise ValueError("Host NPC event must be registered and pending in this world")
    # The verifier independently upgrades a deferred transaction to a writer
    # lease and refuses query_only. Clock ticks already hold BEGIN IMMEDIATE.
    latest = db.execute("SELECT COALESCE(MAX(id),0) FROM main.events").fetchone()[0]
    choice = choose_economic_site(
        engine.db_path, expected_world_id=policy.world_id,
        actor=policy.actor, opponent=policy.opponent, sites=policy.sites,
        observed_through_event=latest, seed=policy.seed, _connection=db,
    )
    verified = verify_economic_choice_in_transaction(
        db, choice=choice, sites=policy.sites, seed=policy.seed,
    )
    actor = db.execute(
        "SELECT id,room_id FROM main.players WHERE id=? COLLATE NOCASE",
        (policy.actor,),
    ).fetchone()
    if actor is None or actor["room_id"] != verified.site.room_id:
        raise ValueError("NPC must already be in the authorized resource room")
    response = perform_in_transaction(
        db, engine=engine, crafting=crafting, actor=actor["id"],
        command="gather " + verified.site.material, action_id=event.event_id,
    )
    return NpcGatherResult(event.event_id, verified, response)


def make_host_npc_gather_handler(
    *, engine: Engine, crafting: Crafting,
    host_key: bytes,
    grants_by_event_id: Mapping[str, tuple[HostNpcGatherPolicy, str]],
    now: Callable[[], datetime],
) -> Callable[[sqlite3.Connection, DueEvent], NpcGatherResult]:
    """Build one opt-in foreground handler from a host-controlled registry.

    Neither event payloads nor untrusted model proposals can register actors,
    providers, permit keys, permissions, or commands. Unknown events fail closed.
    """
    if not callable(now) or not isinstance(grants_by_event_id, Mapping):
        raise ValueError("An explicit bounded host permit registry and UTC clock are required")
    registry = dict(grants_by_event_id)
    if any(type(key) is not str or type(value) is not tuple or len(value) != 2
           or type(value[0]) is not HostNpcGatherPolicy or key != value[0].event_id
           for key, value in registry.items()):
        raise ValueError("Host permit registry has an invalid exact event binding")

    def handler(db: sqlite3.Connection, event: DueEvent) -> NpcGatherResult:
        grant = registry.get(event.event_id)
        if grant is None:
            raise ValueError("HELD: host NPC permit is absent for this event ID")
        policy, mac = grant
        return perform_host_npc_gather_in_transaction(
            db, event, engine=engine, crafting=crafting, policy=policy,
            host_key=host_key, grant_mac_hex=mac, now_utc=now(),
        )

    return handler
