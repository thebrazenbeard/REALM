"""Original, offline, proposal-only game policy.

No imported weights, database, files, runtime model, or actor authority.
The trusted REALM host supplies *visible* observations, exact legal command
candidates, and post-commit outcomes with independently evaluated rewards.
This is a tiny learned value table + observed navigation graph, not an LLM.
Instantiate one per resident to prevent cross-character private-memory leaks.
"""

from __future__ import annotations

import math
import json
from collections import deque
from collections.abc import Mapping

_DIRECTIONS = frozenset({"north", "south", "east", "west", "up", "down"})
_SHORT = frozenset({"n", "s", "e", "w", "u", "d"})
_VERBS = _DIRECTIONS | _SHORT | {"go", "look", "say", "take", "drop", "inventory"}
_EXPAND = {"n": "north", "s": "south", "e": "east", "w": "west", "u": "up", "d": "down"}


def _text(value: object, label: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or not all(c.isprintable() for c in value):
        raise ValueError(f"{label} must be bounded printable text")
    return value


def _valid_command(command: str) -> bool:
    if not command or len(command) > 512 or command != command.strip() or not all(c.isprintable() for c in command):
        return False
    verb, _, arg = command.partition(" ")
    verb = verb.casefold()
    if verb in _DIRECTIONS | _SHORT | {"look", "inventory"}:
        return not arg
    if verb == "go":
        return arg.casefold() in _DIRECTIONS | _SHORT
    if verb == "say":
        return bool(arg.strip()) and len(arg) <= 280
    if verb in {"take", "drop"}:
        return bool(arg.strip()) and len(arg) <= 120
    return False


def _observation(raw: Mapping) -> dict:
    if not isinstance(raw, Mapping):
        raise ValueError("observation must be a mapping")
    world = _text(raw.get("world_id"), "world_id")
    room = _text(raw.get("room"), "room")
    goal = raw.get("goal")
    if not isinstance(goal, Mapping) or goal.get("kind") not in {"explore", "reach", "take"}:
        raise ValueError("goal needs an approved kind")
    target = goal.get("target", "")
    if not isinstance(target, str) or len(target) > 128 or not all(c.isprintable() for c in target):
        raise ValueError("invalid goal target")
    clean = {"world_id": world, "room": room, "goal": {"kind": goal["kind"], "target": target}}
    for field, limit in (("exits", 8), ("items", 48), ("inventory", 48), ("allowed_commands", 32)):
        values = raw.get(field)
        if not isinstance(values, (list, tuple)) or len(values) > limit or (field == "allowed_commands" and not values):
            raise ValueError(f"{field} must be a bounded list")
        clean[field] = [_text(value, field, 512 if field == "allowed_commands" else 128) for value in values]
    if any(direction.casefold() not in _DIRECTIONS for direction in clean["exits"]):
        raise ValueError("unsupported visible exit")
    commands = clean["allowed_commands"]
    if len(set(commands)) != len(commands) or any(not _valid_command(command) for command in commands):
        raise ValueError("unsupported or duplicate authorized command")
    return clean


class OriginalNPCPolicy:
    """Original learned action ranker with only host-observed room transitions."""

    def __init__(self, seed: int = 0, *, world_id: str | None = None,
                 actor_id: str | None = None):
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise ValueError("seed must be a nonnegative 32-bit integer")
        if (world_id is None) != (actor_id is None):
            raise ValueError("world and actor bindings must be supplied together")
        self.seed = seed
        self._world_id = _text(world_id, "world_id") if world_id is not None else None
        self._actor_id = _text(actor_id, "actor_id") if actor_id is not None else None
        self._values = {}
        self._visits = {}
        self._edges = {}
        self._receipts = {}

    @staticmethod
    def _key(obs: dict, command: str) -> tuple:
        return (obs["world_id"], obs["room"], obs["goal"]["kind"], obs["goal"]["target"], command)

    def _route(self, world: str, start: str, goal: str, allowed: list[str]) -> str | None:
        queue = deque([(start, None)])
        seen = {start}
        while queue and len(seen) < 2048:
            room, first_action = queue.popleft()
            for (w, origin, command), dest in sorted(self._edges.items()):
                if w != world or origin != room or dest in seen or (room == start and command not in allowed):
                    continue
                first = first_action or command
                if dest == goal:
                    return first
                seen.add(dest)
                queue.append((dest, first))
        return None

    def propose(self, observation: Mapping) -> dict[str, str]:
        obs = _observation(observation)
        if self._world_id is not None and obs["world_id"] != self._world_id:
            raise ValueError("policy world binding mismatch")
        def visible(command: str) -> bool:
            verb, _, arg = command.partition(" ")
            direction = arg.lower() if verb.lower() == "go" else verb.lower()
            if direction not in _DIRECTIONS | _SHORT:
                return True
            return _EXPAND.get(direction, direction) in {e.lower() for e in obs["exits"]}
        commands = sorted(command for command in obs["allowed_commands"] if visible(command))
        if not commands:
            raise ValueError("no legal visible action candidates")
        goal = obs["goal"]
        complete = ((goal["kind"] == "reach" and goal["target"] == obs["room"])
                    or (goal["kind"] == "take" and goal["target"] in obs["inventory"]))
        if complete:
            # The host scheduler should stop issuing objective turns; a
            # permitted read-only look is a safe fallback, never movement.
            if "look" in commands:
                return {"command": "look"}
            raise ValueError("goal complete; host should stop scheduling this objective")
        if goal["kind"] == "take" and goal["target"] in obs["items"]:
            action = "take " + goal["target"]
            if action in commands:
                return {"command": action}
        if goal["kind"] == "reach" and goal["target"] != obs["room"]:
            action = self._route(obs["world_id"], obs["room"], goal["target"], commands)
            if action is not None:
                return {"command": action}
        def rank(command: str) -> tuple[float, int]:
            key = self._key(obs, command)
            verb = command.partition(" ")[0].casefold()
            move_bonus = 0.2 if verb in _DIRECTIONS | _SHORT | {"go"} else 0
            count = self._visits.get(key, 0)
            return (self._values.get(key, 0.0) + move_bonus + 0.05 / (1 + count), -count)
        return {"command": max(commands, key=rank)}

    def learn(self, before: Mapping, command: str, after: Mapping, *, reward: float, receipt_id: str) -> bool:
        prior, later = _observation(before), _observation(after)
        if prior["world_id"] != later["world_id"]:
            raise ValueError("world mismatch")
        if self._world_id is not None and prior["world_id"] != self._world_id:
            raise ValueError("policy world binding mismatch")
        if command not in prior["allowed_commands"]:
            raise ValueError("command not explicitly authorized")
        if isinstance(reward, bool) or not isinstance(reward, (float, int)) or not math.isfinite(reward) or abs(reward) > 10:
            raise ValueError("reward must be finite and bounded")
        _text(receipt_id, "receipt_id")
        receipt = (prior["world_id"], receipt_id)
        evidence = (prior["room"], command, later["room"], float(reward))
        if receipt in self._receipts:
            if self._receipts[receipt] != evidence:
                raise ValueError("conflicting replay receipt")
            return False
        if len(self._receipts) >= 4096 or len(self._values) >= 16384:
            raise ValueError("learning budget exhausted")
        self._receipts[receipt] = evidence
        key = self._key(prior, command)
        old = self._values.get(key, 0.0)
        self._values[key] = old + 0.5 * (float(reward) - old)
        self._visits[key] = self._visits.get(key, 0) + 1
        verb = command.partition(" ")[0].casefold()
        if verb in _DIRECTIONS | _SHORT | {"go"} and prior["room"] != later["room"]:
            self._edges[(prior["world_id"], prior["room"], command)] = later["room"]
        return True

    def export_state(self) -> dict[str, object]:
        """JSON-safe diagnostics with unambiguous tuple keys; no restore API."""
        def encoded(key: tuple) -> str:
            return json.dumps(key, separators=(",", ":"), ensure_ascii=True)
        return {
            "schema": "realm-original-npc-policy-v2",
            "seed": self.seed,
            "values": {encoded(key): v for key, v in sorted(self._values.items())},
            "visits": {encoded(key): v for key, v in sorted(self._visits.items())},
            "edges": {encoded(key): v for key, v in sorted(self._edges.items())},
            "receipts": len(self._receipts),
        }

    def export_checkpoint(self) -> dict[str, object]:
        """Host-requested, complete JSON-safe state; never writes on its own.

        Only actor/world-bound policy instances may be persisted. This includes
        the original receipt evidence, so a restart cannot silently retrain an
        already-consumed action receipt.
        """
        if self._world_id is None or self._actor_id is None:
            raise ValueError("checkpoint requires explicit world/actor binding")
        world = self._world_id
        if any(key[0] != world for key in self._values):
            raise ValueError("cross-world policy values")
        if any(key[0] != world for key in self._visits):
            raise ValueError("cross-world policy visits")
        if any(key[0] != world for key in self._edges):
            raise ValueError("cross-world policy edges")
        if any(key[0] != world for key in self._receipts):
            raise ValueError("cross-world policy receipts")
        return {
            "schema": "realm-npc-checkpoint-v1",
            "world_id": world,
            "actor_id": self._actor_id,
            "seed": self.seed,
            "values": [list(key[1:]) + [value] for key, value in sorted(self._values.items())],
            "visits": [list(key[1:]) + [count] for key, count in sorted(self._visits.items())],
            "edges": [list(key[1:]) + [dest] for key, dest in sorted(self._edges.items())],
            "receipts": [list(key[1:]) + list(evidence)
                         for key, evidence in sorted(self._receipts.items())],
        }

    @classmethod
    def from_checkpoint(cls, payload: Mapping, *, world_id: str,
                        actor_id: str) -> "OriginalNPCPolicy":
        """Strict restoration of validated host-custodied state, not authority.

        A checkpoint is untrusted data; it cannot change the host's expected
        world/actor binding. A hash in an outer file is corruption detection,
        not authentication against an attacker who can rewrite the file.
        """
        world = _text(world_id, "world_id")
        actor = _text(actor_id, "actor_id")
        expected = {"schema", "world_id", "actor_id", "seed", "values",
                    "visits", "edges", "receipts"}
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise ValueError("invalid checkpoint fields")
        if payload["schema"] != "realm-npc-checkpoint-v1":
            raise ValueError("checkpoint schema mismatch")
        if payload["world_id"] != world or payload["actor_id"] != actor:
            raise ValueError("checkpoint world/actor binding mismatch")
        learned = cls(payload["seed"], world_id=world, actor_id=actor)

        def bounded_rows(name: str, limit: int, width: int) -> list:
            rows = payload[name]
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError(f"invalid {name} count")
            if any(not isinstance(row, list) or len(row) != width for row in rows):
                raise ValueError(f"invalid {name} row")
            return rows

        def room(value: object) -> str:
            return _text(value, "checkpoint room")

        def command(value: object) -> str:
            if not isinstance(value, str) or not _valid_command(value):
                raise ValueError("invalid checkpoint command")
            return value

        def action_key(row: list) -> tuple:
            origin = room(row[0])
            kind, target = row[1:3]
            if not isinstance(kind, str) or kind not in {"explore", "reach", "take"}:
                raise ValueError("invalid checkpoint goal")
            if not isinstance(target, str) or len(target) > 128 or not all(c.isprintable() for c in target):
                raise ValueError("invalid checkpoint target")
            return (world, origin, kind, target, command(row[3]))

        for row in bounded_rows("values", 16384, 5):
            key = action_key(row)
            value = row[4]
            if (key in learned._values or type(value) not in (float, int)
                    or not math.isfinite(value) or abs(value) > 10):
                raise ValueError("invalid or duplicate checkpoint value")
            learned._values[key] = float(value)

        for row in bounded_rows("visits", 16384, 5):
            key = action_key(row)
            count = row[4]
            if key in learned._visits or type(count) is not int or not 1 <= count <= 4096:
                raise ValueError("invalid or duplicate checkpoint visits")
            learned._visits[key] = count
        if set(learned._values) != set(learned._visits):
            raise ValueError("checkpoint values/visits disagree")

        for row in bounded_rows("edges", 4096, 3):
            origin, move, dest = room(row[0]), command(row[1]), room(row[2])
            verb = move.partition(" ")[0].casefold()
            if verb not in _DIRECTIONS | _SHORT | {"go"}:
                raise ValueError("checkpoint edge must be movement")
            key = (world, origin, move)
            if key in learned._edges:
                raise ValueError("duplicate checkpoint edge")
            learned._edges[key] = dest

        for row in bounded_rows("receipts", 4096, 5):
            rid, source, move, dest, reward = row
            key = (world, _text(rid, "receipt_id"))
            if (key in learned._receipts or type(reward) not in (int, float)
                    or not math.isfinite(reward) or abs(reward) > 10):
                raise ValueError("invalid or duplicate checkpoint receipt")
            learned._receipts[key] = (room(source), command(move), room(dest), float(reward))

        if sum(learned._visits.values()) != len(learned._receipts):
            raise ValueError("checkpoint receipt and visit totals disagree")
        observed_moves = {(r[0], r[1], r[2]) for r in learned._receipts.values()}
        if any((key[1], key[2], dest) not in observed_moves
               for key, dest in learned._edges.items()):
            raise ValueError("checkpoint edge not supported by a receipt")
        return learned
