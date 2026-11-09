"""Optional, setting-neutral gathering, crafting and construction for REALM.

The host/worldpack registers resource nodes and recipes.  Players can then use
``gather``, ``craft`` and ``build``.  Each action is an atomic, idempotent SQLite
transaction against the same world database as :class:`realm.engine.Engine`.

This module deliberately does not seed resources or fictional lore by default.
It is a command adapter; a host can integrate it with its CLI, API or NPC driver.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .engine import Engine


_KEY = re.compile(r"^[a-z][a-z0-9 _-]{0,47}$")
_COMMANDS = frozenset({"gather", "craft", "build", "resources", "recipes", "structures"})
_MAX_BATCHES = 10


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Names must be strings")
    key = value.strip().lower()
    if not _KEY.fullmatch(key):
        raise ValueError("Names must start with a letter and contain at most 48 plain characters")
    return key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Crafting:
    """World-local resource economy with no time gates or mandatory grind loops.

    World-author APIs ``place_node`` and ``register_recipe`` are deliberately
    separate from player commands.  They must not be reachable by untrusted
    players.  ``perform`` returns None for verbs owned by another command module.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        Engine(self.db_path)  # Ensure the underlying world schema exists.
        self._initialize()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS gathering_nodes (
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    material TEXT NOT NULL,
                    remaining INTEGER NOT NULL CHECK (remaining >= 0),
                    batch_yield INTEGER NOT NULL CHECK (batch_yield BETWEEN 1 AND 100),
                    PRIMARY KEY (room_id, material)
                );
                CREATE TABLE IF NOT EXISTS material_pouches (
                    actor TEXT NOT NULL REFERENCES players(id),
                    material TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity >= 0),
                    PRIMARY KEY (actor, material)
                );
                CREATE TABLE IF NOT EXISTS craft_recipes (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('craft', 'build')),
                    ingredients_json TEXT NOT NULL,
                    output TEXT NOT NULL,
                    output_count INTEGER NOT NULL CHECK (output_count BETWEEN 1 AND 100)
                );
                CREATE TABLE IF NOT EXISTS crafted_goods (
                    actor TEXT NOT NULL REFERENCES players(id),
                    good TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity >= 0),
                    PRIMARY KEY (actor, good)
                );
                CREATE TABLE IF NOT EXISTS built_structures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    recipe TEXT NOT NULL REFERENCES craft_recipes(id),
                    owner TEXT NOT NULL REFERENCES players(id),
                    constructed_at TEXT NOT NULL,
                    UNIQUE (room_id, recipe, owner)
                );
                """
            )

    def place_node(self, room_id: str, material: str, *, stock: int, batch_yield: int = 3) -> bool:
        """World-author insertion. Does not silently refill depleted nodes."""
        material = _name(material)
        if type(stock) is not int or stock < 0 or type(batch_yield) is not int or not 1 <= batch_yield <= 100:
            raise ValueError("Stock must be nonnegative and batch yield must be 1–100")
        with self._connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO gathering_nodes VALUES (?,?,?,?)",
                (room_id, material, stock, batch_yield),
            )
            return result.rowcount == 1

    def register_recipe(
        self, recipe_id: str, *, kind: str, ingredients: Mapping[str, int],
        output: str | None = None, output_count: int = 1,
    ) -> bool:
        """World-author insertion of a deterministic, non-scripting recipe."""
        recipe_id = _name(recipe_id)
        output = _name(output or recipe_id)
        if kind not in {"craft", "build"}:
            raise ValueError("Recipe kind must be craft or build")
        if not ingredients or type(output_count) is not int or not 1 <= output_count <= 100:
            raise ValueError("Recipes need ingredients and 1–100 outputs")
        if kind == "build" and output_count != 1:
            raise ValueError("Construction recipes create one persistent structure")
        clean: dict[str, int] = {}
        for material, count in ingredients.items():
            if type(count) is not int or not 1 <= count <= 10000:
                raise ValueError("Ingredient counts must be positive integers")
            key = _name(material)
            if key in clean:
                raise ValueError("Duplicate normalized ingredient")
            clean[key] = count
        normalized = json.dumps(clean, sort_keys=True)
        with self._connect() as db:
            existing = db.execute("SELECT * FROM craft_recipes WHERE id=?", (recipe_id,)).fetchone()
            if existing:
                if (existing["kind"], existing["ingredients_json"], existing["output"], existing["output_count"]) != (kind, normalized, output, output_count):
                    raise ValueError("Recipe ID already defined differently")
                return False
            db.execute(
                "INSERT INTO craft_recipes VALUES (?,?,?,?,?)",
                (recipe_id, kind, normalized, output, output_count),
            )
            return True

    def perform(self, actor: str, command: str, *, action_id: str | None = None,
                writer_guard: Callable[[sqlite3.Connection], None] | None = None) -> str | None:
        """Run one recognized player command; return None for unrelated verbs.

        The host must authenticate ``actor`` before calling this method.  Local
        actor names are **not** authentication.  Optional ``action_id`` shares
        the core action-receipt namespace and disallows replay with new text.
        """
        if not isinstance(command, str):
            raise ValueError("Command must be text")
        command = command.strip()
        verb, _, arg = command.partition(" ")
        verb, arg = verb.lower(), arg.strip()
        if verb not in _COMMANDS:
            return None
        if action_id is not None and (not isinstance(action_id, str) or not 1 <= len(action_id) <= 128):
            raise ValueError("Action IDs must be nonempty strings up to 128 characters")

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if writer_guard is not None:
                writer_guard(db)
            player = db.execute("SELECT id,room_id FROM players WHERE id=? COLLATE NOCASE", (actor,)).fetchone()
            if player is None:
                raise ValueError("Join this REALM before gathering or crafting")
            actor, room = player["id"], player["room_id"]
            if action_id is not None:
                receipt = db.execute(
                    "SELECT command,response FROM action_receipts WHERE actor=? AND action_id=?",
                    (actor, action_id),
                ).fetchone()
                if receipt:
                    if receipt["command"] != command:
                        raise ValueError("Action ID already used for a different command")
                    return receipt["response"]

            if verb == "resources":
                found = db.execute(
                    "SELECT material,quantity FROM material_pouches WHERE actor=? AND quantity>0 ORDER BY material",
                    (actor,),
                ).fetchall()
                response = "Materials: " + (", ".join(f"{r['material']} x{r['quantity']}" for r in found) or "none")
                goods = db.execute(
                    "SELECT good,quantity FROM crafted_goods WHERE actor=? AND quantity>0 ORDER BY good",
                    (actor,),
                ).fetchall()
                response += "\nCrafted goods: " + (", ".join(f"{r['good']} x{r['quantity']}" for r in goods) or "none")
            elif verb == "recipes":
                found = db.execute("SELECT id,kind,ingredients_json FROM craft_recipes ORDER BY id").fetchall()
                response = "Available recipes:\n" + ("\n".join(
                    f"{r['kind']} {r['id']}: " + ", ".join(f"{k} x{v}" for k, v in json.loads(r['ingredients_json']).items())
                    for r in found
                ) or "none")
            elif verb == "structures":
                found = db.execute(
                    "SELECT recipe,owner FROM built_structures WHERE room_id=? ORDER BY id", (room,)
                ).fetchall()
                response = "Structures here: " + (", ".join(f"{r['recipe']} ({r['owner']})" for r in found) or "none")
            elif verb == "gather":
                response = self._gather(db, actor, room, arg)
            else:
                response = self._make(db, actor, room, verb, arg)

            if action_id is not None:
                db.execute(
                    "INSERT INTO action_receipts (actor,action_id,command,response) VALUES (?,?,?,?)",
                    (actor, action_id, command, response),
                )
            return response

    def _gather(self, db: sqlite3.Connection, actor: str, room: str, arg: str,
                *, emissions: list[tuple[int, str]] | None = None) -> str:
        if not arg:
            nodes = db.execute(
                "SELECT material,remaining FROM gathering_nodes WHERE room_id=? ORDER BY material", (room,)
            ).fetchall()
            return "Gatherable here: " + (", ".join(f"{n['material']} ({n['remaining']})" for n in nodes) or "none")
        material, _, maybe_batches = arg.rpartition(" ")
        if maybe_batches.isdecimal():
            batches = int(maybe_batches)
            if not 1 <= batches <= _MAX_BATCHES:
                return f"Gather between 1 and {_MAX_BATCHES} batches per action."
        else:
            material, batches = arg, 1
        try:
            material = _name(material)
        except ValueError:
            return "Gather which material?"
        node = db.execute(
            "SELECT remaining,batch_yield FROM gathering_nodes WHERE room_id=? AND material=?",
            (room, material),
        ).fetchone()
        if not node:
            return f"There is no {material} to gather here."
        amount = min(node["remaining"], batches * node["batch_yield"])
        if amount == 0:
            return f"No {material} remains here."
        db.execute(
            "UPDATE gathering_nodes SET remaining=remaining-? WHERE room_id=? AND material=? AND remaining>=?",
            (amount, room, material, amount),
        )
        db.execute(
            "INSERT INTO material_pouches VALUES (?,?,?) ON CONFLICT(actor,material) DO UPDATE SET quantity=quantity+excluded.quantity",
            (actor, material, amount),
        )
        self._event(db, actor, room, "gather", {"material": material, "amount": amount},
                    emissions=emissions)
        return f"You gather {material} x{amount}."

    def _make(self, db: sqlite3.Connection, actor: str, room: str, kind: str, arg: str) -> str:
        if not arg:
            return f"{kind.capitalize()} what? Type 'recipes' for options."
        recipe = db.execute("SELECT * FROM craft_recipes WHERE id=? AND kind=?", (arg.lower(), kind)).fetchone()
        if recipe is None:
            return f"No {kind} recipe named {arg!r}."
        if kind == "build":
            existing = db.execute(
                "SELECT id FROM built_structures WHERE room_id=? AND recipe=? AND owner=?",
                (room, recipe["id"], actor),
            ).fetchone()
            if existing:
                return f"You already built {recipe['output']} here."
        ingredients = json.loads(recipe["ingredients_json"])
        holdings = {
            row["material"]: row["quantity"] for row in db.execute(
                "SELECT material,quantity FROM material_pouches WHERE actor=?", (actor,)
            )
        }
        missing = [f"{material} x{qty-have}" for material, qty in ingredients.items()
                   if (have := holdings.get(material, 0)) < qty]
        if missing:
            return "Missing materials: " + ", ".join(missing)
        for material, count in ingredients.items():
            db.execute(
                "UPDATE material_pouches SET quantity=quantity-? WHERE actor=? AND material=? AND quantity>=?",
                (count, actor, material, count),
            )
        if kind == "craft":
            db.execute(
                "INSERT INTO crafted_goods VALUES (?,?,?) ON CONFLICT(actor,good) DO UPDATE SET quantity=quantity+excluded.quantity",
                (actor, recipe["output"], recipe["output_count"]),
            )
            response = f"You craft {recipe['output']} x{recipe['output_count']}."
        else:
            db.execute(
                "INSERT INTO built_structures (room_id,recipe,owner,constructed_at) VALUES (?,?,?,?)",
                (room, recipe["id"], actor, _now()),
            )
            response = f"You build {recipe['output']} here."
        self._event(db, actor, room, kind, {"recipe": recipe["id"], "output": recipe["output"]})
        return response

    @staticmethod
    def _event(db: sqlite3.Connection, actor: str, room: str, kind: str,
               payload: dict, *, emissions: list[tuple[int, str]] | None = None) -> None:
        values = (_now(), actor, room, kind, json.dumps(payload, sort_keys=True))
        sql = "INSERT INTO events (occurred_at,actor,room_id,kind,payload_json) VALUES (?,?,?,?,?)"
        if emissions is None:
            db.execute(sql, values)
        else:
            # The RETURNING tuple is the original outer insert, unaffected by
            # AFTER INSERT triggers that can rewrite durable event timestamps.
            with closing(db.execute(sql + " RETURNING id,occurred_at", values)) as cursor:
                emitted = cursor.fetchone()
            if emitted is None:
                raise ValueError("Gather emitter did not return its inserted row")
            emissions.append((emitted[0], emitted[1]))
