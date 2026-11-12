"""Stock loopback CLI explicitly activates safe PvP without custom Python wiring."""
from __future__ import annotations

import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from realm.auth import CredentialStore
from realm.engine import Engine
from realm.gateway import Gateway, GatewayServer, main as gateway_main
from realm.telnet import LocalTelnetServer, main as telnet_main


class HostOptInCliTests(unittest.TestCase):
    def test_unbound_shared_token_presence_flags_fail_before_listening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.sqlite3"
            with self.assertRaises(SystemExit) as ex:
                gateway_main([
                    "--db", str(path), "--world-id", "cli", "--port", "0",
                    "--enable-local-presence",
                ])
            self.assertEqual(2, ex.exception.code)
            with self.assertRaises(SystemExit) as ex:
                telnet_main([
                    "--db", str(path), "--port", "0",
                    "--enable-local-presence",
                ])
            self.assertEqual(2, ex.exception.code)
            with self.assertRaises(SystemExit) as ex:
                gateway_main([
                    "--db", str(path), "--world-id", "cli",
                    "--install-pvp-policy",
                ])
            self.assertEqual(2, ex.exception.code)
            for extra in (
                ["--scoped-credentials", "--pvp-safe-room", "atrium"],
                ["--scoped-credentials", "--pvp-announcement-style", "black_bell"],
                ["--scoped-credentials", "--install-pvp-policy",
                 "--pvp-announcement-style", "unknown-style"],
            ):
                with self.subTest(extra=extra), self.assertRaises(SystemExit) as error:
                    gateway_main(["--db", str(path), "--world-id", "cli", *extra])
                self.assertEqual(2, error.exception.code)
            self.assertFalse(path.exists(), "Invalid flags cannot mutate the world")
            with self.assertRaises(SystemExit) as ex:
                telnet_main([
                    "--db", str(path), "--port", "0",
                    "--player-credentials", "--enable-local-presence",
                ])
            self.assertEqual(2, ex.exception.code)
            self.assertFalse(path.exists(),
                             "Unbound player-token presence cannot create an implicit world")

    def test_host_selected_safe_room_and_announcement_are_one_time_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.sqlite3"
            setup = Gateway(path, world_id="pvp_safe", scoped_credentials=True)
            args = ["--db", str(path), "--world-id", "pvp_safe",
                    "--scoped-credentials", "--install-pvp-policy"]
            with self.assertRaisesRegex(ValueError, "Safe room"):
                gateway_main([*args, "--pvp-safe-room", "not-a-real-room"])
            with self.assertRaisesRegex(ValueError, "distinct room"):
                gateway_main([*args, "--pvp-safe-room", "atrium",
                              "--pvp-safe-room", "atrium"])
            with closing(sqlite3.connect(path)) as db:
                self.assertIsNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name='combat_policy'"
                ).fetchone(), "Rejected policy must not leave combat tables")

            self.assertEqual(0, gateway_main([
                *args, "--pvp-safe-room", "atrium",
                "--pvp-announcement-style", "black_bell",
            ]))
            with closing(sqlite3.connect(path)) as db:
                policy = db.execute(
                    "SELECT safe_rooms_json,announcement_style,allow_offline_targets "
                    "FROM combat_policy"
                ).fetchone()
            self.assertEqual((json.dumps(["atrium"]), "black_bell", 0), policy)
            with self.assertRaisesRegex(ValueError, "already installed"):
                gateway_main([*args, "--pvp-safe-room", "atrium"])

            for player in ("Ada", "Bob"):
                setup.engine.join(player)
            tokens = {player: setup.scoped_credentials.provision(player)
                      for player in ("Ada", "Bob")}
            def act(player, command, action_id):
                return setup.command_remote(player, command, action_id, tokens[player])["text"]
            act("Ada", "pvp on", "a-on")
            act("Bob", "pvp on", "b-on")
            with self.assertRaisesRegex(ValueError, "safe zone"):
                act("Ada", "attack Bob", "safe-denied")
            act("Ada", "north", "a-north")
            act("Bob", "north", "b-north")
            with self.assertRaisesRegex(ValueError, "presence"):
                act("Ada", "attack Bob", "no-online-witness")

    def test_stock_player_bound_telnet_cli_installs_socket_witness_only_when_opted_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.sqlite3"
            Gateway(path, world_id="telnet_cli")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE optional_worldpack_extension(note TEXT)")
            token = CredentialStore(Engine(path)).provision("Ada")
            started = threading.Event()
            captured = []
            actual_serve = LocalTelnetServer.serve_forever

            def observe(server, *args, **kwargs):
                captured.append(server)
                started.set()
                return actual_serve(server, *args, **kwargs)

            outcomes = []

            def run_cli():
                try:
                    outcomes.append(telnet_main([
                        "--db", str(path), "--port", "0",
                        "--player-credentials", "--enable-local-presence",
                    ]))
                except BaseException as error:
                    outcomes.append(error)

            with patch.object(LocalTelnetServer, "serve_forever", observe):
                thread = threading.Thread(target=run_cli, daemon=True)
                thread.start()
                try:
                    self.assertTrue(started.wait(3), "Stock Telnet CLI did not bind")
                    server = captured[0]
                    self.assertEqual("127.0.0.1", server.server_address[0])
                    self.assertTrue(server.player_credentials)
                    self.assertIsNotNone(server.presence_witness)
                    with socket.create_connection(server.server_address, timeout=3) as client:
                        client.settimeout(3)
                        self.assertIn(b"Player token:", client.recv(2048))
                        client.sendall(token.encode("ascii") + b"\r\n")
                        response = client.recv(4096)
                        self.assertIn(b"REALM>", response)
                        self.assertEqual(1, len(server.presence_witness._sessions))
                finally:
                    if captured:
                        captured[0].shutdown()
                    thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
                self.assertEqual([0], outcomes)

    def test_untrusted_bogus_world_marker_cannot_trigger_core_schema_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            for marker in ("invalid world", "bogus"):
                with self.subTest(marker=marker):
                    path = Path(directory) / (marker.replace(" ", "-") + ".sqlite3")
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute("CREATE TABLE world_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                        db.execute("INSERT INTO world_meta VALUES('world_id',?)", (marker,))
                    with patch.object(LocalTelnetServer, "serve_forever", return_value=None):
                        with self.assertRaises(SystemExit) as ex:
                            telnet_main([
                                "--db", str(path), "--port", "0",
                                "--player-credentials", "--enable-local-presence",
                            ])
                    self.assertEqual(2, ex.exception.code)
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(["world_meta"], [row[0] for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                        )], "Rejected bogus world marker must not seed an Engine")

    def test_forged_core_table_names_with_wrong_columns_fail_without_db_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forged.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE world_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.execute("INSERT INTO world_meta VALUES('world_id','fake')")
                db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,garbage TEXT)")
                for table in ("rooms", "exits", "players", "items", "action_receipts"):
                    db.execute(f"CREATE TABLE {table}(garbage TEXT)")
            before = path.read_bytes()
            with self.assertRaises(SystemExit) as ex:
                telnet_main([
                    "--db", str(path), "--port", "0",
                    "--player-credentials", "--enable-local-presence",
                ])
            self.assertEqual(2, ex.exception.code)
            self.assertEqual(before, path.read_bytes(),
                             "Bogus schema must be rejected before WAL/Engine writes")

    def test_one_time_host_policy_and_real_scoped_cli_http_pvp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.sqlite3"
            policy_args = [
                "--db", str(path), "--world-id", "cli_live",
                "--scoped-credentials", "--install-pvp-policy",
                "--pvp-safe-room", "atrium",
            ]
            self.assertEqual(0, gateway_main(policy_args))
            with self.assertRaisesRegex(ValueError, "already installed"):
                gateway_main(policy_args)
            setup = Gateway(path, world_id="cli_live", scoped_credentials=True)
            for player in ("Ada", "Bob"):
                setup.engine.join(player)
            tokens = {
                name: setup.scoped_credentials.provision(name)
                for name in ("Ada", "Bob")
            }
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual((1, 0), db.execute(
                    "SELECT COUNT(*),MAX(allow_offline_targets) FROM combat_policy"
                ).fetchone())
            servers = []
            ready = threading.Event()
            actual_serve = GatewayServer.serve_forever

            def observed_serve(server, *args, **kwargs):
                servers.append(server)
                ready.set()
                return actual_serve(server, *args, **kwargs)

            outcomes = []
            def run_cli():
                try:
                    outcomes.append(gateway_main([
                        "--db", str(path), "--world-id", "cli_live",
                        "--scoped-credentials", "--enable-local-presence",
                        "--port", "0",
                    ]))
                except BaseException as error:
                    outcomes.append(error)

            with patch.object(GatewayServer, "serve_forever", observed_serve):
                thread = threading.Thread(target=run_cli, daemon=True)
                thread.start()
                try:
                    self.assertTrue(ready.wait(3), "Stock CLI did not bind a listener")
                    server = servers[0]
                    self.assertEqual("127.0.0.1", server.server_address[0])
                    self.assertIsNotNone(server.presence_witness)

                    def command(name: str, text: str, action_id: str):
                        conn = http.client.HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=4,
                        )
                        try:
                            payload = json.dumps({
                                "player": name, "command": text,
                                "action_id": action_id,
                            })
                            conn.request("POST", "/api/v1/command", payload, headers={
                                "Authorization": "Bearer " + tokens[name],
                                "Content-Type": "application/json",
                            })
                            response = conn.getresponse()
                            return response.status, json.loads(response.read())
                        finally:
                            conn.close()

                    self.assertEqual(200, command("Ada", "pvp on", "a-on")[0])
                    self.assertEqual(200, command("Bob", "pvp on", "b-on")[0])
                    status, denied = command("Ada", "attack Bob", "safe-spawn")
                    self.assertEqual(400, status)
                    self.assertIn("safe zone", denied["error"])
                    self.assertEqual(200, command("Ada", "north", "a-north")[0])
                    self.assertEqual(200, command("Bob", "north", "b-north")[0])
                    self.assertEqual(200, command("Ada", "attack Bob", "actual-pvp")[0])
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(8, db.execute(
                            "SELECT hp FROM combatants WHERE player='Bob'"
                        ).fetchone()[0])
                finally:
                    if servers:
                        servers[0].shutdown()
                    thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
                self.assertEqual([0], outcomes)


if __name__ == "__main__":
    unittest.main()
