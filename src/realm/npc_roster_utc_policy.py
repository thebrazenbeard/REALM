"""Opt-in, host-signed NPC roster gathering inside one REALM UTC clock transaction.

A roster proposal is evidence, never a gameplay permit. One trusted, bounded
host HMAC signs the exact event, actor roster, sites, allocation and UTC window.
WorldClock.tick owns the SQLite writer transaction and completion receipt.
No model/network activity, player consent, daemon or canonical release is inferred.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Mapping

from .clock_gameplay import (
    _ROSTER_RECEIPT_ACTOR_PREFIX, _guard_connection, perform_in_transaction,
)
from .crafting import Crafting
from .engine import Engine
from .npc_game_theory import ResourceSite
from .npc_roster_allocation import RosterProposal, propose_roster_allocation
from .world_clock import DueEvent

_KIND = "npc_roster_gather"
_DOMAIN = b"REALM/NPC-ROSTER-HOST-PERMIT/v1\x00"
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_WINDOW = timedelta(hours=1)


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A host UTC-aware instant is required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class HostNpcRosterPolicy:
    policy_id: str
    world_id: str
    proposal: RosterProposal
    sites: tuple[ResourceSite, ...]
    site_values: dict[ResourceSite, int] | None
    event_id: str
    not_before_utc: datetime
    expires_at_utc: datetime

    def __post_init__(self):
        if type(self.policy_id) is not str or not _ID.fullmatch(self.policy_id):
            raise ValueError("A bounded host policy ID is required")
        if type(self.world_id) is not str or not self.world_id:
            raise ValueError("A host-bound world is required")
        if type(self.event_id) is not str or not _ID.fullmatch(self.event_id):
            raise ValueError("A bounded host event ID is required")
        if type(self.proposal) is not RosterProposal or (
            self.proposal.world_id != self.world_id
            or self.proposal.status != "HOST_REVIEW_REQUIRED"
            or not 2 <= len(self.proposal.assignments) <= 8
        ):
            raise ValueError("An exact held roster proposal is required")
        if type(self.sites) is not tuple or not 2 <= len(self.sites) <= 8 or (
            any(type(site) is not ResourceSite for site in self.sites)
            or len(set(self.sites)) != len(self.sites)
        ):
            raise ValueError("Bound host resource sites are required")
        if self.site_values is not None and (
            type(self.site_values) is not dict
            or set(self.site_values) != set(self.sites)
            or any(type(value) is not int or not 1 <= value <= 1000
                   for value in self.site_values.values())
        ):
            raise ValueError("Invalid host resource utility matrix")
        start, stop = _utc(self.not_before_utc), _utc(self.expires_at_utc)
        if not start < stop or stop - start > _MAX_WINDOW:
            raise ValueError("Host grant requires a positive <=1h UTC window")
        actors = [assignment.actor.casefold() for assignment in self.proposal.assignments]
        if len(set(actors)) != len(actors):
            raise ValueError("Actor roster repeats an identity")
        if any(assignment.site is not None and assignment.site not in self.sites
               for assignment in self.proposal.assignments):
            raise ValueError("Roster assignment references an unbound resource site")

    def signing_bytes(self) -> bytes:
        """Canonical host grant, not player authorization or canon rights."""
        sites = sorted(self.sites, key=lambda site: (site.room_id, site.material))
        payload = {
            "schema": "REALM_NPC_ROSTER_HOST_POLICY_V1",
            "scope": "NPC_ROSTER_GATHER_ONLY", "kind": _KIND,
            "policy_id": self.policy_id, "event_id": self.event_id,
            "world_id": self.world_id, "seed": self.proposal.seed,
            "observed_event": self.proposal.observed_through_event,
            "evidence_sha256": self.proposal.evidence_sha256,
            "expected_units": self.proposal.total_expected_units,
            "expected_utility": self.proposal.total_expected_utility,
            "assignments": [
                [a.actor, None if a.site is None else [a.site.room_id, a.site.material],
                 a.expected_units, a.expected_utility]
                for a in self.proposal.assignments
            ],
            "sites": [[site.room_id, site.material,
                       1 if self.site_values is None else self.site_values[site]]
                      for site in sites],
            "not_before_utc": _utc(self.not_before_utc).isoformat(),
            "expires_at_utc": _utc(self.expires_at_utc).isoformat(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class NpcRosterGatherResult:
    event_id: str
    proposal: RosterProposal
    executed: int
    waiting: int
    responses: tuple[str, ...]


def _roster_result_sha256(result: NpcRosterGatherResult) -> str:
    """Stable complete result binding; evidence_sha256 alone omits assignments."""
    canonical = json.dumps(asdict(result), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def perform_host_roster_gather_in_transaction(
    db: sqlite3.Connection, event: DueEvent, *,
    engine: Engine, crafting: Crafting,
    policy: HostNpcRosterPolicy, host_key: bytes,
    grant_mac_hex: str, now_utc: datetime,
) -> NpcRosterGatherResult:
    """Execute exactly one signed roster through the clock-owned writer.

    The action/clock receipts and every actor's inventory update commit or
    roll back together. The host must itself govern permit issuance; a model
    or queued event cannot produce an authorized grant.
    """
    if type(policy) is not HostNpcRosterPolicy:
        raise ValueError("Host policy missing")
    if type(host_key) is not bytes or len(host_key) < 32:
        raise ValueError("A private host permit key is required")
    if type(grant_mac_hex) is not str or re.fullmatch(r"[0-9a-f]{64}", grant_mac_hex) is None:
        raise ValueError("Malformed host grant MAC")
    expected = hmac.new(host_key, _DOMAIN + policy.signing_bytes(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, grant_mac_hex):
        raise ValueError("Host grant actor/roster/world scope mismatch")
    now = _utc(now_utc)
    if not _utc(policy.not_before_utc) <= now <= _utc(policy.expires_at_utc):
        raise ValueError("Host roster permit expired or not active")
    if type(event) is not DueEvent or event.kind != _KIND or event.processed_at is not None:
        raise ValueError("Pending roster UTC event required")
    if event.event_id != policy.event_id or _utc(event.due_at) > now or not (
        _utc(policy.not_before_utc) <= _utc(event.due_at) <= _utc(policy.expires_at_utc)
    ):
        raise ValueError("Roster permit exact UTC event mismatch")
    if type(event.payload) is not dict or event.payload != {
        "policy_id": policy.policy_id, "world_id": policy.world_id,
    }:
        raise ValueError("Untrusted queued payload does not bind host policy")
    _guard_connection(db, engine, crafting)
    if db.execute("PRAGMA query_only").fetchone()[0] != 0:
        raise ValueError("Read-only transaction cannot authorize roster effects")
    world = db.execute(
        "SELECT value FROM main.world_meta WHERE key='world_id'"
    ).fetchone()
    if world is None or world["value"] != policy.world_id:
        raise ValueError("Host permit belongs to another world")
    row = db.execute(
        "SELECT due_utc,kind,payload_json,processed_utc "
        "FROM main.clock_events WHERE event_id=?", (event.event_id,),
    ).fetchone()
    if row is None or row["processed_utc"] is not None or (
        row["due_utc"] != _utc(event.due_at).isoformat(timespec="microseconds")
        or row["kind"] != event.kind or json.loads(row["payload_json"]) != event.payload
    ):
        raise ValueError("Clock event not pending in same authoritative world")

    # A separate read-only planner snapshot is safe only because WorldClock
    # already holds BEGIN IMMEDIATE on exactly this main SQLite file.
    current = propose_roster_allocation(
        engine.db_path, expected_world_id=policy.world_id,
        actors=tuple(a.actor for a in policy.proposal.assignments),
        sites=policy.sites, observed_through_event=policy.proposal.observed_through_event,
        seed=policy.proposal.seed, site_values=policy.site_values,
    )
    if current != policy.proposal:
        raise ValueError("Roster snapshot or stock changed since host review")

    # Check every actor and receipt BEFORE the first effect. WAIT is no action.
    expected_actions = []
    for a in current.assignments:
        if a.site is None:
            if a.expected_units != 0 or a.expected_utility != 0:
                raise ValueError("WAIT proposal must have no effect")
            continue
        player = db.execute(
            "SELECT id,room_id FROM main.players WHERE id=? COLLATE NOCASE", (a.actor,)
        ).fetchone()
        if player is None or player["id"] != a.actor or player["room_id"] != a.site.room_id:
            raise ValueError("NPC not present in host-authorized resource room")
        action_id = "npcR_" + hashlib.sha256(
            f"{event.event_id}|{a.actor}".encode("utf-8")
        ).hexdigest()[:50]
        if db.execute(
            "SELECT 1 FROM main.action_receipts WHERE actor=? AND action_id=?",
            (_ROSTER_RECEIPT_ACTOR_PREFIX + a.actor, action_id),
        ).fetchone() is not None:
            raise ValueError("Roster actor receipt already exists")
        expected_actions.append((a, action_id))

    db.execute("SAVEPOINT realm_npc_roster")
    try:
        responses = []
        for a, action_id in expected_actions:
            before_node = db.execute(
                "SELECT remaining FROM main.gathering_nodes WHERE room_id=? AND material=?",
                (a.site.room_id, a.site.material),
            ).fetchone()["remaining"]
            before_pouch = db.execute(
                "SELECT quantity FROM main.material_pouches WHERE actor=? AND material=?",
                (a.actor, a.site.material),
            ).fetchone()
            prior = 0 if before_pouch is None else before_pouch["quantity"]
            response = perform_in_transaction(
                db, engine=engine, crafting=crafting, actor=a.actor,
                command="gather " + a.site.material, action_id=action_id,
                receipt_origin="roster",
            )
            after_node = db.execute(
                "SELECT remaining FROM main.gathering_nodes WHERE room_id=? AND material=?",
                (a.site.room_id, a.site.material),
            ).fetchone()["remaining"]
            after_pouch = db.execute(
                "SELECT quantity FROM main.material_pouches WHERE actor=? AND material=?",
                (a.actor, a.site.material),
            ).fetchone()["quantity"]
            if before_node - after_node != a.expected_units or (
                after_pouch - prior != a.expected_units
            ):
                raise ValueError("Actual NPC gathered quantity differs from host proposal")
            responses.append(response)
        result = NpcRosterGatherResult(
            event_id=event.event_id, proposal=current, executed=len(expected_actions),
            waiting=len(current.assignments) - len(expected_actions),
            responses=tuple(responses),
        )
        digest = _roster_result_sha256(result)
        proof = hmac.new(
            host_key, b"REALM/NPC-ROSTER-EXECUTION/v1\x00"
            + policy.signing_bytes() + digest.encode("ascii"), hashlib.sha256,
        ).hexdigest()
        db.execute(
            "INSERT INTO main.npc_roster_utc_attestations "
            "(event_id,world_id,policy_id,due_utc,outcome_sha256,"
            "grant_mac_hex,execution_mac_hex) VALUES(?,?,?,?,?,?,?)",
            (event.event_id, policy.world_id, policy.policy_id,
             _utc(event.due_at).isoformat(timespec="microseconds"),
             digest, grant_mac_hex, proof),
        )
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT realm_npc_roster")
        db.execute("RELEASE SAVEPOINT realm_npc_roster")
        raise
    db.execute("RELEASE SAVEPOINT realm_npc_roster")
    return result


def make_host_roster_gather_handler(
    *, engine: Engine, crafting: Crafting, host_key: bytes,
    grants_by_event_id: Mapping[str, tuple[HostNpcRosterPolicy, str]],
    now: Callable[[], datetime],
) -> Callable[[sqlite3.Connection, DueEvent], NpcRosterGatherResult]:
    """Host-controlled exact grant registry for opt-in WorldClock.tick use."""
    if not callable(now) or not isinstance(grants_by_event_id, Mapping):
        raise ValueError("Host permit registry and time provider are required")
    registry = dict(grants_by_event_id)
    if any(type(event_id) is not str or type(grant) is not tuple
           or len(grant) != 2 or type(grant[0]) is not HostNpcRosterPolicy
           or grant[0].event_id != event_id for event_id, grant in registry.items()):
        raise ValueError("Host permit registry has invalid event binding")

    def handler(db: sqlite3.Connection, event: DueEvent) -> NpcRosterGatherResult:
        if event.event_id not in registry:
            raise ValueError("HELD: no host roster grant for queued event")
        policy, mac = registry[event.event_id]
        return perform_host_roster_gather_in_transaction(
            db, event, engine=engine, crafting=crafting, policy=policy,
            host_key=host_key, grant_mac_hex=mac, now_utc=now(),
        )

    return handler
