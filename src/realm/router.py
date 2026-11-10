"""One command dispatcher shared by local terminal and HTTP entry points.

Both subsystems own their SQLite transactions.  A craft action must therefore
be dispatched *before* entering Engine.act's transaction, never from inside
Engine._perform; doing the latter would self-lock SQLite's writer.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from .crafting import Crafting
from .engine import Engine


class CommandRouter:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.crafting = Crafting(engine.db_path)

    def act(self, player: str, command: str, *, action_id: str | None = None,
            writer_guard: Callable[[sqlite3.Connection], None] | None = None) -> str:
        result = self.crafting.perform(
            player, command, action_id=action_id, writer_guard=writer_guard
        )
        if result is not None:
            return result
        return self.engine.act(
            player, command, action_id=action_id, writer_guard=writer_guard
        )

    def seed_demo_crafting(self) -> None:
        """Explicit, noncanonical demo fixtures; safe to repeat without refilling.

        Never invoke this for a private worldpack unless its owner specifically
        wants the demonstration material/recipe fixtures mixed into that world.
        """
        self.crafting.place_node("atrium", "stone", stock=40, batch_yield=3)
        self.crafting.place_node("corridor", "wood", stock=40, batch_yield=4)
        self.crafting.register_recipe("field knife", kind="craft", ingredients={"stone": 2})
        self.crafting.register_recipe(
            "supply cache", kind="build", ingredients={"stone": 2, "wood": 4}
        )
