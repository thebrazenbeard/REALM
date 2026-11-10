"""Real credential-bound transport activity qualifies default-offline PvP."""
from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from realm.auth import CredentialStore
from realm.combat import Combat
from realm.engine import Engine
from realm.gateway import Gateway, GatewayServer
from realm.local_presence import LocalPresenceWitness
from realm.telnet import LocalTelnetServer


class LivePvPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "world.sqlite3"
        self.gateway = Gateway(
            self.path, world_id="livepvp", scoped_credentials=True,
            scoped_guard_path=Path(temporary.name) / "scoped-anchor.json",
        )
        self.engine = self.gateway.engine
        for name in ("Ada", "Bob", "Cara"):
            self.engine.join(name)
        self.tokens = {
            name: self.gateway.scoped_credentials.provision(name)
            for name in ("Ada", "Bob", "Cara")
        }
        Combat.configure(self.engine, max_hp=4, damage=1, safe_rooms=())
        self.clock = [0.0]
        self.witness = LocalPresenceWitness(
            self.engine, "livepvp", idle_seconds=10,
            monotonic=lambda: self.clock[0],
        )
        self.http = GatewayServer(self.gateway, (), port=0, presence_witness=self.witness)
        thread = threading.Thread(
            target=self.http.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        thread.start()
        self.addCleanup(lambda: (
            self.http.shutdown(), self.http.server_close(), thread.join(timeout=2),
        ))

    def exchange(self, name: str, path: str, *, command: str | None = None,
                 action_id: str | None = None, token: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=4)
        headers = {"Authorization": "Bearer " + (token or self.tokens[name])}
        body = None
        method = "GET"
        if command is not None:
            method = "POST"
            headers["Content-Type"] = "application/json"
            body = json.dumps({"player": name, "command": command,
                               "action_id": action_id}).encode()
        try:
            conn.request(method, path, body=body, headers=headers)
            result = conn.getresponse()
            return result.status, json.loads(result.read())
        finally:
            conn.close()

    def command(self, name: str, text: str, action_id: str):
        return self.exchange(name, "/api/v1/command", command=text,
                             action_id=action_id)

    def test_two_scoped_http_players_pvp_with_recent_feed_activity(self):
        self.assertEqual(200, self.command("Ada", "pvp on", "opt-a")[0])
        self.assertEqual(200, self.command("Bob", "pvp on", "opt-b")[0])
        for name in ("Ada", "Bob"):
            self.assertEqual(200, self.exchange(name, "/api/v1/feed?after=0")[0])
        self.assertEqual(200, self.command("Ada", "attack Bob", "hit-one")[0])
        self.assertEqual(200, self.command("Ada", "attack Bob", "hit-one")[0])
        with closing(self.engine._connect()) as db:
            self.assertEqual(3, db.execute(
                "SELECT hp FROM combatants WHERE player='Bob'"
            ).fetchone()[0], "Same-ID replay must not double damage")
        for serial in range(2, 5):
            self.assertEqual(200, self.command("Ada", "attack Bob", f"hit-{serial}")[0])
        spectator = self.exchange("Cara", "/api/v1/feed?after=0")
        self.assertEqual(200, spectator[0])
        death = [e for e in spectator[1]["events"] if e["kind"] == "pvp_death"]
        self.assertEqual(1, len(death))
        self.assertEqual("global", death[0]["room"])
        self.assertNotIn("atrium", str(death[0]).lower())

    def test_recent_http_lease_expires_then_fresh_authorized_feed_readmits(self):
        self.command("Ada", "pvp on", "a-on")
        self.command("Bob", "pvp on", "b-on")
        self.clock[0] = 11.0
        status, body = self.command("Ada", "attack Bob", "stale")
        self.assertEqual(400, status)
        self.assertIn("presence", str(body).lower())
        self.assertEqual(200, self.exchange("Bob", "/api/v1/feed?after=0")[0])
        self.assertEqual(200, self.command("Ada", "attack Bob", "fresh")[0])

    def test_revocation_invalidates_target_even_inside_recency_window(self):
        self.command("Ada", "pvp on", "a-on")
        self.command("Bob", "pvp on", "b-on")
        self.gateway.scoped_credentials.revoke("Bob")
        status, body = self.command("Ada", "attack Bob", "revoked-target")
        self.assertEqual(400, status)
        self.assertIn("presence", str(body).lower())
        self.assertEqual(401, self.exchange("Bob", "/api/v1/feed?after=0")[0])

    def test_repeated_scoped_polls_and_token_rotation_keep_one_actor_lease(self):
        self.command("Bob", "pvp on", "b-on")
        for _ in range(25):
            self.assertEqual(200, self.exchange("Bob", "/api/v1/feed?after=0")[0])
        self.assertEqual(1, len(self.http._presence_sessions))
        self.assertEqual(1, len(self.witness._sessions),
                         "Polling must refresh, not mint distinct witness sessions")
        previous = self.tokens["Bob"]
        self.tokens["Bob"] = self.gateway.scoped_credentials.rotate(previous)
        self.assertEqual(401, self.exchange("Bob", "/api/v1/feed?after=0",
                                            token=previous)[0])
        self.assertEqual(200, self.exchange("Bob", "/api/v1/feed?after=0")[0])
        self.assertEqual(1, len(self.http._presence_sessions))
        self.assertEqual(1, len(self.witness._sessions),
                         "Rotated token must replace prior actor lease")

    def test_feed_revoke_during_presence_touch_cannot_send_stale_bytes(self):
        self.command("Ada", "say Revocation secret", "private-test")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.http.refresh_scoped_presence
        def held_touch(*args, **kwargs):
            original(*args, **kwargs)
            entered.set()
            self.assertTrue(release.wait(4))
        self.http.refresh_scoped_presence = held_touch
        result = {}
        reader = threading.Thread(target=lambda: result.setdefault(
            "response", self.exchange("Bob", "/api/v1/feed?after=0")
        ), daemon=True)
        reader.start()
        self.assertTrue(entered.wait(2))
        self.gateway.scoped_credentials.revoke("Bob")
        release.set()
        reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        status, body = result["response"]
        self.assertEqual(401, status)
        self.assertNotIn("Revocation secret", json.dumps(body))

    def test_failed_bind_does_not_orphan_engine_witness_or_block_replacement(self):
        contender = LocalPresenceWitness(self.engine, "livepvp", idle_seconds=10)
        with self.assertRaises(OSError):
            GatewayServer(
                self.gateway, (), port=self.http.server_port,
                presence_witness=contender,
            )
        self.assertIs(self.witness, self.engine._local_presence_witness)
        self.http.shutdown()
        self.http.server_close()
        self.assertIsNone(getattr(self.engine, "_local_presence_witness", None))
        replacement = GatewayServer(
            self.gateway, (), port=0, presence_witness=contender,
        )
        self.assertIs(contender, self.engine._local_presence_witness)
        replacement.server_close()
        self.assertIsNone(getattr(self.engine, "_local_presence_witness", None))

    def test_two_host_listeners_sharing_engine_release_only_after_both_close(self):
        player_terminal = LocalTelnetServer(
            self.engine, None, 0, player_credentials=True,
            presence_witness=self.witness,
        )
        self.addCleanup(player_terminal.server_close)
        self.http.shutdown()
        self.http.server_close()
        self.assertIs(self.witness, self.engine._local_presence_witness)
        player_terminal.server_close()
        self.assertIsNone(getattr(self.engine, "_local_presence_witness", None))

    def test_shared_token_telnet_never_mints_pvp_presence(self):
        self.command("Ada", "pvp on", "a-on")
        self.command("Bob", "pvp on", "b-on")
        # The target's scoped HTTP lease ages out while legacy Telnet remains
        # genuinely connected using a shared server token and a claimed name.
        self.clock[0] = 11.0
        with self.assertRaisesRegex(ValueError, "player-bound"):
            LocalTelnetServer(self.engine, "shared-not-personal", 0,
                              presence_witness=self.witness)
        legacy = LocalTelnetServer(self.engine, "shared-not-personal", 0)
        thread = threading.Thread(target=legacy.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (
            legacy.shutdown(), legacy.server_close(), thread.join(timeout=2),
        ))
        conn = socket.create_connection(legacy.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        self._read_to(conn, b"Token: ")
        conn.sendall(b"shared-not-personal\r\n")
        self._read_to(conn, b"Name: ")
        conn.sendall(b"Bob\r\n")
        self._read_to(conn, b"REALM> ")
        status, body = self.command("Ada", "attack Bob", "not-from-shared")
        self.assertEqual(400, status)
        self.assertIn("presence", str(body).lower())

    def test_shared_token_cannot_borrow_real_live_players_combat_authority(self):
        self.command("Ada", "pvp on", "a-on")
        self.command("Bob", "pvp on", "b-on")
        legacy = LocalTelnetServer(self.engine, "shared-not-personal", 0)
        thread = threading.Thread(target=legacy.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (
            legacy.shutdown(), legacy.server_close(), thread.join(timeout=2),
        ))
        conn = socket.create_connection(legacy.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        self._read_to(conn, b"Token: ")
        conn.sendall(b"shared-not-personal\r\n")
        self._read_to(conn, b"Name: ")
        conn.sendall(b"Ada\r\n")
        self._read_to(conn, b"REALM> ")
        conn.sendall(b"attack Bob\r\n")
        self.assertIn("Access revoked", self._read_to(conn, b"Access revoked"))
        with closing(self.engine._connect()) as db:
            self.assertEqual(4, db.execute(
                "SELECT hp FROM combatants WHERE player='Bob'"
            ).fetchone()[0])
        self.assertEqual(200, self.command("Ada", "attack Bob", "real-attacker")[0])

    def test_second_engine_or_process_cannot_borrow_local_witness(self):
        self.command("Ada", "pvp on", "a-on")
        self.command("Bob", "pvp on", "b-on")
        foreign_engine = Engine(self.path)
        with self.assertRaisesRegex(ValueError, "presence"):
            foreign_engine.act("Ada", "attack Bob", action_id="other-engine",
                               writer_guard=lambda db: None)
        with closing(self.engine._connect()) as db:
            self.assertEqual(4, db.execute(
                "SELECT hp FROM combatants WHERE player='Bob'"
            ).fetchone()[0])

    @staticmethod
    def _read_to(conn: socket.socket, marker: bytes):
        data = bytearray()
        for _ in range(20):
            chunk = conn.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if marker in data:
                break
        assert marker in data, data
        return data.decode(errors="replace")


class TelnetPresenceTests(unittest.TestCase):
    """A host-owned live socket refreshes while idle; EOF revokes immediately."""

    exchange = LivePvPTests.exchange
    command = LivePvPTests.command
    _read_to = staticmethod(LivePvPTests._read_to)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "world.sqlite3"
        self.gateway = Gateway(
            path, world_id="socketpvp", scoped_credentials=True,
            scoped_guard_path=Path(temporary.name) / "anchor.json",
        )
        self.engine = self.gateway.engine
        for name in ("Ada", "Bob"):
            self.engine.join(name)
        self.tokens = {name: self.gateway.scoped_credentials.provision(name)
                       for name in ("Ada", "Bob")}
        Combat.configure(self.engine, max_hp=4, damage=1, safe_rooms=())
        self.witness = LocalPresenceWitness(self.engine, "socketpvp", idle_seconds=1)
        self.http = GatewayServer(self.gateway, (), port=0, presence_witness=self.witness)
        self.http_thread = threading.Thread(
            target=self.http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.http_thread.start()
        self.addCleanup(lambda: (
            self.http.shutdown(), self.http.server_close(),
            self.http_thread.join(timeout=2),
        ))
        self.player_tokens = CredentialStore(self.engine)
        self.telnet_token = self.player_tokens.provision("Bob")
        self.telnet = LocalTelnetServer(
            Engine(path), None, 0, player_credentials=True,
            presence_witness=self.witness,
        )
        self.telnet_thread = threading.Thread(
            target=self.telnet.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.telnet_thread.start()
        self.addCleanup(lambda: (
            self.telnet.shutdown(), self.telnet.server_close(),
            self.telnet_thread.join(timeout=2),
        ))

    def login_bob(self):
        conn = socket.create_connection(self.telnet.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        self._read_to(conn, b"Player token: ")
        conn.sendall((self.telnet_token + "\r\n").encode())
        self._read_to(conn, b"REALM> ")
        return conn

    def test_authenticated_idle_socket_stays_present_then_eof_removes_lease(self):
        bob = self.login_bob()
        bob.sendall(b"pvp on\r\n")
        self.assertIn("now ON", self._read_to(bob, b"REALM> "))
        self.assertEqual(200, self.command("Ada", "pvp on", "a-on")[0])
        time.sleep(1.45)  # longer than 1s witness TTL, no client typing
        self.assertEqual(200, self.command("Ada", "attack Bob", "while-idle")[0])
        bob.close()
        deadline = time.monotonic() + 2
        while self.telnet.is_name_active("Bob") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.telnet.is_name_active("Bob"))
        status, body = self.command("Ada", "attack Bob", "after-eof")
        self.assertEqual(400, status)
        self.assertIn("presence", str(body).lower())

    def test_revoked_telnet_credential_denies_even_before_socket_closes(self):
        bob = self.login_bob()
        bob.sendall(b"pvp on\r\n")
        self.assertIn("now ON", self._read_to(bob, b"REALM> "))
        self.command("Ada", "pvp on", "a-on")
        self.assertTrue(self.player_tokens.revoke("Bob"))
        status, body = self.command("Ada", "attack Bob", "revoked-while-open")
        self.assertEqual(400, status)
        self.assertIn("presence", str(body).lower())


if __name__ == "__main__":
    unittest.main()
