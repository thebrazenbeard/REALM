"""Optional, deterministic strategic-interaction laboratory over actual REALM state.

Two host-bound principals choose resource locations without seeing the other's
current choice. This is a test harness, not an autonomous resident or service.
All world actions use the existing Engine/Crafting validation and receipts.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import random
import sqlite3
from tempfile import TemporaryDirectory
from typing import Callable

from realm.crafting import Crafting
from realm.engine import Engine


PLAYERS = ("Ada", "Ben")
SITES = {"A": ("atrium", "stone"), "B": ("corridor", "wood")}
Policy = Callable[["Observation"], str]


@dataclass(frozen=True)
class Round:
    index: int
    choices: tuple[str, str]
    payoffs: tuple[int, int]
    first_actor: str
    replies: tuple[str, str]


@dataclass(frozen=True)
class Observation:
    player: str
    completed: tuple[Round, ...]


@dataclass(frozen=True)
class Trial:
    rounds: tuple[Round, ...]
    total_payoffs: tuple[int, int]
    collision_count: int
    efficiency: float


def always(site: str) -> Policy:
    """A fixed-action control, never a neural model."""
    _validate_site(site)
    return lambda observation: site


def _validate_site(site: object) -> str:
    if type(site) is not str or site not in SITES:
        raise ValueError("Policy must propose exactly one site: A or B")
    return site


def _play_round(
    db_path: Path, index: int, choices: tuple[str, str], first: int,
) -> Round:
    """Host-provision actors, then commit the already collected choices.

    Persist a whole-round commitment BEFORE any movement or gathering. A
    conflicting retry may never partly apply an alternative action sequence.
    """
    if type(index) is not int or index < 0 or type(first) is not int or first not in (0, 1):
        raise ValueError("Round index and first actor must be valid integers")
    if type(choices) is not tuple or len(choices) != 2:
        raise ValueError("Round requires exactly two site choices")
    choices = (_validate_site(choices[0]), _validate_site(choices[1]))
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS strategy_round_commitments (
            round_index INTEGER PRIMARY KEY,
            ada_site TEXT NOT NULL,
            ben_site TEXT NOT NULL,
            first_index INTEGER NOT NULL
        )""")
        committed = db.execute(
            "SELECT ada_site,ben_site,first_index FROM strategy_round_commitments WHERE round_index=?",
            (index,),
        ).fetchone()
        expected = (choices[0], choices[1], first)
        if committed is None:
            db.execute("INSERT INTO strategy_round_commitments VALUES (?,?,?,?)", (index, *expected))
        elif committed != expected:
            raise ValueError("conflicting round commitment; replay refused before world effects")
    engine = Engine(db_path)
    for actor in PLAYERS:
        engine.join(actor)
    crafting = Crafting(db_path)
    crafting.place_node("atrium", "stone", stock=3, batch_yield=3)
    crafting.place_node("corridor", "wood", stock=3, batch_yield=3)
    for actor, site in zip(PLAYERS, choices):
        if site == "B":
            engine.act(actor, "north", action_id=f"round-{index}-{actor}-move")
    replies = ["", ""]
    for actor_index in (first, 1 - first):
        actor = PLAYERS[actor_index]
        material = SITES[choices[actor_index]][1]
        replies[actor_index] = crafting.perform(
            actor, f"gather {material}",
            action_id=f"round-{index}-{actor}-gather",
        )
    with closing(sqlite3.connect(db_path)) as db:
        payoffs = tuple(
            db.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM material_pouches WHERE actor=?",
                (actor,),
            ).fetchone()[0]
            for actor in PLAYERS
        )
    return Round(index, choices, payoffs, PLAYERS[first], tuple(replies))


def run_trial(
    ada_policy: Policy, ben_policy: Policy, *, rounds: int, seed: int,
) -> Trial:
    """Evaluate actual scarcity payoffs; policies see only past rounds."""
    if type(rounds) is not int or not 1 <= rounds <= 1000:
        raise ValueError("Rounds must be an integer between 1 and 1000")
    if type(seed) is not int:
        raise ValueError("Seed must be an integer")
    if not callable(ada_policy) or not callable(ben_policy):
        raise TypeError("Both policies must be callable")
    initial_first = random.Random(seed).randrange(2)
    history: list[Round] = []
    with TemporaryDirectory(prefix="realm-strategy-") as directory:
        for index in range(rounds):
            completed = tuple(history)
            # Proposals are collected before any action can change world state.
            choices = (
                _validate_site(ada_policy(Observation("Ada", completed))),
                _validate_site(ben_policy(Observation("Ben", completed))),
            )
            history.append(
                _play_round(
                    Path(directory) / f"round-{index}.sqlite3",
                    index, choices, (initial_first + index) % 2,
                )
            )
    total_payoffs = (
        sum(r.payoffs[0] for r in history),
        sum(r.payoffs[1] for r in history),
    )
    collisions = sum(r.choices[0] == r.choices[1] for r in history)
    return Trial(tuple(history), total_payoffs, collisions, sum(total_payoffs) / (6 * rounds))


def avoid_last(observation: Observation) -> str:
    """Choose the site opposite the opponent's last *completed* action.

    This deterministic reaction is not learning, opponent inference, or neural AI.
    """
    if not observation.completed:
        return "A"
    opponent_index = 1 if observation.player == "Ada" else 0
    last_opponent = observation.completed[-1].choices[opponent_index]
    return "B" if last_opponent == "A" else "A"


def seeded_random(seed: int) -> Policy:
    """Reproducible random baseline, independent of opponent observations."""
    if type(seed) is not int:
        raise ValueError("Strategy seed must be an integer")
    rng = random.Random(seed)
    return lambda observation: rng.choice(("A", "B"))
