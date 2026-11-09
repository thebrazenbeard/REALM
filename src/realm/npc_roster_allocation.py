"""Read-only, capacity-aware full-roster NPC allocation proposal.

This is an opt-in *host welfare* policy, not independent NPC best-response,
a Nash equilibrium, authenticated action, or a writer-locked world mutation.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3

from .npc_game_theory import ResourceSite


@dataclass(frozen=True)
class RosterAssignment:
    actor: str
    site: ResourceSite | None
    expected_units: int
    expected_utility: int


@dataclass(frozen=True)
class RosterProposal:
    world_id: str
    observed_through_event: int
    seed: int
    assignments: tuple[RosterAssignment, ...]
    total_expected_units: int
    total_expected_utility: int
    evidence_sha256: str
    status: str = "HOST_REVIEW_REQUIRED"


def propose_roster_allocation(
    db_path: str | Path, *, expected_world_id: str,
    actors: tuple[str, ...], sites: tuple[ResourceSite, ...],
    observed_through_event: int, seed: int,
    site_values: dict[ResourceSite, int] | None = None,
) -> RosterProposal:
    """Maximize symmetric expected harvest using a complete host-supplied roster.

    Capacity is counted once per site; excess actors receive a WAIT proposal.
    No world, receipt, actor, or resource is mutated. A host must separately
    authorize every actor and revalidate stock under its own writer transaction.
    """
    if type(expected_world_id) is not str or not expected_world_id:
        raise ValueError("A bound world ID is required")
    if (type(actors) is not tuple or not 2 <= len(actors) <= 8
            or any(type(a) is not str or not a for a in actors)
            or len({a.casefold() for a in actors}) != len(actors)):
        raise ValueError("Two to eight unique host actors required")
    if (type(sites) is not tuple or not 2 <= len(sites) <= 8
            or any(type(s) is not ResourceSite or type(s.room_id) is not str
                   or type(s.material) is not str or not s.room_id or not s.material
                   for s in sites) or len(set(sites)) != len(sites)):
        raise ValueError("Two to eight unique host sites required")
    if site_values is None:
        values = {site: 1 for site in sites}
    elif (type(site_values) is not dict or set(site_values) != set(sites)
          or any(type(v) is not int or not 1 <= v <= 1000 for v in site_values.values())):
        raise ValueError("Host site utilities must be bounded positive integers")
    else:
        values = site_values
    if type(observed_through_event) is not int or not 0 <= observed_through_event <= 2**63 - 1:
        raise ValueError("A bounded event watermark is required")
    if type(seed) is not int or seed.bit_length() > 64:
        raise ValueError("A bounded host seed is required")
    path = Path(db_path).resolve()
    if not path.is_file():
        raise ValueError("World DB missing")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        world = db.execute("SELECT value FROM main.world_meta WHERE key='world_id'").fetchone()
        if world is None or world["value"] != expected_world_id:
            raise ValueError("Different or unbound world")
        actual_actors = []
        for actor in actors:
            row = db.execute("SELECT id FROM main.players WHERE id=? COLLATE NOCASE", (actor,)).fetchone()
            if row is None:
                raise ValueError("Host actor is not joined")
            actual_actors.append(row["id"])
        latest = db.execute("SELECT COALESCE(MAX(id),0) FROM main.events").fetchone()[0]
        if observed_through_event > latest:
            raise ValueError("Event watermark exceeds durable events")
        ordered_sites = tuple(sorted(sites, key=lambda s: (s.room_id, s.material)))
        stocks = []
        for site in ordered_sites:
            row = db.execute(
                "SELECT remaining,batch_yield FROM main.gathering_nodes "
                "WHERE room_id=? AND material=?", (site.room_id, site.material),
            ).fetchone()
            if row is None or row["remaining"] < 0 or row["batch_yield"] <= 0:
                raise ValueError("Missing or invalid host resource node")
            stocks.append((int(row["remaining"]), int(row["batch_yield"])))
        if not any(remaining > 0 for remaining, batch in stocks):
            raise ValueError("No harvestable resources")
        canonical_actors = tuple(sorted(actual_actors, key=lambda a: (a.casefold(), a)))
        def priority(actor: str, site: ResourceSite | None) -> str:
            loc = "WAIT" if site is None else f"{site.room_id}/{site.material}"
            return hashlib.sha256(
                f"{seed}|{expected_world_id}|{observed_through_event}|{actor}|{loc}".encode()
            ).hexdigest()

        @lru_cache(maxsize=None)
        def solve(index: int, picks: tuple[int, ...]) -> tuple[int, tuple[tuple[int, int], ...]]:
            if index == len(canonical_actors):
                return 0, ()
            actor = canonical_actors[index]
            options: list[tuple[int, tuple[tuple[int, int], ...], str]] = []
            next_total, suffix = solve(index + 1, picks)
            options.append((next_total, ((-1, 0),) + suffix, priority(actor, None)))
            for idx, (remaining, batch) in enumerate(stocks):
                units = min(batch, max(0, remaining - picks[idx] * batch))
                if not units:
                    continue
                next_picks = list(picks)
                next_picks[idx] += 1
                next_total, suffix = solve(index + 1, tuple(next_picks))
                options.append((units * values[ordered_sites[idx]] + next_total, ((idx, units),) + suffix,
                                priority(actor, ordered_sites[idx])))
            best_score = max(result[0] for result in options)
            best = min((r for r in options if r[0] == best_score), key=lambda r: r[2])
            return best[0], best[1]

        total_utility, winners = solve(0, (0,) * len(ordered_sites))
        assignments = tuple(
            RosterAssignment(actor, None if idx < 0 else ordered_sites[idx], units,
                             0 if idx < 0 else units * values[ordered_sites[idx]])
            for actor, (idx, units) in zip(canonical_actors, winners)
        )
        evidence = json.dumps({
            "schema": "realm.npc.roster_snapshot.v1", "world_id": expected_world_id,
            "actors": canonical_actors, "sites": [
                (site.room_id, site.material, *stock) for site, stock in zip(ordered_sites, stocks)
            ], "site_values": [
                (site.room_id, site.material, values[site]) for site in ordered_sites
            ], "event_id": observed_through_event, "latest_event_id": latest, "seed": seed,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return RosterProposal(
            world_id=expected_world_id, observed_through_event=observed_through_event,
            seed=seed, assignments=assignments,
            total_expected_units=sum(a.expected_units for a in assignments),
            total_expected_utility=total_utility,
            evidence_sha256=hashlib.sha256(evidence).hexdigest(),
        )
