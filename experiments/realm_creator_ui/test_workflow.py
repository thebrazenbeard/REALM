"""Black-box synthetic HTTP workflow for the standalone, loopback Creator UI.

No private source, real world database, service installation, or host approval is
involved. Acknowledged JSON review is intentionally NOT authenticated approval.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from experiments.realm_creator_ui.server import make_server
from realm.worldpack import read_worldpack

_SYNTHETIC = (
    b"# Synthetic source\nNot canon, and not an imported world.\n"
    b"```markdown\n## Location: Phantom\nThis is only a code example.\n```\n"
    b"## Location: Copper Harbor\nAn old harbor with copper rails.\n"
    b"## Location: Glasswood\nA grove with glassy leaves.\n"
)


def _source_record(source: bytes = _SYNTHETIC) -> dict:
    return {
        "name": "synthetic-atlas.md",
        "content_base64": base64.b64encode(source).decode("ascii"),
    }


def _approval(draft: dict, region: dict) -> dict:
    return {
        "schema": "realm.creator.approval.v1",
        "draft_sha256": draft["sha256"],
        "world_id": draft["world_id"],
        "name": draft["name"],
        "start_room": region["rooms"][0]["id"],
        "rooms": [
            {
                "id": room["id"],
                "name": room["name"],
                "description": "Reviewed fictional test description: " + room["name"],
                "exits": {
                    direction: exit_candidate["to"]
                    for direction, exit_candidate in room["exits"].items()
                },
                "source_proposal_ids": [room["name_evidence"]["proposal_id"]],
            }
            for room in region["rooms"]
        ],
        "items": [],
        "acknowledged_review": True,
    }


class CreatorHTTPWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = make_server(port=0, token="synthetic-workflow-test-token")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post(self, path: str, value: object, *, token: bool = True,
              origin: str | None = "correct", host: str | None = None) -> tuple[int, dict]:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Host": host or f"127.0.0.1:{self.port}",
        }
        if origin == "correct":
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            headers["Origin"] = origin
        if token:
            headers["X-REALM-CREATOR-TOKEN"] = "synthetic-workflow-test-token"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("POST", path, body=raw, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {"_invalid_json_body": payload[:160].decode(
                    "utf-8", errors="replace")}
            return response.status, decoded
        finally:
            connection.close()

    def _preview(self, source: bytes = _SYNTHETIC) -> tuple[dict, dict, dict]:
        request = {
            "world_id": "creator_http_fixture",
            "name": "Synthetic Creator Test",
            "sources": [_source_record(source)],
        }
        status, preview = self._post("/api/v1/creator/preview", request)
        self.assertEqual(status, 200, preview)
        return request, preview["draft"], preview["region"]

    def test_preview_then_manual_review_compiles_unimported_worldpack(self) -> None:
        request, draft, region = self._preview()
        self.assertEqual([room["name"] for room in region["rooms"]],
                         ["Copper Harbor", "Glasswood"])
        self.assertEqual(region["status"], "REVIEW_REQUIRED")
        self.assertEqual(region["canon_status"], "UNREVIEWED")
        self.assertEqual(region["topology_evidence"], "NOT_SOURCE_ESTABLISHED")
        self.assertIsNone(region["worldpack"])
        self.assertFalse(region["world_imported"])
        self.assertFalse(region["approval_authenticated"])

        approval = _approval(draft, region)
        status, response = self._post("/api/v1/creator/compile",
                                      {**request, "draft": draft, "approval": approval})
        self.assertEqual(status, 200, response)
        compiled = response["compiled"]
        canonical_json = response["worldpack_json"]
        self.assertIsInstance(canonical_json, str)
        raw = canonical_json.encode("utf-8")
        self.assertLessEqual(len(raw), 524288)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         compiled["worldpack_sha256"])
        self.assertEqual(json.loads(canonical_json), compiled["worldpack"])
        self.assertFalse(compiled["world_imported"])
        self.assertFalse(compiled["approval_authenticated"])
        self.assertEqual(compiled["lineage"]["semantic_relation"], "NOT_ENTAILMENT")
        with tempfile.TemporaryDirectory(prefix="realm_creator_ui_http_test_") as td:
            pack_path = Path(td) / "unimported-synthetic-worldpack.json"
            pack_path.write_bytes(raw)
            parsed, digest = read_worldpack(pack_path)
            self.assertEqual(digest, compiled["worldpack_sha256"])
            self.assertEqual(parsed["world_id"], request["world_id"])
        # In particular, the workflow did not import a live world.

    def test_original_source_is_required_when_draft_is_submitted(self) -> None:
        request, draft, region = self._preview()
        review = _approval(draft, region)
        changed_source = (b"## Location: Copper Harbor\n"
                          b"Different source after initial preview.\n")
        forged = {**request, "sources": [_source_record(changed_source)],
                  "draft": draft, "approval": review}
        status, response = self._post("/api/v1/creator/compile", forged)
        self.assertGreaterEqual(status, 400, response)
        self.assertLess(status, 500, response)
        self.assertNotIn("worldpack_json", response)

        # Even a self-rehashed persisted draft is not proof of original bytes.
        altered = copy.deepcopy(draft)
        altered["proposals"][0]["excerpt"] = "Replaced despite matching digest"
        unsigned = {key: value for key, value in altered.items() if key != "sha256"}
        altered["sha256"] = hashlib.sha256(json.dumps(
            unsigned, sort_keys=True, ensure_ascii=False, allow_nan=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        status, response = self._post("/api/v1/creator/compile",
                                      {**request, "draft": altered, "approval": review})
        self.assertGreaterEqual(status, 400, response)
        self.assertNotIn("worldpack_json", response)

    def test_token_and_origin_cannot_be_bypassed_by_review_checkbox(self) -> None:
        request, draft, region = self._preview()
        compiled_request = {**request, "draft": draft, "approval": _approval(draft, region)}
        status, _ = self._post("/api/v1/creator/compile", compiled_request, token=False)
        self.assertIn(status, (401, 403))
        status, _ = self._post("/api/v1/creator/compile", compiled_request,
                               origin="https://evil.example")
        self.assertIn(status, (400, 401, 403))
        status, _ = self._post("/api/v1/creator/compile", compiled_request,
                               host="evil.example")
        self.assertIn(status, (400, 401, 403))

        unacknowledged = copy.deepcopy(compiled_request)
        unacknowledged["approval"]["acknowledged_review"] = False
        status, response = self._post("/api/v1/creator/compile", unacknowledged)
        self.assertGreaterEqual(status, 400, response)
        self.assertNotIn("worldpack_json", response)

    def test_oversize_or_bidi_compiled_output_is_not_exported(self) -> None:
        request, draft, region = self._preview()
        oversized = _approval(draft, region)
        source_id = region["rooms"][0]["name_evidence"]["proposal_id"]
        oversized["rooms"] = [{
            "id": f"room_{index}",
            "name": f"Room {index}",
            "description": "A" * 1500,
            "exits": {},
            "source_proposal_ids": [source_id],
        } for index in range(512)]
        oversized["start_room"] = "room_0"
        status, response = self._post("/api/v1/creator/compile",
                                      {**request, "draft": draft, "approval": oversized})
        self.assertGreaterEqual(status, 400, response)
        self.assertNotIn("worldpack_json", response)

        unsafe = _approval(draft, region)
        unsafe["rooms"][0]["description"] = "Fictitious display override \u202e exploit"
        status, response = self._post("/api/v1/creator/compile",
                                      {**request, "draft": draft, "approval": unsafe})
        self.assertGreaterEqual(status, 400, response)
        self.assertNotIn("worldpack_json", response)


if __name__ == "__main__":
    unittest.main()
