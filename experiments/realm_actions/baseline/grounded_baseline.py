"""Minimal, untrained REALM action baseline driven only by player-visible observations.

This intentionally narrow rule set provides a reproducible counterfactual for
evaluating future neural candidates. It never queries world storage, sees target
labels, changes REALM state, or executes its proposals. A default abstention is
safer than inventing map exits, inventory, or authorizations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Mapping


_DIRECTIONS = frozenset({"north", "south", "east", "west", "up", "down"})
_EXIT_PATTERNS = (
    re.compile(r"Move through the (?P<direction>north|south|east|west|up|down) exit\.?", re.I),
    re.compile(r"Enter the room through the (?P<direction>north|south|east|west|up|down) exit\.?", re.I),
    re.compile(r"Take the (?P<direction>north|south|east|west|up|down) exit back to the previous room\.?", re.I),
    re.compile(r"Climb through the (?P<direction>north|south|east|west|up|down) exit, if one exists\.?", re.I),
)
_TAKE_PATTERN = re.compile(r"Pick up the (?P<item>.+?) (?:visible in|in) this room\.?", re.I)
_DROP_PATTERN = re.compile(r"Drop the carried (?P<item>.+?) here\.?", re.I)


def _field(lines: str, prefix: str) -> str | None:
    for line in lines.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _room_fields(room: str) -> tuple[str | None, str | None]:
    """Ignore item/exit-looking text inside descriptions before actual exits.

    The current engine renders an authoritative `Exits:` line *after* its room
    description, then an optional `Items:` line. A description can contain
    attacker-written newline text, so using its first apparent marker would
    be an avoidable prompt-injection bug. This is still a text-protocol
    heuristic, not a cryptographic proof of game state: structured observation
    fields from the trusted host would be stronger.
    """
    lines = room.splitlines()
    exits = [i for i, line in enumerate(lines) if line.startswith("Exits: ")]
    if not exits:
        return None, None
    canonical = exits[-1]
    items = [line[7:] for line in lines[canonical + 1:] if line.startswith("Items: ")]
    return lines[canonical][7:], items[-1] if items else None


def _names(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip() or raw.casefold() in {"none", "nothing"}:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _grounded_name(requested: str, available: tuple[str, ...]) -> str | None:
    matches = [name for name in available if name.casefold() == requested.casefold()]
    return matches[0] if len(matches) == 1 else None


def decide(observation: Mapping[str, object]) -> dict[str, object]:
    """Select one allowed command, or abstain, without seeing target labels.

    An observation must supply the engine-rendered room, inventory, objective,
    and host-provided allowed verbs. Any malformed or out-of-template objective
    produces abstention rather than guessed access to hidden game state.
    """
    abstain: dict[str, object] = {"decision": "abstain", "command": None}
    if not isinstance(observation, Mapping) or set(observation) != {
        "room", "inventory", "objective", "allowed_verbs"
    }:
        return abstain
    room = observation["room"]
    inventory = observation["inventory"]
    objective = observation["objective"]
    allow = observation["allowed_verbs"]
    if (type(room) is not str or type(inventory) is not str
            or type(objective) is not str or type(allow) is not list
            or any(type(value) is not str for value in allow)
            or len(objective) > 512 or "\n" in objective or "\r" in objective):
        return abstain
    allowed = set(allow)

    command = None
    if re.fullmatch(r"Describe your current room without moving\.?", objective, re.I):
        command = "look"
    elif re.fullmatch(r"Check what you are carrying\.?", objective, re.I):
        command = "inventory"
    else:
        exits_raw, items_raw = _room_fields(room)
        exits = tuple(direction for direction in _names(exits_raw)
                      if direction in _DIRECTIONS)
        for pattern in _EXIT_PATTERNS:
            match = pattern.fullmatch(objective)
            if match is not None:
                direction = match.group("direction").casefold()
                if direction in exits:
                    command = f"go {direction}"
                break
        if command is None and not any(p.fullmatch(objective) for p in _EXIT_PATTERNS):
            match = _TAKE_PATTERN.fullmatch(objective)
            if match is not None:
                item = _grounded_name(match.group("item"), _names(items_raw))
                if item is not None:
                    command = f"take {item}"
            else:
                match = _DROP_PATTERN.fullmatch(objective)
                if match is not None:
                    item = _grounded_name(match.group("item"),
                                          _names(_field(inventory, "You carry: ")))
                    if item is not None:
                        command = f"drop {item}"

    if command is None or command.split(" ", 1)[0] not in allowed:
        return abstain
    return {"decision": "act", "command": command}


def predict_record(record: Mapping[str, object]) -> dict[str, object]:
    """Preserve only ID and player-visible input; discard all result metadata."""
    if not isinstance(record, Mapping) or not isinstance(record.get("id"), str):
        raise ValueError("Prediction requires a string case ID")
    return {"id": record["id"], **decide(record.get("input"))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True,
                        help="Input-only JSONL, without labels or database state")
    parser.add_argument("--predictions", type=Path, required=True,
                        help="New JSONL path for predictions")
    args = parser.parse_args()
    if args.predictions.exists():
        parser.error("Refusing to overwrite predictions")
    if args.inputs.resolve() == args.predictions.resolve():
        parser.error("Inputs and predictions must differ")
    rows = [json.loads(line) for line in args.inputs.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    predictions = [predict_record(row) for row in rows]
    if len(set(p["id"] for p in predictions)) != len(predictions):
        parser.error("Input case IDs must be unique")
    with args.predictions.open("x", encoding="utf-8") as out:
        for prediction in predictions:
            out.write(json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"cases": len(predictions),
                      "acted": sum(p["decision"] == "act" for p in predictions)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
