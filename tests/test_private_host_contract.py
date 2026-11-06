"""Offline adversarial *proposed ingress contract* tests; no sockets/tokens issued."""

from dataclasses import replace
import hashlib
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.private_host_contract import (
    AdmittedPlayer, AdmissionDenied, InvitationGrant, PrivateHostPolicy,
    RequestMetadata, TrustedEdgeEvidence, evaluate_private_admission,
)


_SYNTHETIC_TOKEN = "test-fixture-only-not-a-real-player-token-12345"
_SYNTHETIC_HASH = hashlib.sha256(_SYNTHETIC_TOKEN.encode("ascii")).hexdigest()


class PrivateHostContractTests(unittest.TestCase):
    def setUp(self):
        self.policy = PrivateHostPolicy(
            host_id="lappy-private-test", world_id="forest",
            https_origin="https://realm.example.test",
            allowed_browser_origins=frozenset({"https://client.example.test"}),
            max_request_bytes=4096, max_header_bytes=8192,
            max_requests_per_window=30,
        )
        self.grant = InvitationGrant(
            host_id="lappy-private-test", world_id="forest", player="Oak",
            token_sha256=_SYNTHETIC_HASH, expires_unix=2000,
            scopes=frozenset({"read", "write"}),
        )
        self.edge = TrustedEdgeEvidence(
            tls_verified=True, adapter_authenticated=True,
            host_id="lappy-private-test",
            https_origin="https://realm.example.test",
            client_supplied_headers={},
        )
        self.request = RequestMetadata(
            world_id="forest", authorization="Bearer " + _SYNTHETIC_TOKEN,
            method="POST", operation="write", origin="https://client.example.test",
            asserted_player="Oak", body_bytes=73, header_bytes=500,
            requests_in_window=0,
        )

    def admit(self, *, policy=None, grant=None, edge=None, request=None, now=1000):
        return evaluate_private_admission(
            policy or self.policy, grant or self.grant,
            edge or self.edge, request or self.request, now_unix=now,
        )

    def denial(self, *, expected, **overrides):
        with self.assertRaises(AdmissionDenied) as caught:
            self.admit(**overrides)
        self.assertEqual(expected, caught.exception.internal_reason)
        self.assertNotIn(_SYNTHETIC_TOKEN, str(caught.exception))

    def test_valid_synthetic_invitation_admits_exact_actor_without_network(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            accepted = self.admit()
        self.assertEqual(
            AdmittedPlayer("lappy-private-test", "forest", "Oak", "write"), accepted,
        )

    def test_transport_and_host_identity_require_trusted_edge_evidence(self):
        self.denial(expected="unverified-edge", edge=replace(self.edge, tls_verified=False))
        self.denial(expected="unverified-edge", edge=replace(self.edge, adapter_authenticated=False))
        self.denial(expected="wrong-host-identity", edge=replace(self.edge, host_id="attacker"))
        self.denial(expected="wrong-host-identity", edge=replace(self.edge, https_origin="https://wrong.example.test"))

    def test_world_binding_and_actor_claim_never_follow_untrusted_input(self):
        self.denial(expected="wrong-world-or-host-grant", request=replace(self.request, world_id="other"))
        self.denial(expected="wrong-world-or-host-grant", grant=replace(self.grant, world_id="other"))
        self.denial(expected="wrong-world-or-host-grant", grant=replace(self.grant, host_id="other"))
        self.denial(expected="actor-mismatch", request=replace(self.request, asserted_player="Admin"))
        self.assertEqual("Oak", self.admit(request=replace(self.request, asserted_player=None)).player)

    def test_forwarded_identity_and_origin_spoofing_fail_closed(self):
        for header in ("X-Forwarded-For", "X-Forwarded-Port", "Forwarded", "X-Real-IP", "X-Tailscale-User-Login", "X-Original-Host"):
            with self.subTest(header=header):
                self.denial(expected="untrusted-forwarded-identity", edge=replace(
                    self.edge, client_supplied_headers={header: "admin@evil.invalid"},
                ))
        self.denial(expected="origin-not-allowed", request=replace(
            self.request, origin="https://client.example.test.attacker.invalid",
        ))
        self.denial(expected="origin-not-allowed", request=replace(
            self.request, origin="http://client.example.test",
        ))
        # Native clients may omit Origin, but a valid scoped bearer remains mandatory.
        self.assertEqual("Oak", self.admit(request=replace(self.request, origin=None)).player)

    def test_scopes_method_size_and_rate_limits(self):
        self.denial(expected="method-or-scope-not-allowed", grant=replace(
            self.grant, scopes=frozenset({"read"}),
        ))
        self.denial(expected="method-or-scope-not-allowed", request=replace(
            self.request, method="GET",
        ))
        self.denial(expected="method-or-scope-not-allowed", request=replace(
            self.request, operation="admin",
        ))
        self.denial(expected="request-size-limit", request=replace(
            self.request, body_bytes=4097,
        ))
        self.denial(expected="request-size-limit", request=replace(
            self.request, header_bytes=8193,
        ))
        self.denial(expected="rate-limit", request=replace(
            self.request, requests_in_window=30,
        ))
        self.denial(expected="rate-limit", request=replace(
            self.request, requests_in_window=-1,
        ))

    def test_revocation_expiry_and_synthetic_token_mismatch(self):
        self.denial(expected="revoked-or-expired", grant=replace(self.grant, revoked=True))
        self.denial(expected="revoked-or-expired", now=2000)
        self.denial(expected="token-mismatch", request=replace(
            self.request, authorization="Bearer totally-other-synthetic-token-xyz",
        ))
        self.denial(expected="invalid-token-shape", request=replace(
            self.request, authorization="Basic " + _SYNTHETIC_TOKEN,
        ))

    def test_invalid_operator_policy_fails_before_admission(self):
        for origin in ("http://realm.example.test", "https://realm.example.test/path", "https://user:pass@realm.example.test"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                replace(self.policy, https_origin=origin)
        with self.assertRaises(ValueError):
            replace(self.policy, max_requests_per_window=0)


if __name__ == "__main__":
    unittest.main()
