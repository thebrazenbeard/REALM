"""Synthetic, source-only tests for the host-custodied Creator staging gate."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from experiments.realm_creator_ui.release_gate import (
    ReleaseGateError,
    stage_exact_worldpack,
)


KEY = b"a-test-only-host-owned-key-of-at-least-thirty-two-bytes!"
DOMAIN = b"REALM/CREATOR-RELEASE-STAGING/v1\x00"


def dump(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def fixture(revision=1, name="Test World", release_id="synthetic-release-1"):
    pack = {
        "schema": "realm.worldpack.v1", "world_id": "test_world", "name": name,
        "start_room": "deck", "rooms": [{"id": "deck", "name": "Deck",
                                   "description": "Synthetic source only", "exits": {}}],
        "items": [],
    }
    raw = dump(pack)
    manifest = dump({"schema": "realm.creator.source-manifest.v1", "sources": [
        {"source_id": "worldpack", "sha256": hashlib.sha256(raw).hexdigest()},
        {"source_id": "synthetic-notes", "sha256": "b" * 64},
    ]})
    payload = {
        "schema": "realm.creator.release-authorization.v1",
        "scope": "STAGE_ONLY", "release_id": release_id,
        "owner_id": "patrick", "key_id": "host-signing-key", "ledger_id": "test-ledger",
        "world_id": pack["world_id"], "revision": revision,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "semantic_sha256": hashlib.sha256(dump(pack)).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    approval = dump(payload)
    mac = hmac.new(KEY, DOMAIN + approval, hashlib.sha256).hexdigest()
    return raw, manifest, approval, mac


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "staging.sqlite"

    def stage(self, raw=None, manifest=None, approval=None, mac=None, **kwargs):
        original = fixture()
        config = {"expected_owner_id": "patrick", "expected_key_id": "host-signing-key",
                  "expected_ledger_id": "test-ledger", "host_signing_key": KEY}
        config.update(kwargs)
        return stage_exact_worldpack(
            self.ledger,
            original[0] if raw is None else raw,
            original[1] if manifest is None else manifest,
            original[2] if approval is None else approval,
            original[3] if mac is None else mac,
            **config,
        )

    def test_signature_bound_exact_digests_and_single_use(self):
        first = self.stage()
        self.assertEqual(first["status"], "STAGED_NOT_IMPORTED")
        self.assertFalse(first["unchanged"])
        self.assertFalse(first["world_imported"])
        self.assertEqual(first["owner_authority"], "HELD_UNVERIFIED")
        again = self.stage()
        self.assertTrue(again["unchanged"])
        with sqlite3.connect(self.ledger) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM creator_release_staging").fetchone()[0], 1)

    def test_forged_or_tampered_approval_cannot_create_ledger(self):
        raw, manifest, approval, mac = fixture()
        tampered = json.loads(approval)
        tampered["revision"] = 2
        for proposed, signature in ((dump(tampered), mac), (approval, "0" * 64)):
            with self.assertRaises(ReleaseGateError):
                self.stage(raw, manifest, proposed, signature)
        self.assertFalse(self.ledger.exists())

    def test_raw_worldpack_and_manifest_tamper_rejected(self):
        raw, manifest, approval, mac = fixture()
        with self.assertRaisesRegex(ReleaseGateError, "exact worldpack raw SHA"):
            self.stage(raw + b" ", manifest, approval, mac)
        with self.assertRaisesRegex(ReleaseGateError, "manifest"):
            self.stage(raw, manifest + b" ", approval, mac)
        self.assertFalse(self.ledger.exists())

    def test_semantic_hash_tamper_even_when_resigned(self):
        raw, manifest, approval, _ = fixture()
        tampered = json.loads(approval)
        tampered["semantic_sha256"] = "0" * 64
        approval = dump(tampered)
        mac = hmac.new(KEY, DOMAIN + approval, hashlib.sha256).hexdigest()
        with self.assertRaisesRegex(ReleaseGateError, "semantic_sha256"):
            self.stage(raw, manifest, approval, mac)

    def test_owner_and_host_key_id_exact_binding(self):
        raw, manifest, approval, mac = fixture()
        with self.assertRaisesRegex(ReleaseGateError, "owner"):
            self.stage(raw, manifest, approval, mac, expected_owner_id="different")
        with self.assertRaisesRegex(ReleaseGateError, "key_id"):
            self.stage(raw, manifest, approval, mac, expected_key_id="different")

    def test_untrusted_localhost_approval_flag_is_not_accepted(self):
        raw, manifest, approval, mac = fixture()
        changed = json.loads(approval)
        changed["approved"] = True
        new_approval = dump(changed)
        new_mac = hmac.new(KEY, DOMAIN + new_approval, hashlib.sha256).hexdigest()
        with self.assertRaisesRegex(ReleaseGateError, "fields"):
            self.stage(raw, manifest, new_approval, new_mac)

    def test_key_possession_only_stages_and_never_grants_owner_release(self):
        # An actor with the staging key can sign arbitrary synthetic proposals.
        # They cannot thereby establish the external owner's release authority.
        raw, manifest, approval, mac = fixture(name="Intruder's World")
        result = self.stage(raw, manifest, approval, mac)
        self.assertEqual(result["owner_authority"], "HELD_UNVERIFIED")
        self.assertEqual(result["canon_status"], "UNREVIEWED")
        self.assertFalse(result["release_permitted"])

    def test_altered_release_id_for_same_revision_fails_closed(self):
        self.stage()
        raw, manifest, approval, mac = fixture(release_id="other-release")
        with self.assertRaisesRegex(ReleaseGateError, "already staged"):
            self.stage(raw, manifest, approval, mac)

    def test_one_release_id_cannot_bind_another_revision(self):
        self.stage()
        raw, manifest, approval, mac = fixture(revision=2)
        with self.assertRaisesRegex(ReleaseGateError, "release_id"):
            self.stage(raw, manifest, approval, mac)

    def test_cross_ledger_context_replay_rejected(self):
        raw, manifest, approval, mac = fixture()
        with self.assertRaisesRegex(ReleaseGateError, "ledger_id"):
            self.stage(raw, manifest, approval, mac, expected_ledger_id="other-ledger")

    def test_replay_after_local_ledger_tamper_fails_closed(self):
        self.stage()
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE creator_release_staging SET raw_sha256=?", ("0" * 64,))
        with self.assertRaisesRegex(ReleaseGateError, "release_id"):
            self.stage()

    def test_even_correctly_signed_duplicate_authorization_keys_rejected(self):
        raw, manifest, approval, _ = fixture()
        duplicate = approval[:-1] + b',"revision":2}'
        signature = hmac.new(KEY, DOMAIN + duplicate, hashlib.sha256).hexdigest()
        with self.assertRaisesRegex(ReleaseGateError, "Duplicate JSON key"):
            self.stage(raw, manifest, duplicate, signature)

    def test_two_concurrent_releases_one_revision_only(self):
        first = fixture(release_id="first-release")
        second = fixture(release_id="second-release")
        def attempt(data):
            try:
                self.stage(*data)
            except ReleaseGateError:
                return "rejected"
            return "staged"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (first, second)))
        self.assertEqual(sorted(results), ["rejected", "staged"])

    def test_foreign_schema_and_memory_db_rejected(self):
        with sqlite3.connect(self.ledger) as db:
            db.execute("CREATE TABLE unrelated (id INTEGER)")
        with self.assertRaisesRegex(ReleaseGateError, "unexpected ledger schema"):
            self.stage()
        with self.assertRaises(ReleaseGateError):
            stage_exact_worldpack(":memory:", *fixture(),
                                  expected_owner_id="patrick", expected_key_id="host-signing-key",
                                  expected_ledger_id="test-ledger",
                                  host_signing_key=KEY)

    def test_manifest_duplicate_keys_and_source_ids_rejected(self):
        raw, manifest, approval, mac = fixture()
        bad = b'{"schema":"realm.creator.source-manifest.v1","sources":[],"sources":[]}'
        with self.assertRaises(ReleaseGateError):
            self.stage(raw, bad, approval, mac)
        manifest_obj = json.loads(manifest)
        manifest_obj["sources"].append(manifest_obj["sources"][0])
        with self.assertRaises(ReleaseGateError):
            self.stage(raw, dump(manifest_obj), approval, mac)

    def test_missing_host_key_fails_closed(self):
        with self.assertRaisesRegex(ReleaseGateError, "host signing key"):
            self.stage(host_signing_key=b"short")


if __name__ == "__main__":
    unittest.main()
