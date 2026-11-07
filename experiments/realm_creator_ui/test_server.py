"""Real loopback HTTP contract tests for an UNIMPORTED Creator review app."""

import base64
import hashlib
import http.client
import json
import threading
import unittest

from experiments.realm_creator_ui.server import make_server


TEST_BYTES = b"# Atlas\n## Location: Copper Port\nA synthetic pier.\n"


def sample_request():
    return {
        "world_id": "creator_probe",
        "name": "Creator Probe",
        "sources": [{"name": "test.md", "content_base64": base64.b64encode(TEST_BYTES).decode("ascii")}],
    }


class CreatorServerHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(port=0, token="test-local-creator-token")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path="/api/v1/creator/preview", payload=None, *,
                method="POST", extra_headers=None, raw=None):
        body = (json.dumps(sample_request() if payload is None else payload).encode("utf-8")
                if raw is None else raw)
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Host": f"127.0.0.1:{self.port}",
            "X-REALM-CREATOR-TOKEN": "test-local-creator-token",
        }
        if extra_headers:
            headers.update(extra_headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        try:
            conn.request(method, path, body if method == "POST" else None, headers)
            response = conn.getresponse()
            raw_output = response.read()
            result = json.loads(raw_output) if response.getheader("Content-Type", "").startswith("application/json") else raw_output
            return response.status, result, dict(response.getheaders())
        finally:
            conn.close()

    def test_preview_returns_bound_unreviewed_region_without_import(self):
        status, result, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(result["draft"]["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["region"]["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["region"]["rooms"][0]["name"], "Copper Port")
        self.assertFalse(result["region"]["approval_authenticated"])
        self.assertFalse(result["region"]["live_world_modified"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_existing_worldpack_http_preview_is_lossless_and_unapproved(self):
        from experiments.realm_creator_ui.test_existing_pack import PACK
        raw = json.dumps(PACK, ensure_ascii=False).encode("utf-8")
        payload = {
            "source_id": "local_existing_pack",
            "worldpack_base64": base64.b64encode(raw).decode("ascii"),
        }
        status, result, headers = self.request("/api/v1/creator/existing-preview", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["preview"]["worldpack"], PACK)
        self.assertEqual(result["counts"], {"rooms": 2, "items": 1, "directed_exits": 2})
        self.assertEqual(result["preview"]["provenance"]["source_sha256"],
                         hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["preview"]["canon_status"], "UNREVIEWED")
        self.assertFalse(result["preview"]["approval_authenticated"])
        self.assertFalse(result["preview"]["world_imported"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_existing_worldpack_upload_rejects_malformed_and_cross_origin(self):
        from experiments.realm_creator_ui.test_existing_pack import PACK
        raw = json.dumps(PACK).encode("utf-8")
        payload = {"source_id": "local_existing_pack",
                   "worldpack_base64": base64.b64encode(raw).decode("ascii")}
        status, _, _ = self.request("/api/v1/creator/existing-preview", payload,
                                    extra_headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        bad_cases = [
            {**payload, "worldpack_base64": "!not-base64!"},
            {**payload, "claimed_approval": True},
            {**payload, "worldpack_base64": base64.b64encode(b"not json").decode()},
        ]
        for bad in bad_cases:
            with self.subTest(bad=bad):
                status, _, _ = self.request("/api/v1/creator/existing-preview", bad)
                self.assertEqual(status, 400)
        oversized = {**payload, "worldpack_base64":
                     base64.b64encode(b"x" * (512 * 1024 + 1)).decode("ascii")}
        status, _, _ = self.request("/api/v1/creator/existing-preview", oversized)
        self.assertEqual(status, 413)
        duplicate = (
            b'{"source_id":"local_existing_pack","source_id":"forged",'
            b'"worldpack_base64":"' + payload["worldpack_base64"].encode("ascii") + b'"}'
        )
        status, _, _ = self.request("/api/v1/creator/existing-preview", raw=duplicate)
        self.assertEqual(status, 400)
        status, _, _ = self.request("/api/v1/creator/existing-preview",
                                    raw=b'{"source_id":' + b"9" * 6000 + b'}')
        self.assertEqual(status, 400)

    def test_rejects_missing_or_wrong_host_token_and_origin(self):
        cases = [
            {"X-REALM-CREATOR-TOKEN": "not-authorized"},
            {"X-REALM-CREATOR-TOKEN": "\u00e9"},
            {"X-REALM-CREATOR-TOKEN": ""},
            {"Origin": "http://evil.example"},
            {"Origin": ""},
            {"Host": "evil.example"},
        ]
        for modified in cases:
            with self.subTest(modified=modified):
                status, _, _ = self.request(extra_headers=modified)
                self.assertEqual(status, 403)

    def test_rejects_untrusted_bytes_without_file_persistence(self):
        cases = [
            ({"Content-Type": "text/plain"}, json.dumps(sample_request()).encode()),
            ({}, b"{broken-json"),
            ({}, json.dumps({**sample_request(), "sources": [{"name": "notes.md", "content_base64": "!invalid!"}]}).encode()),
            ({}, b"x" * (1536 * 1024 + 1)),
        ]
        for headers, raw in cases:
            with self.subTest(headers=headers, bytes=len(raw)):
                status, _, _ = self.request(extra_headers=headers, raw=raw)
                self.assertIn(status, {400, 413, 415})

    def _reviewed_payload(self):
        status, result, _ = self.request()
        self.assertEqual(status, 200)
        draft, region = result["draft"], result["region"]
        room = region["rooms"][0]
        approved = {
            "schema": "realm.creator.approval.v1",
            "draft_sha256": draft["sha256"],
            "world_id": draft["world_id"],
            "name": draft["name"],
            "start_room": room["id"],
            "rooms": [{
                "id": room["id"],
                "name": room["name"],
                "description": "Host reviewed a synthetic copper-sided port.",
                "exits": {},
                "source_proposal_ids": [room["name_evidence"]["proposal_id"]],
            }],
            "items": [],
            "acknowledged_review": True,
        }
        return {**sample_request(), "draft": draft, "approval": approved}

    def test_compilation_stays_unimported_and_is_exact_download_bytes(self):
        payload = self._reviewed_payload()
        status, result, _ = self.request("/api/v1/creator/compile", payload)
        self.assertEqual(status, 200)
        compiled = result["compiled"]
        self.assertFalse(compiled["world_imported"])
        self.assertFalse(compiled["approval_authenticated"])
        encoded = result["worldpack_json"].encode("utf-8")
        self.assertLessEqual(len(encoded), 512 * 1024)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), compiled["worldpack_sha256"])
        self.assertEqual(json.loads(encoded), compiled["worldpack"])

    def test_compilation_allows_valid_multiline_description(self):
        payload = self._reviewed_payload()
        payload["approval"]["rooms"][0]["description"] = "First line\nSecond line"
        status, result, _ = self.request("/api/v1/creator/compile", payload)
        self.assertEqual(status, 200)
        self.assertIn("First line\\nSecond line", result["worldpack_json"])

    def test_compile_rejects_forged_source_revision_and_bidi(self):
        payload = self._reviewed_payload()
        payload["draft"]["name"] = "Tampered"
        status, _, _ = self.request("/api/v1/creator/compile", payload)
        self.assertEqual(status, 400)
        payload = self._reviewed_payload()
        payload["approval"]["rooms"][0]["description"] = "Bidi \u202e spoof"
        status, _, _ = self.request("/api/v1/creator/compile", payload)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
