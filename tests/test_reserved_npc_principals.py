"""Host NPC identity reservation must deny every player network bearer lane."""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer
from realm.principal_roles import is_reserved_npc, reserve_npc
from realm.residents import ResidentController
from realm.telnet import LocalTelnetServer


class ReservedNPCPrincipalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = Gateway(self.root / "legacy.sqlite3", world_id="one")
        self.scoped = Gateway(
            self.root / "scoped.sqlite3", world_id="two",
            scoped_credentials=True,
        )
        self.legacy.join("Moss")
        self.scoped.join("Moss")

    def test_host_reservation_requires_bound_world_and_existing_character(self):
        with self.assertRaises(ValueError):
            reserve_npc(self.legacy.engine, "Unjoined")
        self.assertTrue(reserve_npc(self.legacy.engine, "mOsS"))
        self.assertFalse(reserve_npc(self.legacy.engine, "Moss"))
        with closing(self.legacy.engine._connect()) as db:
            self.assertTrue(is_reserved_npc(db, "MOSS"))
            self.assertEqual(
                ("Moss", "one"),
                tuple(db.execute(
                    "SELECT player_name,world_id FROM main.reserved_npc_principals"
                ).fetchone()),
            )
        with closing(self.scoped.engine._connect()) as db:
            self.assertFalse(is_reserved_npc(db, "Moss"))

    def test_reservation_denies_new_legacy_and_scoped_player_bearers(self):
        reserve_npc(self.legacy.engine, "Moss")
        reserve_npc(self.scoped.engine, "Moss")
        with self.assertRaises(ValueError):
            self.legacy.credentials.provision("MOSS")
        with self.assertRaises(ValueError):
            self.scoped.scoped_credentials.provision("moss")
        self.legacy.join("Juniper")
        self.scoped.join("Juniper")
        self.assertIsInstance(self.legacy.credentials.provision("Juniper"), str)
        self.assertIsInstance(
            self.scoped.scoped_credentials.provision("Juniper"), str
        )

    def test_reservation_blocks_preissued_legacy_bearer_and_writer_rechecks(self):
        old = self.legacy.credentials.provision("Moss")
        self.assertEqual("Moss", self.legacy.credentials.authenticate(old))
        reserve_npc(self.legacy.engine, "moss")
        self.assertIsNone(self.legacy.credentials.authenticate(old))
        with self.assertRaises(PermissionError):
            self.legacy.command_legacy_remote(
                "Moss", "say SPOOFED BY CLIENT", "old-legacy", old
            )
        with self.assertRaises(ValueError):
            self.legacy.credentials.rotate("MOSS")
        self.assertNotIn("SPOOFED BY CLIENT",
                         self.legacy.command("Moss", "history", None)["text"])

    def test_reservation_blocks_preissued_scoped_bearer_writer_and_rotation(self):
        old = self.scoped.scoped_credentials.provision("Moss")
        self.assertEqual("Moss", self.scoped.scoped_credentials.authenticate(
            old, required_capability="command:write",
        ))
        reserve_npc(self.scoped.engine, "Moss")
        self.assertIsNone(self.scoped.scoped_credentials.authenticate(
            old, required_capability="command:write",
        ))
        with self.assertRaises(PermissionError):
            self.scoped.command_remote(
                "Moss", "say SPOOFED BY SCOPED", "old-scoped", old
            )
        with self.assertRaises(ValueError):
            self.scoped.scoped_credentials.rotate(old)
        self.assertNotIn("SPOOFED BY SCOPED",
                         self.scoped.command("Moss", "history", None)["text"])

    def test_reservation_survives_reopen_without_mutating_other_world(self):
        legacy_old = self.legacy.credentials.provision("Moss")
        reserve_npc(self.legacy.engine, "Moss")
        recovered = Gateway(self.root / "legacy.sqlite3", world_id="one")
        self.assertIsNone(recovered.credentials.authenticate(legacy_old))
        unrelated = self.scoped.scoped_credentials.provision("Moss")
        self.assertEqual("Moss", self.scoped.scoped_credentials.authenticate(
            unrelated, required_capability="command:write",
        ))

    def test_actual_loopback_http_rejects_preissued_reserved_legacy_bearer(self):
        token = self.legacy.credentials.provision("Moss")
        reserve_npc(self.legacy.engine, "Moss")
        server = GatewayServer(self.legacy, (), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = json.dumps({
                "player": "Moss", "command": "say HTTP IMPERSONATION",
                "action_id": "http-old-reserved",
            }).encode()
            req = request.Request(
                f"http://127.0.0.1:{server.server_port}/api/v1/command",
                data=data,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(error.HTTPError) as raised:
                request.urlopen(req, timeout=3)
            self.assertEqual(401, raised.exception.code)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_reservation_races_legacy_and_scoped_issuance_without_dual_authority(self):
        for gate, store, prefix in (
            (self.legacy, self.legacy.credentials, "L"),
            (self.scoped, self.scoped.scoped_credentials, "S"),
        ):
            for index in range(4):
                actor = f"{prefix}Resident{index}"
                gate.join(actor)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    issuing = pool.submit(store.provision, actor)
                    reserving = pool.submit(reserve_npc, gate.engine, actor)
                    self.assertTrue(reserving.result())
                    try:
                        token = issuing.result()
                    except ValueError:
                        token = None
                with closing(gate.engine._connect()) as db:
                    self.assertTrue(is_reserved_npc(db, actor))
                if token is not None:
                    self.assertIsNone(store.authenticate(token))

    def test_main_schema_not_shad_owned_by_temp_role_table(self):
        reserve_npc(self.legacy.engine, "Moss")
        with closing(self.legacy.engine._connect()) as db:
            db.execute("CREATE TEMP TABLE reserved_npc_principals (player_name TEXT)")
            self.assertTrue(is_reserved_npc(db, "Moss"))
            self.assertFalse(is_reserved_npc(db, "Juniper"))

    def test_silent_reservation_trigger_delete_rolls_back(self):
        self.legacy.join("Seed")
        self.assertTrue(reserve_npc(self.legacy.engine, "Seed"))
        with closing(self.legacy.engine._connect()) as db, db:
            db.execute(
                "CREATE TRIGGER suppress_reservation AFTER INSERT "
                "ON main.reserved_npc_principals "
                "WHEN NEW.player_name='Moss' BEGIN "
                "DELETE FROM reserved_npc_principals "
                "WHERE player_name='Moss'; END"
            )
        with self.assertRaises(RuntimeError):
            reserve_npc(self.legacy.engine, "Moss")
        with closing(self.legacy.engine._connect()) as db:
            self.assertFalse(is_reserved_npc(db, "Moss"))

    def test_scoped_http_preissued_reserved_bearer_denied(self):
        token = self.scoped.scoped_credentials.provision("Moss")
        reserve_npc(self.scoped.engine, "Moss")
        server = GatewayServer(self.scoped, (), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = request.Request(
                f"http://127.0.0.1:{server.server_port}/api/v1/command",
                data=json.dumps({
                    "player": "Moss", "command": "say FORGED",
                    "action_id": "scoped-reserved"
                }).encode(),
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(error.HTTPError) as raised:
                request.urlopen(req, timeout=3)
            self.assertEqual(401, raised.exception.code)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_reserved_world_binding_cannot_be_relabelled_through_gateway(self):
        reserve_npc(self.legacy.engine, "Moss")
        with self.assertRaisesRegex(ValueError, "already bound"):
            Gateway(self.root / "legacy.sqlite3", world_id="different")
        with closing(self.legacy.engine._connect()) as db:
            self.assertTrue(is_reserved_npc(db, "Moss"))

    def test_reservation_vs_token_rotation_race_blocks_any_new_bearer(self):
        for gate, store, prefix in (
            (self.legacy, self.legacy.credentials, "Legacy"),
            (self.scoped, self.scoped.scoped_credentials, "Scoped"),
        ):
            for index in range(3):
                actor = f"{prefix}Rotate{index}"
                gate.join(actor)
                old = store.provision(actor)
                def rotate():
                    if gate is self.legacy:
                        return store.rotate(actor)
                    return store.rotate(old)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    rotating = pool.submit(rotate)
                    reserving = pool.submit(reserve_npc, gate.engine, actor)
                    self.assertTrue(reserving.result())
                    try:
                        new_token = rotating.result()
                    except ValueError:
                        new_token = None
                self.assertIsNone(store.authenticate(old))
                if new_token:
                    self.assertIsNone(store.authenticate(new_token))

    @staticmethod
    def _telnet_receive(conn: socket.socket, marker: bytes) -> bytes:
        data = bytearray()
        for _ in range(30):
            part = conn.recv(4096)
            if not part:
                break
            data.extend(part)
            if marker in data:
                return bytes(data)
        raise AssertionError(f"Missing {marker!r} from Telnet response")

    def test_reserved_player_bearer_rejected_on_real_telnet_socket(self):
        token = self.legacy.credentials.provision("Moss")
        reserve_npc(self.legacy.engine, "Moss")
        server = LocalTelnetServer(
            self.legacy.engine, None, port=0, player_credentials=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=3) as conn:
                conn.settimeout(3)
                self._telnet_receive(conn, b"Player token: ")
                conn.sendall(token.encode() + b"\r\n")
                denial = self._telnet_receive(conn, b"Access denied")
                self.assertIn(b"Access denied", denial)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_active_telnet_bearer_revoked_by_host_reservation(self):
        token = self.legacy.credentials.provision("Moss")
        server = LocalTelnetServer(
            self.legacy.engine, None, port=0, player_credentials=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=3) as conn:
                conn.settimeout(3)
                self._telnet_receive(conn, b"Player token: ")
                conn.sendall(token.encode() + b"\r\n")
                self._telnet_receive(conn, b"REALM> ")
                reserve_npc(self.legacy.engine, "Moss")
                conn.sendall(b"say TELNET UNAUTHORIZED AFTER RESERVATION\r\n")
                denial = self._telnet_receive(conn, b"Access revoked")
                self.assertIn(b"Access revoked", denial)
            self.assertNotIn("TELNET UNAUTHORIZED AFTER RESERVATION",
                             self.legacy.command("Moss", "history", None)["text"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())


    def test_shared_token_telnet_cannot_select_reserved_npc_name(self):
        reserve_npc(self.legacy.engine, "Moss")
        server = LocalTelnetServer(self.legacy.engine, "local-shared-secret", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=3) as conn:
                conn.settimeout(3)
                self._telnet_receive(conn, b"Token: ")
                conn.sendall(b"local-shared-secret\r\n")
                self._telnet_receive(conn, b"Name: ")
                conn.sendall(b"mOsS\r\n")
                answer = self._telnet_receive(conn, b"Access revoked")
                self.assertIn(b"Access revoked", answer)
            with closing(self.legacy.engine._connect()) as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM main.events WHERE actor=? AND kind='join'",
                    ("Moss",),
                ).fetchone()[0]
                self.assertEqual(1, count)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_shared_token_telnet_active_name_revoked_after_reservation(self):
        server = LocalTelnetServer(self.legacy.engine, "local-shared-secret", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=3) as conn:
                conn.settimeout(3)
                self._telnet_receive(conn, b"Token: ")
                conn.sendall(b"local-shared-secret\r\n")
                self._telnet_receive(conn, b"Name: ")
                conn.sendall(b"Moss\r\n")
                self._telnet_receive(conn, b"REALM> ")
                reserve_npc(self.legacy.engine, "Moss")
                conn.sendall(b"say SHARED TOKEN IMPERSONATION\r\n")
                answer = self._telnet_receive(conn, b"Access revoked")
                self.assertIn(b"Access revoked", answer)
            self.assertNotIn("SHARED TOKEN IMPERSONATION",
                             self.legacy.command("Moss", "history", None)["text"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_reserved_resident_retains_authorized_host_only_action(self):
        reserve_npc(self.legacy.engine, "Moss")
        ctrl = ResidentController(self.legacy.engine, "Moss")
        result = ctrl.commit({"command": "say MOSS IS PRESENT"}, action_id="npc-safe-1")
        self.assertIn("MOSS IS PRESENT", result)
