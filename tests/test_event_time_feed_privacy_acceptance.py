"""UNMET private-multiplayer acceptance: room history must honor event-time sight.

These are intentionally *failing* tests against the current loopback gateway.
They identify a future multiplayer privacy release gate, not a regression in
PR33 ActorMemory: that module explicitly does not govern Gateway.feed or
Engine.history/listen. Synthetic dialogue only; no actual player secrets.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer


class EventTimeRoomPrivacyAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        gateway = Gateway(Path(temporary.name) / "private-fixture.sqlite3", world_id="sandbox")
        self.tokens = {actor: gateway.credentials.provision(actor) for actor in ("Early", "Late")}
        self.server = GatewayServer(gateway, (), port=0)
        thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        thread.start()

        def shutdown():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)

        self.addCleanup(shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _call(self, actor: str, route: str, body: dict | None = None) -> dict:
        headers = {"Authorization": "Bearer " + self.tokens[actor]}
        payload = None if body is None else json.dumps(body).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "application/json"
        client = request.Request(self.base + route, data=payload, headers=headers)
        try:
            with request.urlopen(client, timeout=3) as response:
                self.assertEqual(response.status, 200)
                return json.loads(response.read())
        except error.HTTPError as failure:
            with closing(failure):
                self.fail(f"Unexpected HTTP {failure.code} for authenticated {actor}")

    def _join(self, actor: str) -> None:
        self.assertTrue(self._call(actor, "/api/v1/join", {"player": actor})["ok"])

    def _command(self, actor: str, text: str, action_id: str) -> str:
        result = self._call(actor, "/api/v1/command", {
            "player": actor, "command": text, "action_id": action_id,
        })
        self.assertTrue(result["ok"])
        return result["text"]

    def test_late_join_does_not_read_prejoin_speech_through_authenticated_feed(self):
        marker = "PREJOIN_FIXTURE_ONLY_NOT_PRIVATE_DATA"
        self._join("Early")
        self._command("Early", "say " + marker, "early-speech-1")
        self._join("Late")

        # Current gateway filters by CURRENT room, not event-time visibility.
        # `after=0` is a legal authenticated replay request for the newcomer.
        feed = self._call("Late", "/api/v1/feed?" + parse.urlencode({
            "player": "Late", "after": 0,
        }))
        self.assertFalse(
            any(marker in event["text"] for event in feed["events"]),
            "Private-multiplayer release gate: late entrant received prejoin speech",
        )

    def test_later_arrival_does_not_replay_prior_speech_using_history_or_listen(self):
        marker = "PRIOR_TO_ROOM_ARRIVAL_FIXTURE_ONLY"
        self._join("Early")
        self._command("Early", "north", "early-move-1")
        self._command("Early", "say " + marker, "early-speech-2")
        self._join("Late")
        self._command("Late", "north", "late-move-1")

        for verb in ("history 50", "listen"):
            with self.subTest(verb=verb):
                text = self._command("Late", verb, "late-" + verb.replace(" ", "-"))
                self.assertNotIn(
                    marker, text,
                    f"Private-multiplayer release gate: {verb} revealed prearrival speech",
                )


if __name__ == "__main__":
    unittest.main()
