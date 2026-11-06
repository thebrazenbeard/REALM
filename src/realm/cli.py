"""Terminal interface to an isolated, local REALM database."""

from __future__ import annotations

import argparse
from pathlib import Path

from .engine import Engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REALM local-first playable prototype")
    parser.add_argument("--db", type=Path, default=Path("realm.sqlite3"), help="SQLite file for this isolated world")
    parser.add_argument("--player", required=True, help="Local player name")
    parser.add_argument("--once", help="Run one command and exit (useful for smoke tests)")
    args = parser.parse_args(argv)
    engine = Engine(args.db)
    try:
        greeting = engine.join(args.player)
    except ValueError as exc:
        parser.error(str(exc))
    if args.once is not None:
        print(engine.act(args.player, args.once))
        return 0
    print(greeting)
    while True:
        try:
            cmd = input("REALM> ")
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye. Your position is saved.")
            break
        print(engine.act(args.player, cmd))
        if cmd.strip().casefold() in {"quit", "exit"}:
            break
    return 0
