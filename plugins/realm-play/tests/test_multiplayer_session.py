"""Bounded two-character play through the real, local plugin stdio launcher.

This is a deterministic gameplay integration test, NOT an AI reasoning or
autonomous-learning evaluation. It exercises 21 distinct committed player
actions with interleaved character sessions and process restarts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


PLUGIN_REPO = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ.get("REALM_PR52_ROOT", str(PLUGIN_REPO))).resolve()
LAUNCHER = PLUGIN_REPO / "plugins" / "realm-play" / "server" / "realm_mcp.py"
BACKEND_SOURCE = BACKEND_ROOT / "src" / "realm" / "mcp_server.py"

if BACKEND_SOURCE.is_file():
    sys.path.insert(0, str(BACKEND_ROOT / "src"))
    from realm.gateway import Gateway
else:
    Gateway = None


def rpc(identifier: int, method: str, params: dict | None = None) -> dict:
    message = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        message["params"] = params
    return message


def tool_call(identifier: int, tool: str, arguments: dict) -> dict:
    return rpc(identifier, "tools/call", {"name": tool, "arguments": arguments})


def take(actor: str, sequence: int) -> tuple[str, dict, str]:
    return ("realm_command", {"command": "take token", "action_id": f"{actor}-{sequence:02}"}, "You take")


def drop(actor: str, sequence: int) -> tuple[str, dict, str]:
    return ("realm_command", {"command": "drop token", "action_id": f"{actor}-{sequence:02}"}, "You drop")


def move(actor: str, sequence: int, direction: str) -> tuple[str, dict, str]:
    return ("realm_move", {"direction": direction, "action_id": f"{actor}-{sequence:02}"},
            "Corridor" if direction == "north" else "Atrium")


def say(actor: str, sequence: int, message: str) -> tuple[str, dict, str]:
    return ("realm_say", {"message": message, "action_id": f"{actor}-{sequence:02}"}, "You say")


class TwoPlayerSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Gateway is None:
            raise RuntimeError(
                "REALM MCP backend missing: set REALM_PR52_ROOT to PR #52 checkout; "
                "two-player integration must not be silently skipped."
            )

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="realm-two-player-session-")
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "world.sqlite3"
        host = Gateway(self.database, world_id="two_player_test")
        self.tokens = {name: host.credentials.provision(name) for name in ("Moss", "Ember")}

    def _process(self, actor: str, steps: list[tuple[str, dict, str]], *, join: bool = False):
        requests = [rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
                    tool_call(2, "realm_status", {}),
                    tool_call(3, "realm_look", {})]
        if join:
            # A join must precede room observation for a newly provisioned
            # character, but not for a resumed, previously joined character.
            requests[2] = tool_call(3, "realm_join", {})
            requests.append(tool_call(4, "realm_look", {}))
        first_step = len(requests) + 1
        requests.extend(tool_call(first_step + index, tool, args)
                        for index, (tool, args, _) in enumerate(steps))

        environment = os.environ.copy()
        for key in ("REALM_DB", "REALM_ACTOR", "PYTHONPATH", "REALM_PLAYER_TOKEN"):
            environment.pop(key, None)
        environment.update({
            "REALM_ROOT": str(BACKEND_ROOT),
            "REALM_DB_PATH": str(self.database),
            "REALM_WORLD_ID": "two_player_test",
            "REALM_PLAYER_TOKEN": self.tokens[actor],
        })
        completed = subprocess.run(
            [sys.executable, "-B", str(LAUNCHER)],
            input="".join(json.dumps(item) + "\n" for item in requests),
            capture_output=True, text=True, env=environment,
            cwd=PLUGIN_REPO, timeout=20, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn(self.tokens[actor], completed.stdout + completed.stderr)
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(requests), len(replies))
        self.assertEqual("realm-local", replies[0]["result"]["serverInfo"]["name"])
        status = replies[1]["result"]["structuredContent"]
        self.assertEqual("two_player_test", status["world_id"])
        self.assertEqual(actor, status["player"])
        if join:
            self.assertFalse(replies[2]["result"]["isError"])
        self.assertIn("Atrium", replies[3 if join else 2]["result"]["content"][0]["text"])
        for (tool, args, fragment), response in zip(steps, replies[-len(steps):]):
            result = response["result"]
            self.assertFalse(result["isError"], (actor, tool, args, result))
            self.assertIn(fragment, result["content"][0]["text"])
        return replies

    def _count(self, table: str, kind: str | None = None) -> int:
        with sqlite3.connect(self.database) as database:
            if kind is None:
                return int(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return int(database.execute(
                "SELECT COUNT(*) FROM events WHERE kind=?", (kind,)
            ).fetchone()[0])

    def test_twenty_one_actions_two_players_and_replayed_intents_after_restart(self):
        # Four separate subprocess sessions use two host-bound player tokens.
        # Each action ID denotes one logical intent, never a tool-call nonce.
        moss_first = [
            take("moss", 1), say("moss", 2, "I found a brass token."),
            move("moss", 3, "north"), say("moss", 4, "The corridor is clear."),
            move("moss", 5, "south"), drop("moss", 6),
        ]
        ember_first = [
            take("ember", 1), say("ember", 2, "I'll carry it for now."),
            move("ember", 3, "north"), move("ember", 4, "south"),
            drop("ember", 5),
        ]
        moss_second = [
            take("moss", 7), say("moss", 8, "Back to the atrium."),
            move("moss", 9, "north"), move("moss", 10, "south"),
            drop("moss", 11),
        ]
        ember_second = [
            take("ember", 6), say("ember", 7, "Your turn next."),
            move("ember", 8, "north"), move("ember", 9, "south"),
            drop("ember", 10),
        ]

        self._process("Moss", moss_first, join=True)
        self._process("Ember", ember_first, join=True)

        # The original move response was notionally lost. The actor is now
        # back in the atrium; replaying a previous north intent must NOT move.
        receipts_before = self._count("action_receipts")
        moves_before = self._count("events", "move")
        self._process("Moss", [move("moss", 3, "north")])
        self.assertEqual(receipts_before, self._count("action_receipts"))
        self.assertEqual(moves_before, self._count("events", "move"))

        self._process("Moss", moss_second)
        self._process("Ember", ember_second)

        # A second player resumes in another process and retries speech.
        receipts_before = self._count("action_receipts")
        speech_before = self._count("events", "say")
        self._process("Ember", [say("ember", 2, "I'll carry it for now.")])
        self.assertEqual(receipts_before, self._count("action_receipts"))
        self.assertEqual(speech_before, self._count("events", "say"))

        self.assertEqual(21, self._count("action_receipts"))
        self.assertEqual(4, self._count("events", "take"))
        self.assertEqual(4, self._count("events", "drop"))
        self.assertEqual(8, self._count("events", "move"))
        self.assertEqual(8, self._count("events", "leave"))
        self.assertEqual(5, self._count("events", "say"))
        with sqlite3.connect(self.database) as database:
            position = dict(database.execute("SELECT id,room_id FROM players").fetchall())
            self.assertEqual({"Moss": "atrium", "Ember": "atrium"}, position)
            held_by, room_id = database.execute(
                "SELECT held_by,room_id FROM items WHERE id='token'"
            ).fetchone()
            self.assertIsNone(held_by)
            self.assertEqual("atrium", room_id)


if __name__ == "__main__":
    unittest.main()
