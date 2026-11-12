"""Read-only, host-bound game-theoretic resource choice proposal.

No actor authentication, model execution, scheduling, command commit, or
world mutation occurs here. The host is responsible for observing a completed
event watermark before offering the next simultaneous strategic round.
"""
from __future__ import annotations

from contextlib import closing, nullcontext
from dataclasses import dataclass, replace
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import random
import sqlite3


@dataclass(frozen=True)
class ResourceSite:
    room_id: str
    material: str


@dataclass(frozen=True)
class ResourceChoice:
    world_id: str
    actor: str
    opponent: str
    site: ResourceSite
    available_units: int
    forecast_numerator: int
    forecast_denominator: int
    observations: int
    observed_through_event: int
    evidence_sha256: str
    status: str = "HOST_REVIEW_REQUIRED"


def choose_economic_site(
    db_path: str | Path, *, expected_world_id: str, actor: str, opponent: str,
    sites: tuple[ResourceSite, ...], observed_through_event: int, seed: int,
    _connection: sqlite3.Connection | None = None,
) -> ResourceChoice:
    """Choose an anti-coordination resource site from a real world snapshot.

    Forecast = u_first - (p_same/2) * (u_first - u_second), where
    u_first=min(remaining,batch), u_second=min(max(remaining-batch,0),batch).
    p_same is Laplace-smoothed opponent *past successful gather* frequency
    (not proof of opponent intent or a current-round secret). The factor 1/2
    assumes fair random ordering on collisions; REALM does not enforce it.
    Complementary tie choices are limited to exact *two-actor* symmetric
    scores, not an N-player equilibrium, coalition policy, or guarantee of
    collision-freedom after dynamic world changes.
    This function never executes the proposal or creates gameplay authority.
    """
    if type(expected_world_id) is not str or not expected_world_id:
        raise ValueError("A host-bound world_id is required")
    if type(actor) is not str or not actor or type(opponent) is not str or not opponent:
        raise ValueError("Both host-provisioned actor IDs are required")
    if actor.casefold() == opponent.casefold():
        raise ValueError("Actor and opponent must be distinct")
    if (type(sites) is not tuple or not 2 <= len(sites) <= 8
            or any(type(s) is not ResourceSite or not isinstance(s.room_id, str)
                   or not isinstance(s.material, str) or not s.room_id or not s.material
                   for s in sites) or len(set(sites)) != len(sites)):
        raise ValueError("Two to eight unique world-author resource sites required")
    if type(observed_through_event) is not int or not 0 <= observed_through_event <= 2**63 - 1:
        raise ValueError("Event watermark must be a nonnegative SQLite integer")
    if type(seed) is not int or seed.bit_length() > 64:
        raise ValueError("Host seed must be a bounded integer")
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise ValueError("Bound world database is missing")
    if _connection is None:
        context = closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True))
    else:
        if not isinstance(_connection, sqlite3.Connection) or not _connection.in_transaction:
            raise ValueError("Host must provide an open, owned SQLite transaction")
        if _connection.row_factory is not sqlite3.Row:
            raise ValueError("Host transaction must use sqlite3.Row")
        files = _connection.execute("PRAGMA database_list").fetchall()
        main = next((row[2] for row in files if row[1] == "main"), None)
        if not main or Path(main).resolve() != db_path:
            raise ValueError("Host transaction is not bound to the offered world DB")
        context = nullcontext(_connection)
    with context as db:
        if _connection is None:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
        binding = db.execute("SELECT value FROM main.world_meta WHERE key='world_id'").fetchone()
        if binding is None or binding["value"] != expected_world_id:
            raise ValueError("Unbound or different world database")
        identities = []
        for name in (actor, opponent):
            row = db.execute("SELECT id FROM main.players WHERE id=? COLLATE NOCASE", (name,)).fetchone()
            if row is None:
                raise ValueError("Host must pre-provision both actors")
            identities.append(row["id"])
        actor_id, rival_id = identities
        latest = db.execute("SELECT COALESCE(MAX(id),0) FROM main.events").fetchone()[0]
        if observed_through_event > latest:
            raise ValueError("Event watermark is ahead of the durable event ledger")
        capacity = []
        after_rival_capacity = []
        resources = []
        for site in sites:
            row = db.execute(
                "SELECT remaining,batch_yield FROM main.gathering_nodes WHERE room_id=? AND material=?",
                (site.room_id, site.material),
            ).fetchone()
            if row is None:
                raise ValueError("Offered resource site is not registered in this world")
            available = min(row["remaining"], row["batch_yield"])
            if available < 0:
                raise ValueError("Negative world resource capacity")
            capacity.append(available)
            resources.append((site.room_id, site.material, row["remaining"], row["batch_yield"]))
            after_rival_capacity.append(min(max(0, row["remaining"] - row["batch_yield"]),
                                            row["batch_yield"]))
        if not any(capacity):
            raise ValueError("No harvestable resources in proposed sites")
        counts = [0] * len(sites)
        offered = {(site.room_id, site.material): idx for idx, site in enumerate(sites)}
        events = db.execute(
            "SELECT room_id,payload_json FROM main.events "
            "WHERE actor=? AND kind='gather' AND id<=? ORDER BY id DESC LIMIT 256",
            (rival_id, observed_through_event),
        ).fetchall()
        for event in events:
            if len(event["payload_json"]) > 2048:
                raise ValueError("Untrusted oversized event payload")
            try:
                payload = json.loads(event["payload_json"])
            except (ValueError, TypeError) as exc:
                raise ValueError("Malformed historical event payload") from exc
            if not isinstance(payload, dict):
                raise ValueError("Malformed historical gather event")
            material = payload.get("material")
            amount = payload.get("amount")
            if type(material) is not str or type(amount) is not int or amount <= 0:
                raise ValueError("Malformed historical gather event")
            index = offered.get((event["room_id"], material))
            if index is not None:
                counts[index] += 1
        observations = sum(counts)
        denominator = 2 * (observations + len(sites))
        # An occupied node is not necessarily exhausted: a second actor
        # can still receive a full batch while adequate stock remains.
        numerators = [
            available * denominator - (available - after_rival) * (count + 1)
            for available, after_rival, count in zip(capacity, after_rival_capacity, counts)
        ]
        scores = [Fraction(numerator, denominator) for numerator in numerators]
        max_score = max(scores)
        winners = [idx for idx, score in enumerate(scores) if score == max_score]
        # Symmetric pairwise planners sharing a host seed must not draw the
        # same tied site merely because their PRNG state starts identically.
        # Sort by site identity so different offered tuple orders agree.
        winners.sort(key=lambda idx: (sites[idx].room_id, sites[idx].material))
        start = random.Random(seed).randrange(len(winners))
        offset = 1 if actor_id.casefold() > rival_id.casefold() and len(winners) > 1 else 0
        selected = winners[(start + offset) % len(winners)]
        evidence = json.dumps({
            "schema": "realm.npc.economic_snapshot.v1", "world_id": expected_world_id,
            "actor": actor_id, "opponent": rival_id,
            "observed_through_event": observed_through_event, "current_event_max": latest,
            "resources": resources, "opponent_gather_counts": counts,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return ResourceChoice(
            world_id=expected_world_id, actor=actor_id, opponent=rival_id,
            site=sites[selected], available_units=capacity[selected],
            forecast_numerator=numerators[selected], forecast_denominator=denominator,
            observations=observations, observed_through_event=observed_through_event,
            evidence_sha256=hashlib.sha256(evidence).hexdigest(),
        )


def verify_economic_choice_in_transaction(
    db: sqlite3.Connection, *, choice: ResourceChoice,
    sites: tuple[ResourceSite, ...], seed: int,
) -> ResourceChoice:
    """Recheck a read-only proposal against current state in the host's transaction.

    The host must independently authenticate/authorize actor, site and action.
    A zero-row UPDATE on main.world_meta acquires SQLite's writer reservation
    inside the caller-owned transaction (also upgrading a deferred BEGIN). It
    fails closed if query_only, read-only, or another writer holds the reservation.
    No world row is touched; no receipt or gameplay action is created. The host
    must commit the eventual action in *this same transaction* to close TOCTOU.
    """
    if type(choice) is not ResourceChoice or choice.status != "HOST_REVIEW_REQUIRED":
        raise ValueError("Only an original host-review-required choice may be adjudicated")
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("Adjudication requires a caller-owned SQLite transaction")
    if db.row_factory is not sqlite3.Row:
        raise ValueError("Adjudication requires sqlite3.Row")
    files = db.execute("PRAGMA database_list").fetchall()
    main = next((row[2] for row in files if row[1] == "main"), None)
    if not main:
        raise ValueError("Adjudication requires a durable world database")
    if db.execute("PRAGMA query_only").fetchone()[0]:
        raise ValueError("Query-only transactions cannot adjudicate actions")
    try:
        # SQLite enters the write transaction before the zero-row scan. Unlike
        # db.in_transaction, this actually reserves the single-writer lease.
        probe = db.execute("UPDATE main.world_meta SET value=value WHERE 0")
    except sqlite3.DatabaseError as exc:
        raise ValueError("Cannot reserve the authoritative world writer lock") from exc
    if probe.rowcount != 0:
        raise ValueError("Unexpected world write during reservation")
    latest = db.execute("SELECT COALESCE(MAX(id),0) FROM main.events").fetchone()[0]
    if latest != choice.observed_through_event:
        raise ValueError("The proposed event watermark is stale")
    regenerated = choose_economic_site(
        main, expected_world_id=choice.world_id,
        actor=choice.actor, opponent=choice.opponent,
        sites=sites, observed_through_event=choice.observed_through_event,
        seed=seed, _connection=db,
    )
    if regenerated != choice:
        raise ValueError("Proposal evidence or contents no longer match world state")
    return replace(regenerated, status="VERIFIED_FOR_HOST_ACTION")
