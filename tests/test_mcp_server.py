"""Real SQLite and stdio protocol tests for the bounded local REALM plugin."""

from __future__ import annotations

from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from realm.gateway import Gateway
from realm.mcp_server import MAX_RESULT_BYTES, RealmMCPBridge, RealmMCPError, TOOLS, dispatch, serve_stdio


ROOT = Path(__file__).resolve().parents[1]
ALL_TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)
FULL_TOOL_ENV = ",".join(tool["name"] for tool in TOOLS)
READ_ONLY_TOOL_NAMES = {"realm_status", "realm_look"}


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "realm.sqlite3"
        self.host = Gateway(self.database, world_id="sandbox")
        self.token = self.host.credentials.provision("Vera")
        # Ordinary engine coverage runs with explicit trusted local authority;
        # production stdio must never get this authority implicitly.
        self.bridge = RealmMCPBridge(str(self.database), "sandbox", self.token,
                                     allowed_tools=ALL_TOOL_NAMES)

    def test_tool_registry_and_fixed_principal_without_player_argument(self):
        tools = {tool["name"]: tool for tool in TOOLS}
        self.assertEqual(set(tools), {
            "realm_status", "realm_join", "realm_look", "realm_command",
            "realm_say", "realm_feed", "realm_map", "realm_move", "realm_inventory",
        })
        for tool in tools.values():
            self.assertNotIn("player", tool["inputSchema"]["properties"])
        self.assertEqual(self.bridge.call("realm_status", {}), {
            "ok": True, "world_id": "sandbox", "player": "Vera",
        })
        with self.assertRaises(RealmMCPError):
            self.bridge.call("realm_join", {"player": "Patrick"})

    def test_first_join_look_movement_and_persistence(self):
        self.assertIn("Welcome, Vera", self.bridge.call("realm_join", {})["text"])
        self.assertIn("The Atrium", self.bridge.call("realm_look", {})["text"])
        move = self.bridge.call("realm_command", {"command": "north", "action_id": "move-one"})
        self.assertIn("Corridor", move["text"])
        reconnected = RealmMCPBridge(str(self.database), "sandbox", self.token,
                                     allowed_tools=ALL_TOOL_NAMES)
        self.assertIn("Corridor", reconnected.call("realm_look", {})["text"])

    def test_typed_inventory_and_move_replay_do_not_duplicate_world_actions(self):
        self.bridge.call("realm_join", {})
        self.assertIn("nothing", self.bridge.call("realm_inventory", {})["text"].lower())
        self.bridge.call("realm_command", {"command": "take token", "action_id": "take-token"})
        self.assertIn("token", self.bridge.call("realm_inventory", {})["text"].lower())
        args = {"direction": "north", "action_id": "typed-north"}
        first = self.bridge.call("realm_move", args)
        self.assertIn("Corridor", first["text"])
        self.assertEqual(first, self.bridge.call("realm_move", args))
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='move'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "Action ID"):
            self.bridge.call("realm_move", {"direction": "south", "action_id": "typed-north"})

    def test_typed_move_fails_closed_on_non_directions_and_unknown_fields(self):
        self.bridge.call("realm_join", {})
        for arguments in ({"direction": "north"},
                          {"direction": "teleport", "action_id": "a"},
                          {"direction": "north; say hi", "action_id": "a"},
                          {"direction": "north", "action_id": "a", "player": "Patrick"}):
            with self.subTest(arguments=arguments), self.assertRaises(RealmMCPError):
                self.bridge.call("realm_move", arguments)
        with self.assertRaises(RealmMCPError):
            self.bridge.call("realm_inventory", {"player": "Patrick"})

    def test_action_id_retry_and_conflicting_reuse(self):
        self.bridge.call("realm_join", {})
        args = {"command": "north", "action_id": "move-1"}
        first = self.bridge.call("realm_command", args)
        self.assertEqual(first, self.bridge.call("realm_command", args))
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT room_id FROM players WHERE id='Vera'").fetchone()[0], "corridor")
        with self.assertRaisesRegex(ValueError, "Action ID"):
            self.bridge.call("realm_command", {"command": "south", "action_id": "move-1"})

    def test_chat_is_separate_durable_tool_and_feed_is_scoped(self):
        self.bridge.call("realm_join", {})
        said = self.bridge.call("realm_say", {"message": "Hello, world!", "action_id": "say-1"})
        self.assertTrue(said["ok"])
        events = self.bridge.call("realm_feed", {"after": 0})["events"]
        self.assertTrue(any(event["text"] == "Vera: Hello, world!" for event in events))
        with self.assertRaises(RealmMCPError):
            self.bridge.call("realm_say", {"message": "bad\nlook", "action_id": "say-2"})
        with self.assertRaises(RealmMCPError):
            self.bridge.call("realm_say", {"message": "hello", "action_id": "say-1", "player": "Patrick"})

    def test_rejected_missing_or_unsafe_arguments(self):
        self.bridge.call("realm_join", {})
        for arguments in (
            {"command": "north"},
            {"command": "north", "action_id": ""},
            {"command": "north", "action_id": "x", "player": "Patrick"},
            {"command": "", "action_id": "x"},
            {"command": "north", "action_id": "bad\nnonce"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(RealmMCPError):
                self.bridge.call("realm_command", arguments)
        for speech in ("say hello", " SAY hello", "say\thello"):
            with self.subTest(speech=speech), self.assertRaisesRegex(RealmMCPError, "realm_say"):
                self.bridge.call("realm_command", {"command": speech, "action_id": "speech-bypass"})
        for after in (True, -1, "0", 2**64):
            with self.subTest(after=after), self.assertRaises(RealmMCPError):
                self.bridge.call("realm_feed", {"after": after})

    def test_wrong_world_refuses_without_rebinding_state(self):
        with self.assertRaisesRegex(RealmMCPError, "does not match"):
            RealmMCPBridge(str(self.database), "otherworld", self.token)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0], "sandbox")

    def test_missing_world_does_not_seed_demo_file(self):
        missing = self.directory / "missing.sqlite3"
        with self.assertRaises(RealmMCPError):
            RealmMCPBridge(str(missing), "sandbox", self.token)
        self.assertFalse(missing.exists())

    def test_rejected_partial_world_never_gets_initialized(self):
        partial = self.directory / "partial.sqlite3"
        with closing(sqlite3.connect(partial)) as db, db:
            db.execute("CREATE TABLE world_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO world_meta VALUES ('world_id','sandbox')")
            before = db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
        with self.assertRaisesRegex(RealmMCPError, "incomplete or incompatible"):
            RealmMCPBridge(str(partial), "sandbox", self.token)
        with closing(sqlite3.connect(partial)) as db, db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall(), before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM world_meta").fetchone()[0], 1)

    def test_rejected_existing_world_does_not_recreate_missing_tables(self):
        self.host.credentials.revoke("Vera")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TABLE geo_places")
            before = db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
        with self.assertRaisesRegex(RealmMCPError, "incomplete or incompatible"):
            RealmMCPBridge(str(self.database), "sandbox", self.token)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall(), before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 3)

    def test_wrong_token_does_not_change_qualified_world(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            before = db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
        with self.assertRaisesRegex(RealmMCPError, "invalid or revoked"):
            RealmMCPBridge(str(self.database), "sandbox", "x" * 32)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall(), before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)

    def test_valid_token_cannot_seed_empty_existing_room_table(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM rooms")
            before = db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        with self.assertRaisesRegex(RealmMCPError, "no authored or seeded rooms"):
            RealmMCPBridge(str(self.database), "sandbox", self.token)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], before)

    def test_revoke_takes_effect_without_restart_and_never_leaks_token(self):
        self.assertEqual(self.bridge.call("realm_status", {})["player"], "Vera")
        self.host.credentials.revoke("Vera")
        with self.assertRaisesRegex(RealmMCPError, "revoked") as caught:
            self.bridge.call("realm_status", {})
        self.assertNotIn(self.token, str(caught.exception))

    def test_rebinding_world_invalidates_live_bridge_before_effect(self):
        self.bridge.call("realm_join", {})
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE world_meta SET value='otherworld' WHERE key='world_id'")
        with self.assertRaisesRegex(RealmMCPError, "does not match"):
            self.bridge.call("realm_move", {"direction": "north", "action_id": "invalid-world-move"})
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT room_id FROM players WHERE id='Vera'").fetchone()[0], "atrium")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 0)

    def test_rebinding_same_token_to_different_player_cannot_change_live_identity(self):
        self.assertEqual(self.bridge.call("realm_status", {})["player"], "Vera")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE player_credentials SET player_name='Patrick' WHERE player_name='Vera'")
        with self.assertRaisesRegex(RealmMCPError, "identity changed"):
            self.bridge.call("realm_status", {})
        with self.assertRaisesRegex(RealmMCPError, "identity changed"):
            self.bridge.call("realm_join", {})
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)

    def test_overlong_or_nonprintable_speech_rejected_without_receipt(self):
        self.bridge.call("realm_join", {})
        for message in ("x" * 281, "hello\tworld", "hello\nworld", " hello", "hello "):
            with self.subTest(message=repr(message)), self.assertRaises(RealmMCPError):
                self.bridge.call("realm_say", {"message": message, "action_id": "bad-speech"})
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 0)

    def test_mcp_initialize_list_and_tool_call_on_stdin(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "realm_join", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "realm_command", "arguments": {"command": "north", "action_id": "move-stdio"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "realm_look", "arguments": {}}},
        ]
        env = dict(os.environ)
        env.update({"REALM_DB_PATH": str(self.database), "REALM_WORLD_ID": "sandbox",
                    "REALM_PLAYER_TOKEN": self.token, "PYTHONPATH": str(ROOT / "src"),
                    "REALM_MCP_ALLOWED_TOOLS": FULL_TOOL_ENV})
        result = subprocess.run(
            [sys.executable, "-m", "realm.mcp_server"],
            input="".join(json.dumps(item) + "\n" for item in requests),
            capture_output=True, text=True, env=env, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4, 5])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(responses[1]["result"]["tools"]), 9)
        self.assertIn("Welcome, Vera", responses[2]["result"]["content"][0]["text"])
        self.assertIn("Corridor", responses[4]["result"]["content"][0]["text"])
        self.assertNotIn("error", responses[3])

    def test_tools_call_rejects_actor_forgery_in_model_arguments(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "realm_join", "arguments": {"player": "Patrick"}}}
        answer = dispatch(request, self.bridge)
        self.assertTrue(answer["result"]["isError"])
        self.assertNotIn(self.token, json.dumps(answer))

    def test_large_game_results_are_bounded_by_actual_stdio_wire_size(self):
        request = {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                   "params": {"name": "realm_look", "arguments": {}}}
        crowded_feed = {"ok": True, "events": [
            {"id": i, "kind": "say", "actor": "Vera", "room": "atrium",
             "at": "2026-11-07T12:00:00Z", "text": "Vera: " + "x" * 280}
            for i in range(1, 101)], "cursor": 100}
        scenarios = {"ascii room": {"ok": True, "text": "x" * 40000},
                     "unicode room": {"ok": True, "text": "😀" * 11000},
                     "crowded feed": crowded_feed}
        for label, response in scenarios.items():
            with self.subTest(case=label), patch.object(
                    self.bridge, "call", return_value=response):
                # The structured response is copied into BOTH text and
                # structuredContent, and stdio escapes Unicode to ASCII.
                reply = dispatch(request, self.bridge)
                self.assertTrue(reply["result"]["isError"])
                wire = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
                self.assertLessEqual(len(wire), MAX_RESULT_BYTES)
                output = io.StringIO()
                serve_stdio(self.bridge, io.BytesIO((json.dumps(request) + "\n").encode()), output)
                self.assertLessEqual(len(output.getvalue().encode("utf-8")), MAX_RESULT_BYTES + 1)
                self.assertTrue(json.loads(output.getvalue())["result"]["isError"])
        with patch.object(self.bridge, "call", return_value={"ok": True, "text": "A small room."}):
            small = dispatch(request, self.bridge)["result"]
            self.assertFalse(small["isError"])
            self.assertEqual(small["structuredContent"]["text"], "A small room.")

    def test_typed_play_calls_work_over_real_stdio_subprocess(self):
        calls = [
            ("realm_join", {}),
            ("realm_inventory", {}),
            ("realm_move", {"direction": "north", "action_id": "stdio-north"}),
            ("realm_move", {"direction": "north", "action_id": "stdio-north"}),
            ("realm_say", {"message": "I made it.", "action_id": "stdio-say"}),
            ("realm_feed", {"after": 0}),
        ]
        requests = [{"jsonrpc": "2.0", "id": index,
                     "method": "tools/call", "params": {"name": name, "arguments": arguments}}
                    for index, (name, arguments) in enumerate(calls, start=1)]
        env = dict(os.environ)
        env.update({"REALM_DB_PATH": str(self.database), "REALM_WORLD_ID": "sandbox",
                    "REALM_PLAYER_TOKEN": self.token, "PYTHONPATH": str(ROOT / "src"),
                    "REALM_MCP_ALLOWED_TOOLS": FULL_TOOL_ENV})
        result = subprocess.run([sys.executable, "-m", "realm.mcp_server"],
                                input="".join(json.dumps(item) + "\n" for item in requests),
                                capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.token, result.stdout + result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([item["id"] for item in replies], list(range(1, 7)))
        self.assertTrue(all(not item["result"]["isError"] for item in replies))
        self.assertEqual(replies[2]["result"]["structuredContent"],
                         replies[3]["result"]["structuredContent"])
        self.assertIn("I made it.", replies[5]["result"]["content"][0]["text"])
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='move'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='say'").fetchone()[0], 1)

    def test_duplicate_json_keys_are_rejected_before_action(self):
        raw = (b'{"jsonrpc":"2.0","id":7,"method":"tools/call",'
               b'"params":{"name":"realm_join","name":"realm_command",'
               b'"arguments":{"command":"north","action_id":"x"}}}\n')
        destination = io.StringIO()
        serve_stdio(self.bridge, io.BytesIO(raw), destination)
        response = json.loads(destination.getvalue())
        self.assertEqual(response["error"]["code"], -32700)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)

    def test_deep_json_parser_recursion_cannot_crash_stdio_session(self):
        # A deeply nested input is below the line limit but triggers Python's
        # JSON recursion guard. It must not terminate the tool session.
        nested = b"[" * 10000 + b"0" + b"]" * 10000 + b"\n"
        ping = b'{"jsonrpc":"2.0","id":9,"method":"ping"}\n'
        self.assertLess(len(nested), 32768)
        output = io.StringIO()
        serve_stdio(self.bridge, io.BytesIO(nested + ping), output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        # Python's JSON decoder may reject this by depth (-32700), or
        # successfully decode a deeply nested non-request value (-32600).
        # The contract is rejection without terminating the subsequent ping.
        self.assertIn(rows[0]["error"]["code"], (-32700, -32600))
        self.assertEqual(rows[1]["id"], 9)
        self.assertEqual(rows[1]["result"], {})

    def test_escaped_unpaired_surrogate_id_cannot_crash_utf8_writer(self):
        malformed_id = b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
        ping = b'{"jsonrpc":"2.0","id":10,"method":"ping"}\n'
        wire = io.BytesIO()
        destination = io.TextIOWrapper(wire, encoding="utf-8", errors="strict", write_through=True)
        serve_stdio(self.bridge, io.BytesIO(malformed_id + ping), destination)
        rows = [json.loads(line) for line in wire.getvalue().decode("utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["result"], {})
        self.assertEqual(rows[1]["id"], 10)
        self.assertEqual(rows[1]["result"], {})

    def test_large_unicode_request_id_cannot_amplify_stdio_reply(self):
        oversized = {"jsonrpc": "2.0", "id": "😀" * 7000, "method": "ping"}
        raw = (json.dumps(oversized, ensure_ascii=False) + "\n").encode("utf-8")
        self.assertLess(len(raw), 32768)
        ping = b'{"jsonrpc":"2.0","id":22,"method":"ping"}\n'
        output = io.StringIO()
        serve_stdio(self.bridge, io.BytesIO(raw + ping), output)
        lines = output.getvalue().splitlines(keepends=True)
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(max(len(line.encode("utf-8")) for line in lines), MAX_RESULT_BYTES + 1)
        rows = [json.loads(line) for line in lines]
        self.assertEqual(rows[0]["error"]["code"], -32600)
        self.assertIsNone(rows[0]["id"])
        self.assertEqual(rows[1]["id"], 22)
        self.assertEqual(rows[1]["result"], {})

    def test_malformed_request_identifiers_and_protocol_version_fail_safely(self):
        self.assertEqual(dispatch({"jsonrpc": "2.0", "id": True,
                                   "method": "tools/list"}, self.bridge)["error"]["code"], -32600)
        self.assertIsNone(dispatch({"jsonrpc": "2.0", "id": True,
                                    "method": "tools/list"}, self.bridge)["id"])
        self.assertEqual(dispatch({"jsonrpc": "2.0", "id": None,
                                   "method": "ping"}, self.bridge)["result"], {})
        bad_version = dispatch({"jsonrpc": "2.0", "id": "hello", "method": "initialize",
                                "params": {"protocolVersion": ["unsupported"]}}, self.bridge)
        self.assertEqual(bad_version["result"]["protocolVersion"], "2025-06-18")

    def test_direct_bridge_defaults_to_read_only_and_cannot_implicitly_join(self):
        restricted = RealmMCPBridge(str(self.database), "sandbox", self.token)
        listed = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, restricted)
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]],
                         ["realm_status", "realm_look"])
        self.assertEqual(restricted.call("realm_status", {})["player"], "Vera")
        with self.assertRaisesRegex(RealmMCPError, "not permitted"):
            restricted.call("realm_join", {})
        reply = dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "realm_look", "arguments": {}}}, restricted)
        self.assertTrue(reply["result"]["isError"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 0)

    def test_host_allowlist_hides_and_denies_all_seven_tools_even_by_direct_call(self):
        self.bridge.call("realm_join", {})
        restricted = RealmMCPBridge(str(self.database), "sandbox", self.token)
        denied = {
            "realm_join": {},
            "realm_inventory": {},
            "realm_move": {"direction": "north", "action_id": "denied-move"},
            "realm_command": {"command": "take token", "action_id": "denied-command"},
            "realm_say": {"message": "Unauthorized speech", "action_id": "denied-say"},
            "realm_feed": {"after": 0},
            "realm_map": {},
        }
        self.assertEqual(set(denied), ALL_TOOL_NAMES - READ_ONLY_TOOL_NAMES)
        with closing(sqlite3.connect(self.database)) as db:
            before = tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                           for table in ("players", "events", "action_receipts"))
        for index, (name, arguments) in enumerate(denied.items(), start=1):
            with self.subTest(tool=name):
                # Advertising is not the security boundary: explicitly bypass
                # tools/list and invoke every hidden tool by its real name.
                request = {"jsonrpc": "2.0", "id": index, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments,
                                      "_meta": {"allowed_tools": list(ALL_TOOL_NAMES)}}}
                response = dispatch(request, restricted)
                self.assertTrue(response["result"]["isError"])
                self.assertIn("not permitted", response["result"]["content"][0]["text"])
                self.assertNotIn("structuredContent", response["result"])
                with self.assertRaisesRegex(RealmMCPError, "not permitted"):
                    restricted.call(name, arguments)
        with closing(sqlite3.connect(self.database)) as db:
            after = tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                          for table in ("players", "events", "action_receipts"))
        self.assertEqual(after, before)
        self.assertIn("Atrium", restricted.call("realm_look", {})["text"])

    def test_real_stdio_read_only_allowlist_blocks_direct_hidden_tool_call(self):
        self.bridge.call("realm_join", {})
        denied = ["realm_join", "realm_inventory", "realm_move", "realm_command",
                  "realm_say", "realm_feed", "realm_map"]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
             "params": {"allowed_tools": list(ALL_TOOL_NAMES)}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "realm_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "realm_look", "arguments": {}}},
        ]
        requests.extend({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                         "params": {"name": name, "arguments": {}}}
                        for index, name in enumerate(denied, start=4))
        env = dict(os.environ)
        env.update({"REALM_DB_PATH": str(self.database), "REALM_WORLD_ID": "sandbox",
                    "REALM_PLAYER_TOKEN": self.token, "PYTHONPATH": str(ROOT / "src"),
                    "REALM_MCP_ALLOWED_TOOLS": "realm_status,realm_look"})
        with closing(sqlite3.connect(self.database)) as db:
            before = tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                           for table in ("players", "events", "action_receipts"))
        process = subprocess.run([sys.executable, "-m", "realm.mcp_server"],
                                 input="".join(json.dumps(item) + "\n" for item in requests),
                                 capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn(self.token, process.stdout + process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 10)
        self.assertEqual([tool["name"] for tool in responses[0]["result"]["tools"]],
                         ["realm_status", "realm_look"])
        self.assertEqual(responses[1]["result"]["structuredContent"]["player"], "Vera")
        self.assertIn("Atrium", responses[2]["result"]["content"][0]["text"])
        for name, response in zip(denied, responses[3:]):
            with self.subTest(tool=name):
                self.assertTrue(response["result"]["isError"])
                self.assertIn("not permitted", response["result"]["content"][0]["text"])
        with closing(sqlite3.connect(self.database)) as db:
            after = tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                          for table in ("players", "events", "action_receipts"))
        self.assertEqual(after, before)

    def test_missing_empty_or_malformed_host_allowlist_refuses_stdio_startup(self):
        for allowlist in (None, "", "realm_status,", ",realm_look", "realm_status,,realm_look",
                          "realm_status, realm_look", "realm_status,realm_status",
                          "realm_status,not_a_tool", "*", "realm_status\nrealm_look"):
            with self.subTest(allowlist=allowlist):
                env = dict(os.environ)
                env.update({"REALM_DB_PATH": str(self.database), "REALM_WORLD_ID": "sandbox",
                            "REALM_PLAYER_TOKEN": self.token, "PYTHONPATH": str(ROOT / "src")})
                env.pop("REALM_MCP_ALLOWED_TOOLS", None)
                if allowlist is not None:
                    env["REALM_MCP_ALLOWED_TOOLS"] = allowlist
                process = subprocess.run([sys.executable, "-m", "realm.mcp_server"],
                                         input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                                         capture_output=True, text=True, env=env, timeout=15)
                self.assertEqual(process.returncode, 2, process.stderr)
                self.assertEqual(process.stdout, "")
                self.assertNotIn(self.token, process.stderr)
                self.assertIn("explicit host tool allowlist", process.stderr)

    def test_unconfigured_subprocess_fails_closed(self):
        env = dict(os.environ)
        for name in ("REALM_DB_PATH", "REALM_WORLD_ID", "REALM_PLAYER_TOKEN",
                     "REALM_MCP_ALLOWED_TOOLS"):
            env.pop(name, None)
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run([sys.executable, "-m", "realm.mcp_server"],
                                input="", capture_output=True, text=True,
                                env=env, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("requires an existing bound world", result.stderr)


if __name__ == "__main__":
    unittest.main()
