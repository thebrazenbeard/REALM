"""Deferred red tests for PR30's *offline* private-ingress policy oracle.

This module has no network, proxy or token issuer. It skips on main (where
the opt-in private_host_contract is absent) and intentionally fails against
PR30 `934fbc7` until the contract validates strict trusted-edge booleans and
rejects identity-header aliases. Synthetic token only, never a real bearer.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import unittest


@unittest.skipUnless(
    importlib.util.find_spec("realm.private_host_contract") is not None,
    "Requires PR30 offline ingress contract",
)
class PrivateHostOracleHardeningContractTests(unittest.TestCase):
    def setUp(self):
        from realm.private_host_contract import (
            AdmissionDenied, InvitationGrant, PrivateHostPolicy,
            RequestMetadata, TrustedEdgeEvidence, evaluate_private_admission,
        )

        self.AdmissionDenied = AdmissionDenied
        self.evaluate = evaluate_private_admission
        token = "synthetic-admission-test-token-99999"
        self.policy = PrivateHostPolicy(
            host_id="host-one", world_id="sandbox",
            https_origin="https://private.example.test",
            allowed_browser_origins=frozenset(),
        )
        self.grant = InvitationGrant(
            host_id="host-one", world_id="sandbox", player="ActorOne",
            token_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_unix=2000, scopes=frozenset({"read"}),
        )
        self.edge = TrustedEdgeEvidence(
            tls_verified=True, adapter_authenticated=True,
            host_id="host-one", https_origin="https://private.example.test",
            client_supplied_headers={},
        )
        self.request = RequestMetadata(
            world_id="sandbox", authorization="Bearer " + token,
            method="GET", operation="read", origin=None,
            asserted_player="ActorOne", body_bytes=0, header_bytes=64,
            requests_in_window=0,
        )

    def _evaluate(self, edge):
        return self.evaluate(
            self.policy, self.grant, edge, self.request, now_unix=1000,
        )

    def test_untyped_truthy_edge_flags_must_not_assert_authenticated_tls(self):
        # Dataclass type annotations do not enforce runtime values. A host
        # adapter that decodes a JSON/string flag must fail closed here.
        for untrusted in ("false", "no", 1):
            with self.subTest(untrusted=repr(untrusted)):
                edge = replace(self.edge, tls_verified=untrusted, adapter_authenticated=untrusted)
                with self.assertRaises(self.AdmissionDenied):
                    self._evaluate(edge)

    def test_underscore_aliases_of_identity_headers_must_be_denied(self):
        # HTTP adapters/proxy stacks can normalize underscores to hyphens;
        # the policy must not bless aliases of forbidden identity headers.
        for header in ("X_Tailscale_User_Login", "X_Forwarded_For"):
            with self.subTest(header=header):
                edge = replace(self.edge, client_supplied_headers={
                    header: "attacker@example.invalid",
                })
                with self.assertRaises(self.AdmissionDenied):
                    self._evaluate(edge)


if __name__ == "__main__":
    unittest.main()
