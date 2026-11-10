"""Actual socket/HTTP parity over one independent SQLite world instance.

The browser calls this gateway via HTTP and a classic MUD client speaks the
Telnet adapter.  These tests verify their shared authoritative engine effects,
not merely that each adapter works in isolation.
"""

import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.channel_roster import ChannelRoster
from realm.gateway import Gateway, GatewayServer
from realm.telnet import LocalTelnetServer


class CrossTransportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "shared.sqlite3"

        self.gateway = Gateway(db, world_id="cross-transport-test")
        self.browser_token = self.gateway.credentials.provision("Browser")
        self.http = GatewayServer(
            self.gateway, ("http://127.0.0.1:8000",), port=0
        )
        self.telnet = LocalTelnetServer(Engine(db), "telnet-local-secret", 0)

        self.http_thread = threading.Thread(
            target=self.http.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.telnet_thread = threading.Thread(
            target=self.telnet.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.http_thread.start()
        self.telnet_thread.start()
        self.addCleanup(self._stop_servers)

    def _stop_servers(self):
        for server, thread in (
            (self.http, self.http_thread),
            (self.telnet, self.telnet_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def api(self, endpoint, payload=None):
        status, content = self.api_status(endpoint, payload)
        self.assertEqual(200, status)
        return content

    def api_status(self, endpoint, payload=None):
        headers = {"Authorization": "Bearer " + self.browser_token}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        req = request.Request(
            f"http://127.0.0.1:{self.http.server_port}{endpoint}",
            data=body,
            headers=headers,
        )
        try:
            with request.urlopen(req, timeout=6) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def read_to(self, conn, marker):
        data = bytearray()
        for _ in range(20):
            data.extend(conn.recv(4096))
            if marker in data:
                return data.decode("utf-8", errors="replace")
        self.fail(f"Telnet did not send {marker!r}; got {data!r}")

    def telnet_login(self, name):
        conn = socket.create_connection(self.telnet.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        self.read_to(conn, b"Token: ")
        conn.sendall(b"telnet-local-secret\r\n")
        self.read_to(conn, b"Name: ")
        conn.sendall(name.encode("utf-8") + b"\r\n")
        self.read_to(conn, b"REALM> ")
        return conn

    def telnet_command(self, conn, command):
        conn.sendall(command.encode("utf-8") + b"\r\n")
        return self.read_to(conn, b"REALM> ")

    def playerbound_telnet_login(self, name):
        bound = LocalTelnetServer(
            Engine(self.gateway.engine.db_path), None, 0, player_credentials=True,
        )
        if not hasattr(self, "_bound_servers"):
            self._bound_servers = []
        self._bound_servers.append(bound)
        thread = threading.Thread(
            target=bound.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
        )
        thread.start()
        def cleanup():
            bound.shutdown()
            bound.server_close()
            thread.join(timeout=2)
        self.addCleanup(cleanup)
        conn = socket.create_connection(bound.server_address, timeout=2)
        conn.settimeout(2)
        self.addCleanup(conn.close)
        self.read_to(conn, b"Player token: ")
        token = self.browser_token if name == "Browser" else self.gateway.credentials.provision(name)
        conn.sendall(token.encode("ascii") + b"\r\n")
        self.read_to(conn, b"REALM> ")
        return conn

    def test_speech_and_room_visibility_cross_browser_and_telnet(self):
        browser = self.api("/api/v1/join", {"player": "Browser"})
        telnet = self.telnet_login("Terminal")

        sent = self.api("/api/v1/command", {
            "player": "Browser", "command": "say From the browser gateway"
        })
        self.assertIn("You say", sent["text"])
        self.assertIn("Browser said: From the browser gateway", self.telnet_command(telnet, "listen"))

        self.assertIn("You say", self.telnet_command(telnet, "say From the terminal adapter"))
        query = parse.urlencode({"player": "Browser", "after": browser["cursor"]})
        feed = self.api(f"/api/v1/feed?{query}")
        self.assertTrue(any(
            event["actor"] == "Terminal" and "From the terminal adapter" in event["text"]
            for event in feed["events"]
        ))

        moved = self.api("/api/v1/command", {"player": "Browser", "command": "north"})
        self.assertIn("Corridor", moved["text"])
        self.assertIn("You say", self.telnet_command(telnet, "say Atrium-only speech"))
        query = parse.urlencode({"player": "Browser", "after": feed["cursor"]})
        after_move = self.api(f"/api/v1/feed?{query}")
        self.assertFalse(any("Atrium-only speech" in event["text"] for event in after_move["events"]))

    def test_item_state_is_shared_across_transports(self):
        self.api("/api/v1/join", {"player": "Browser"})
        telnet = self.telnet_login("Terminal")
        self.assertIn("You take Brass token.", self.telnet_command(telnet, "take token"))
        denied = self.api("/api/v1/command", {"player": "Browser", "command": "take token"})
        self.assertIn("That item is not here.", denied["text"])
        self.assertIn("Brass token", self.telnet_command(telnet, "inventory"))

    def test_telnet_give_to_browser_preserves_one_inventory_and_one_event(self):
        browser = self.api("/api/v1/join", {"player": "Browser"})
        terminal = self.telnet_login("Terminal")
        self.assertIn("You take Brass token.", self.telnet_command(terminal, "take token"))
        self.assertIn("You give Brass token to Browser.",
                      self.telnet_command(terminal, "give token to Browser"))
        held = self.api("/api/v1/command", {"player": "Browser", "command": "inventory"})
        self.assertEqual("You carry: Brass token", held["text"])
        self.assertIn("You carry: nothing", self.telnet_command(terminal, "inventory"))
        query = parse.urlencode({"player": "Browser", "after": browser["cursor"]})
        feed = self.api(f"/api/v1/feed?{query}")
        self.assertEqual(1, sum(event["kind"] == "give" for event in feed["events"]))

    def test_emote_crosses_telnet_and_http_as_one_room_event(self):
        browser = self.api("/api/v1/join", {"player": "Browser"})
        # New authored emotes require a player-bound terminal; unlike legacy
        # shared-token SAY they cannot be used to impersonate a room actor.
        terminal = self.playerbound_telnet_login("Terminal")
        self.assertIn("Terminal sketches a map.", self.telnet_command(terminal, "emote sketches a map."))
        query = parse.urlencode({"player": "Browser", "after": browser["cursor"]})
        events = self.api(f"/api/v1/feed?{query}")["events"]
        self.assertEqual(1, sum(row["kind"] == "emote" for row in events))
        self.assertIn("Terminal sketches a map.", events[-1]["text"])
        self.assertIn("Browser listens closely.", self.api("/api/v1/command", {
            "player": "Browser", "command": "emote listens closely.",
        })["text"])
        self.assertIn("Browser listens closely.", self.telnet_command(terminal, "listen"))
    def test_world_ooc_and_host_granted_group_chat_have_http_telnet_parity(self):
        joined = self.api("/api/v1/join", {"player": "Browser"})
        self.gateway.join("Terminal")
        terminal = self.playerbound_telnet_login("Terminal")
        outsider = self.playerbound_telnet_login("Outsider")
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("local_crew", "Local Crew")
        roster.grant("Browser", "local_crew", role="Leader")
        roster.grant("Terminal", "local_crew", role="Mate")

        http_sent = self.api("/api/v1/command", {
            "player": "Browser", "command": "crewchat From HTTP"
        })
        self.assertEqual('[Local Crew][Leader] Browser: "From HTTP"', http_sent["text"])
        self.assertIn(http_sent["text"], self.telnet_command(terminal, "listen"))
        self.assertNotIn("From HTTP", self.telnet_command(outsider, "history 50"))

        response = self.telnet_command(terminal, "crewchat From terminal")
        self.assertIn('[Local Crew][Mate] Terminal: "From terminal"', response)
        feed = self.api("/api/v1/feed?" + parse.urlencode({
            "player": "Browser", "after": joined["cursor"],
        }))
        self.assertTrue(any(event["text"] == '[Local Crew][Mate] Terminal: "From terminal"'
                            for event in feed["events"]))

        global_reply = self.telnet_command(outsider, "ooc From an outsider")
        self.assertIn("[OOC Channel] Outsider: From an outsider", global_reply)
        self.assertIn("[OOC Channel] Outsider: From an outsider",
                      self.telnet_command(terminal, "listen"))
        next_feed = self.api("/api/v1/feed?" + parse.urlencode({
            "player": "Browser", "after": feed["cursor"],
        }))
        self.assertTrue(any(event["text"] == "[OOC Channel] Outsider: From an outsider"
                            for event in next_feed["events"]))
        roster.revoke("Terminal")
        self.assertIn("No active group channel membership",
                      self.telnet_command(terminal, "crewchat Not authorized"))
        self.assertNotIn("From HTTP", self.telnet_command(terminal, "history 50"))
        self.gateway.credentials.revoke("Terminal")
        terminal.sendall(b"history 50\r\n")
        self.assertIn("Access revoked.", self.read_to(terminal, b"Access revoked."))

    def test_shared_server_token_cannot_impersonate_member_to_read_or_send_group_chat(self):
        self.api("/api/v1/join", {"player": "Browser"})
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("private_group", "Private Group")
        roster.grant("Browser", "private_group", role="Leader")
        self.api("/api/v1/command", {
            "player": "Browser", "command": "crewchat Restricted message"
        })
        # Shared-token terminal deliberately lets clients CLAIM a player name;
        # membership in the database cannot turn that into private identity.
        impersonator = self.telnet_login("Browser")
        self.assertNotIn("Restricted message", self.telnet_command(impersonator, "history 50"))
        self.assertIn("player-bound credentials", self.telnet_command(
            impersonator, "crewchat Forged private message"
        ))
        self.assertIn("player-bound credentials", self.telnet_command(
            impersonator, "ooc Forged global identity"
        ))
        self.assertIn("player-bound credentials", self.telnet_command(
            impersonator, "emote impersonates an authenticated adventurer"
        ))
        self.assertFalse(any(
            ("Forged private message" in event["text"] or
             "Forged global identity" in event["text"] or
             "impersonates an authenticated adventurer" in event["text"])
            for event in self.api("/api/v1/feed?player=Browser")["events"]
        ))

    def test_legacy_http_revoked_bearer_cannot_commit_world_or_group_chat(self):
        self.api("/api/v1/join", {"player": "Browser"})
        self.gateway.join("Watcher")
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("testing", "Testing")
        roster.grant("Browser", "testing", role="Speaker")
        roster.grant("Watcher", "testing", role="Member")
        for command, kind in (
            ("ooc REVOKED_WORLD_POST", "ooc"),
            ("crewchat REVOKED_GROUP_POST", "crewchat"),
        ):
            with self.subTest(kind=kind):
                entered, release = threading.Event(), threading.Event()
                original = self.gateway.command_legacy_remote

                def gated(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("Test timed out before credential revoke")
                    return original(*args, **kwargs)

                self.gateway.command_legacy_remote = gated
                token = self.browser_token
                result = {}

                def send():
                    body = json.dumps({"player": "Browser", "command": command,
                                       "action_id": "revoked-" + kind}).encode()
                    req = request.Request(
                        f"http://127.0.0.1:{self.http.server_port}/api/v1/command",
                        data=body,
                        headers={"Authorization": "Bearer " + token,
                                 "Content-Type": "application/json"},
                    )
                    try:
                        with request.urlopen(req, timeout=5) as response:
                            result["status"] = response.status
                    except error.HTTPError as response:
                        result["status"] = response.code
                        result["body"] = response.read().decode()

                thread = threading.Thread(target=send, daemon=True)
                try:
                    thread.start()
                    self.assertTrue(entered.wait(3), "HTTP command did not reach writer gate")
                    self.assertTrue(self.gateway.credentials.revoke("Browser"))
                finally:
                    release.set()
                    thread.join(timeout=5)
                    self.gateway.command_legacy_remote = original
                self.assertFalse(thread.is_alive())
                self.assertEqual(401, result.get("status"), result)
                self.assertFalse(any(row["kind"] == kind and "REVOKED" in row["text"]
                                     for row in self.gateway.feed("Watcher", 0)["events"]))
                self.browser_token = self.gateway.credentials.provision("Browser")

    def test_private_feed_is_denied_if_membership_revoked_and_regranted_after_result(self):
        self.api("/api/v1/join", {"player": "Browser"})
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("flight", "Flight")
        roster.grant("Browser", "flight", role="Captain")
        self.api("/api/v1/command", {"command": "crewchat FEED_SECRET"})
        built, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.gateway.feed

        def held_feed(*args, **kwargs):
            result = original(*args, **kwargs)
            built.set()
            self.assertTrue(release.wait(5))
            return result

        self.gateway.feed = held_feed
        response = {}
        client = threading.Thread(target=lambda: response.setdefault(
            "result", self.api_status("/api/v1/feed?player=Browser&after=0")
        ), daemon=True)
        client.start()
        self.assertTrue(built.wait(3))
        roster.revoke("Browser")
        # Regrant to the *same* group/role without an intervening message.
        # A post-response check of current membership alone would pass.
        roster.grant("Browser", "flight", role="Captain")
        release.set()
        client.join(timeout=6)
        self.assertFalse(client.is_alive())
        status, body = response["result"]
        self.assertEqual(409, status)
        self.assertNotIn("FEED_SECRET", json.dumps(body))

    def test_private_history_is_denied_if_membership_revoked_after_result_built(self):
        self.api("/api/v1/join", {"player": "Browser"})
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("flight", "Flight")
        roster.grant("Browser", "flight", role="Captain")
        self.api("/api/v1/command", {"command": "crewchat HISTORY_SECRET"})
        built, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.gateway.command_legacy_remote

        def held_command(*args, **kwargs):
            result = original(*args, **kwargs)
            built.set()
            self.assertTrue(release.wait(5))
            return result

        self.gateway.command_legacy_remote = held_command
        response = {}
        client = threading.Thread(target=lambda: response.setdefault(
            "result", self.api_status("/api/v1/command", {"command": "history 50"})
        ), daemon=True)
        client.start()
        self.assertTrue(built.wait(3))
        roster.revoke("Browser")
        release.set()
        client.join(timeout=6)
        self.assertFalse(client.is_alive())
        status, body = response["result"]
        self.assertEqual(409, status)
        self.assertNotIn("HISTORY_SECRET", json.dumps(body))

    def test_bound_telnet_private_history_revoke_during_response_denied(self):
        self.api("/api/v1/join", {"player": "Browser"})
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("flight", "Flight")
        roster.grant("Browser", "flight", role="Captain")
        self.api("/api/v1/command", {"command": "crewchat TELNET_SECRET"})
        terminal = self.playerbound_telnet_login("Browser")
        bound = next(server for server in self._bound_servers if server.player_credentials)
        built, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = bound.router.act

        def held_act(*args, **kwargs):
            result = original(*args, **kwargs)
            built.set()
            self.assertTrue(release.wait(5))
            return result

        bound.router.act = held_act
        terminal.sendall(b"history 50\r\n")
        self.assertTrue(built.wait(3))
        roster.revoke("Browser")
        release.set()
        response = self.read_to(terminal, b"REALM> ")
        self.assertNotIn("TELNET_SECRET", response)
        self.assertIn("membership", response.lower())

    def test_bound_telnet_token_revoked_before_private_history_write_is_denied(self):
        self.api("/api/v1/join", {"player": "Browser"})
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("flight", "Flight")
        roster.grant("Browser", "flight", role="Captain")
        self.api("/api/v1/command", {"command": "crewchat TOKEN_SECRET"})
        terminal = self.playerbound_telnet_login("Browser")
        bound = self._bound_servers[-1]
        built, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = bound.router.act

        def held_act(*args, **kwargs):
            result = original(*args, **kwargs)
            built.set()
            self.assertTrue(release.wait(5))
            return result

        bound.router.act = held_act
        terminal.sendall(b"history 50\r\n")
        self.assertTrue(built.wait(3))
        self.assertTrue(self.gateway.credentials.revoke("Browser"))
        release.set()
        response = self.read_to(terminal, b"Access revoked.")
        self.assertNotIn("TOKEN_SECRET", response)
        self.assertIn("Access revoked.", response)

    def test_http_private_crewchat_ack_revoked_before_reply_is_denied(self):
        self.api("/api/v1/join", {"player": "Browser"})
        roster = ChannelRoster(self.gateway.engine.db_path, world_id="cross-transport-test")
        roster.install()
        roster.create_group("flight", "Flight")
        roster.grant("Browser", "flight", role="Captain")
        built, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.gateway.command_legacy_remote

        def held_command(*args, **kwargs):
            result = original(*args, **kwargs)
            built.set()
            self.assertTrue(release.wait(5))
            return result

        self.gateway.command_legacy_remote = held_command
        response = {}
        client = threading.Thread(target=lambda: response.setdefault(
            "result", self.api_status("/api/v1/command", {"command": "crewchat ACK_SECRET"})
        ), daemon=True)
        client.start()
        self.assertTrue(built.wait(3))
        roster.revoke("Browser")
        release.set()
        client.join(timeout=6)
        self.assertFalse(client.is_alive())
        status, body = response["result"]
        self.assertEqual(409, status)
        self.assertNotIn("ACK_SECRET", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
