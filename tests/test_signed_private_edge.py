"""In-process signed edge → real SQLite Gateway tests; no network listener."""

from dataclasses import replace
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway
from realm.private_host_contract import AdmissionDenied, PrivateHostPolicy
from realm.signed_private_edge import (
    EphemeralInvitations, LocalEdgeSigner, LocalRequest,
    LocalSignedBackend, SignedForwarding,
)


_TEST_KEY = b"REALM-fixture-key-never-use-in-production-0123456789"


class SignedPrivateEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "forest.sqlite3"
        self.gateway = Gateway(self.db_path, world_id="forest")
        self.policy = PrivateHostPolicy(
            host_id="local-test-host", world_id="forest",
            https_origin="https://forest.example.test",
            allowed_browser_origins=frozenset({"https://app.example.test"}),
            max_request_bytes=4096, max_header_bytes=1000,
            max_requests_per_window=3,
        )
        self.invitations = EphemeralInvitations(host_id="local-test-host", world_id="forest")
        self.token = self.invitations.issue("Oak", now=100, lifetime=120)
        self.signer = LocalEdgeSigner(self.policy, self.invitations, key=_TEST_KEY)
        self.backend = LocalSignedBackend(self.gateway, self.policy, self.invitations, key=_TEST_KEY)

    def request(self, path="/api/v1/join", *, player="Oak", token=None,
                method="POST", command=None, action_id=None, headers=()):
        obj = {"player": player}
        if command is not None:
            obj.update(command=command, action_id=action_id)
        body = json.dumps(obj, sort_keys=True).encode("utf-8") if method == "POST" else b""
        return LocalRequest(
            method=method, path=path,
            headers=(("Host", "forest.example.test"),
                     ("Authorization", "Bearer " + (self.token if token is None else token)),
                     ("Origin", "https://app.example.test"), *headers),
            body=body,
        )

    def dispatch(self, request, *, now=110):
        signed = self.signer.sign(request, now=now)
        return self.backend.dispatch(request, signed, now=now)

    def event_count(self, actor=None, kind=None):
        sql = "SELECT COUNT(*) FROM events WHERE 1=1"
        args = []
        if actor is not None:
            sql += " AND actor=?"
            args.append(actor)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        with sqlite3.connect(self.db_path) as db:
            return db.execute(sql, args).fetchone()[0]

    def test_signed_join_and_say_persist_in_real_engine_without_socket(self):
        with patch.object(socket, "socket", side_effect=AssertionError("no socket may open")):
            self.assertIn("Welcome, Oak", self.dispatch(self.request())["text"])
            say = self.request("/api/v1/command", command="say The lantern is lit.", action_id="a-1")
            self.assertIn("You say", self.dispatch(say)["text"])
        self.assertEqual(1, self.event_count("Oak", "say"))
        self.assertEqual("forest", self.gateway.world_id)

    def test_signature_tamper_wrong_body_and_replay_are_denied_before_effect(self):
        join = self.request()
        signed = self.signer.sign(join, now=110)
        forged = SignedForwarding({**signed.claims, "player": "Admin"}, signed.signature)
        with self.assertRaisesRegex(AdmissionDenied, "denied"):
            self.backend.dispatch(join, forged, now=110)
        different = self.request(player="Admin")
        with self.assertRaises(AdmissionDenied):
            self.backend.dispatch(different, signed, now=110)
        self.assertEqual(0, self.event_count("Admin"))
        self.backend.dispatch(join, signed, now=110)
        with self.assertRaises(AdmissionDenied):
            self.backend.dispatch(join, signed, now=110)
        self.assertEqual(1, self.event_count("Oak", "join"))

    def test_distinct_envelopes_same_action_receipt_do_not_duplicate_speech(self):
        self.dispatch(self.request())
        request = self.request("/api/v1/command", command="say One bell.", action_id="same-id")
        first = self.dispatch(request)
        second = self.dispatch(request)
        self.assertEqual(first["text"], second["text"])
        self.assertEqual(1, self.event_count("Oak", "say"))
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM action_receipts WHERE actor='Oak' AND action_id='same-id'",
            ).fetchone()[0])

    def test_mutation_requires_durable_action_id_and_survives_backend_replay_restart(self):
        self.dispatch(self.request())
        missing = self.request("/api/v1/command", command="say A bell with no receipt.")
        missing_signed = self.signer.sign(missing, now=110)
        with self.assertRaises(AdmissionDenied):
            self.backend.dispatch(missing, missing_signed, now=110)
        for bad_action_id in (" ", "a ", " a"):
            with self.subTest(bad_action_id=repr(bad_action_id)):
                malformed = self.request("/api/v1/command", command="say No ambiguous receipt.",
                                         action_id=bad_action_id)
                isolated_signer = LocalEdgeSigner(self.policy, self.invitations, key=_TEST_KEY)
                malformed_signed = isolated_signer.sign(malformed, now=110)
                with self.assertRaises(AdmissionDenied):
                    self.backend.dispatch(malformed, malformed_signed, now=110)
        self.assertEqual(0, self.event_count("Oak", "say"))

        request = self.request("/api/v1/command", command="say One durable bell.", action_id="logical-bell-1")
        signed = self.signer.sign(request, now=110)
        first = self.backend.dispatch(request, signed, now=110)
        # An attacker replaying the *same* signed packet to a new backend
        # (same key/grant but empty process-local nonce cache) cannot create
        # another game effect: Engine's SQLite action receipt is durable.
        fresh_backend = LocalSignedBackend(self.gateway, self.policy, self.invitations, key=_TEST_KEY)
        second = fresh_backend.dispatch(request, signed, now=111)
        self.assertEqual(first["text"], second["text"])
        self.assertEqual(1, self.event_count("Oak", "say"))
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM action_receipts WHERE actor='Oak' AND action_id='logical-bell-1'",
            ).fetchone()[0])

    def test_token_rotation_or_revocation_invalidates_previously_signed_envelope(self):
        request = self.request()
        signed = self.signer.sign(request, now=110)
        with self.assertRaisesRegex(ValueError, "explicit rotation"):
            self.invitations.issue("oak", now=111)
        rotated = self.invitations.rotate("Oak", now=111)
        with self.assertRaises(AdmissionDenied):
            self.backend.dispatch(request, signed, now=112)
        fresh = self.request(token=rotated)
        self.assertIn("Welcome, Oak", self.dispatch(fresh, now=112)["text"])
        self.invitations.revoke("Oak")
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(fresh, now=113)
        self.assertEqual(1, self.event_count("Oak", "join"))

    def test_header_aliases_origin_limits_and_exhausted_budget(self):
        for name in (" X-Forwarded-For ", "X_Forwarded_For", "X-Forwarded-Port", "X-Tailscale-User-Login"):
            with self.subTest(name=name), self.assertRaises(AdmissionDenied):
                self.signer.sign(self.request(headers=((name, "admin"),)), now=110)
        with self.assertRaises(AdmissionDenied):
            changed = self.request()
            self.signer.sign(replace(changed, headers=(("Host", "other.example.test"), *changed.headers[1:])), now=110)
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(replace(self.request(), headers=(("Authorization", "Bearer " + self.token),)), now=110)
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(self.request(headers=(("Authorization", "Bearer overwritten"),)), now=110)
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(self.request(headers=(("Origin", "https://evil.invalid"),)), now=110)
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(replace(self.request(), body=b"x" * 4097), now=110)
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(self.request(headers=(("X-Description", "z" * 1000),)), now=110)
        for _ in range(3):
            self.signer.sign(self.request(), now=110)
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(self.request(), now=110)
        self.assertEqual(0, self.event_count())

    def test_cross_world_or_wrong_signing_key_never_dispatches(self):
        request = self.request()
        signed = self.signer.sign(request, now=110)
        other_gateway = Gateway(Path(self.temp.name) / "other.sqlite3", world_id="other")
        with self.assertRaises(ValueError):
            LocalSignedBackend(other_gateway, self.policy, self.invitations, key=_TEST_KEY)
        wrong_backend = LocalSignedBackend(self.gateway, self.policy, self.invitations, key=b"x" * 32)
        with self.assertRaises(AdmissionDenied):
            wrong_backend.dispatch(request, signed, now=110)
        with self.assertRaises(AdmissionDenied):
            self.backend.dispatch(request, signed, now=126)  # expired 15-second envelope
        self.assertEqual(0, self.event_count())

    def test_replay_cache_is_pruned_and_bounded_before_world_effect(self):
        self.backend._used_nonces["expired-entry"] = 109
        request = self.request()
        signed = self.signer.sign(request, now=110)
        self.backend.dispatch(request, signed, now=110)
        self.assertNotIn("expired-entry", self.backend._used_nonces)
        self.backend._used_nonces = {f"{n:032x}": 200 for n in range(4096)}
        another = self.signer.sign(request, now=110)
        with self.assertRaises(AdmissionDenied):
            self.backend.dispatch(request, another, now=110)
        self.assertEqual(1, self.event_count("Oak", "join"))

    def test_signed_reply_never_leaks_world_global_cursor_off_room(self):
        joined = self.dispatch(self.request())
        self.assertNotIn("cursor", joined)
        read = self.request("/api/v1/room", method="GET")
        before = self.dispatch(read)
        self.assertNotIn("cursor", before)
        self.gateway.join("FarAway")
        self.gateway.command("FarAway", "north", "far-move")
        self.gateway.command("FarAway", "say Hidden off-room activity.", "far-say")
        after = self.dispatch(read)
        self.assertEqual(before, after)

    def test_signed_speech_rejects_control_and_direction_override_text(self):
        self.dispatch(self.request())
        for payload in ("say hello\nAdmin", "say \x1b]52;c;clipboard", "say abc\u202edef"):
            with self.subTest(payload=repr(payload)):
                request = self.request("/api/v1/command", command=payload, action_id="unsafe-text")
                isolated_signer = LocalEdgeSigner(self.policy, self.invitations, key=_TEST_KEY)
                signed = isolated_signer.sign(request, now=110)
                with self.assertRaises(AdmissionDenied):
                    self.backend.dispatch(request, signed, now=110)
        self.assertEqual(0, self.event_count("Oak", "say"))

    def test_history_and_feed_denied_until_explicit_event_visibility_policy(self):
        # Current Gateway.feed after=0 DOES expose old same-room `say` to a
        # late-arriving actor; signed remote boundary must not forward it.
        self.gateway.join("Early")
        self.gateway.command("Early", "say A private past event.", "early-1")
        newcomer = self.invitations.issue("Latecomer", now=100, lifetime=120)
        self.dispatch(self.request(player="Latecomer", token=newcomer))
        exposed = self.gateway.feed("Latecomer", 0)
        self.assertTrue(any("private past event" in e["text"] for e in exposed["events"]))
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(self.request("/api/v1/feed", player="Latecomer", token=newcomer,
                                          method="GET"), now=110)
        with self.assertRaises(AdmissionDenied):
            self.signer.sign(self.request("/api/v1/map", player="Latecomer", token=newcomer,
                                          method="GET"), now=110)
        with self.assertRaises(AdmissionDenied):
            self.dispatch(self.request("/api/v1/command", player="Latecomer", token=newcomer,
                                       command="history 10", action_id="h-1"))
        self.assertEqual(0, self.event_count("Latecomer", "say"))


if __name__ == "__main__":
    unittest.main()
