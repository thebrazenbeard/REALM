"""UNMET private-multiplayer acceptance: room history must honor event-time sight.

These are intentionally *failing* tests against the current loopback gateway.
They identify a future multiplayer privacy release gate, not a regression in
PR33 ActorMemory: that module explicitly does not govern Gateway.feed or
Engine.history/listen. Synthetic dialogue only; no actual player secrets.
"""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer
from realm.engine import _CORE_SCHEMA_SQL, _SEED_EXITS, _SEED_ITEMS, _SEED_ROOMS


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

    def test_later_join_can_observe_only_subsequent_room_events(self):
        self._join("Early")
        self._command("Early", "say BEFORE_LATE_JOIN", "pre-join")
        self._join("Late")
        self._command("Early", "say AFTER_LATE_JOIN", "post-join")
        feed = self._call("Late", "/api/v1/feed?after=0")
        utterances = " ".join(e["text"] for e in feed["events"])
        self.assertNotIn("BEFORE_LATE_JOIN", utterances)
        self.assertIn("AFTER_LATE_JOIN", utterances)
        self.assertEqual(
            list(range(1, len(feed["events"]) + 1)),
            [e["id"] for e in feed["events"]],
            "An actor's feed uses a contiguous personal sequence, not world IDs",
        )

    def test_room_departure_and_return_do_not_grant_unwitnessed_away_speech(self):
        self._join("Early")
        self._join("Late")
        self._command("Early", "north", "north-trip")
        self._command("Early", "say SECRET_IN_CORRIDOR", "corridor-speech")
        self._command("Early", "south", "south-trip")
        self._command("Early", "say SHARED_BACK_IN_ATRIUM", "atrium-speech")
        feed = self._call("Late", "/api/v1/feed?after=0")
        timeline = " ".join(e["text"] for e in feed["events"])
        self.assertIn("SHARED_BACK_IN_ATRIUM", timeline)
        self.assertNotIn("SECRET_IN_CORRIDOR", timeline)
        for verb in ("history 50", "listen"):
            self.assertNotIn(
                "SECRET_IN_CORRIDOR",
                self._command("Late", verb, f"late-{verb.replace(' ', '-')}-reentry"),
            )

    def test_global_activity_does_not_advance_uninvolved_player_cursor(self):
        self._join("Early")
        self._join("Late")
        self._command("Early", "north", "north-cursor")
        before = self._call("Late", "/api/v1/feed?after=0")["cursor"]
        self._command("Early", "say OUT_OF_SIGHT", "away-cursor")
        after = self._call("Late", "/api/v1/feed?after=0")["cursor"]
        self.assertEqual(before, after)
        self.assertNotIn(
            "OUT_OF_SIGHT",
            " ".join(e["text"] for e in self._call("Late", "/api/v1/feed?after=0")["events"]),
        )

    def test_command_and_room_do_not_expose_nonacknowledged_replay_cursors(self):
        self._join("Early")
        result = self._call("Early", "/api/v1/command", {
            "player": "Early", "command": "say CURSOR_CONTRACT", "action_id": "cursor-contract",
        })
        room = self._call("Early", "/api/v1/room?player=Early")
        self.assertNotIn("cursor", result)
        self.assertNotIn("cursor", room)
        feed = self._call("Early", "/api/v1/feed?after=0")
        self.assertIn("cursor", feed)

    def test_unknown_event_and_ungranted_legacy_event_fail_closed(self):
        self._join("Early")
        self._join("Late")
        db_path = self.server.gateway.engine.db_path
        with sqlite3.connect(db_path) as db:
            db.execute(
                "INSERT INTO events (occurred_at,actor,room_id,kind,payload_json) "
                "VALUES ('2000-01-01','Early','atrium','say',?)",
                (json.dumps({"text": "LEGACY_WITHOUT_GRANTS"}),),
            )
            self.server.gateway.engine._event(
                db, "Early", "atrium", "credential_snapshot", {"secret": "NOT_PUBLIC"}
            )
        content = " ".join(e["text"] for e in self._call("Late", "/api/v1/feed?after=0")["events"])
        self.assertNotIn("LEGACY_WITHOUT_GRANTS", content)
        self.assertNotIn("NOT_PUBLIC", content)
        self.assertNotIn("LEGACY_WITHOUT_GRANTS", self._command("Late", "history 50", "legacy-history"))

    def test_durable_revocation_suppresses_feed_and_history(self):
        self._join("Early")
        self._join("Late")
        self._command("Early", "say REVOKE_ME", "speech-revoke")
        initial = self._call("Late", "/api/v1/feed?after=0")
        spoken = next(event for event in initial["events"] if "REVOKE_ME" in event["text"])
        with sqlite3.connect(self.server.gateway.engine.db_path) as db:
            db.execute(
                "UPDATE event_deliveries SET revoked_at='2026-11-07' "
                "WHERE recipient='Late' AND seq=?",
                (spoken["id"],),
            )
        after = self._call("Late", "/api/v1/feed?after=0")
        self.assertFalse(any("REVOKE_ME" in event["text"] for event in after["events"]))
        self.assertNotIn("REVOKE_ME", self._command("Late", "listen", "listen-revoked"))

    def test_concurrent_host_writes_produce_one_contiguous_cursor_per_witness(self):
        self._join("Early")
        self._join("Late")
        engine = self.server.gateway.engine
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(
                lambda index: engine.act("Early", f"say concurrency-{index}", action_id=f"conc-{index}"),
                range(12),
            ))
        self.assertEqual(12, len(results))
        feed = self._call("Late", "/api/v1/feed?after=0")
        self.assertEqual(12, sum(event["kind"] == "say" for event in feed["events"]))
        self.assertEqual(
            list(range(1, len(feed["events"]) + 1)),
            [event["id"] for event in feed["events"]],
        )

    def test_existing_bound_world_upgrades_without_granting_old_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "legacy.sqlite3"
            with sqlite3.connect(db_path) as db:
                db.executescript(_CORE_SCHEMA_SQL)
                db.executemany("INSERT INTO rooms VALUES (?,?,?)", _SEED_ROOMS)
                db.executemany("INSERT INTO exits VALUES (?,?,?)", _SEED_EXITS)
                db.executemany(
                    "INSERT INTO items(id,name,description,room_id) VALUES (?,?,?,?)",
                    _SEED_ITEMS,
                )
                db.execute("INSERT INTO world_meta VALUES('world_id','sandbox')")
                db.execute("INSERT INTO players VALUES ('Early','atrium','2000-01-01')")
                db.execute(
                    "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                    "VALUES ('2000-01-01','Early','atrium','say',?)",
                    (json.dumps({"text": "LEGACY_UNWITNESSED"}),),
                )
            gateway = Gateway(db_path, world_id="sandbox")
            token = gateway.credentials.provision("Early")
            self.assertTrue(token)
            self.assertEqual([], gateway.feed("Early", 0)["events"])
            gateway.command("Early", "say NEW_VERIFIED_GRANT", "legacy-new-1")
            observed = gateway.feed("Early", 0)["events"]
            self.assertEqual([1], [event["id"] for event in observed])
            self.assertIn("NEW_VERIFIED_GRANT", observed[0]["text"])

    def test_join_reply_cannot_skip_interleaved_authorized_event_or_reconnect_backlog(self):
        self._join("Early")
        gateway = self.server.gateway
        original = gateway.engine.join

        def interleaved_join(player):
            response = original(player)
            if player == "Late":
                gateway.engine.act("Early", "say AUTHORIZED_BUT_INTERLEAVED", action_id="join-race-1")
            return response

        with patch.object(gateway.engine, "join", side_effect=interleaved_join):
            joined = gateway.join("Late")
        self.assertEqual(0, joined["cursor"], "Join must never ack unseen deliveries")
        delivered = gateway.feed("Late", joined["cursor"])["events"]
        self.assertTrue(any("AUTHORIZED_BUT_INTERLEAVED" in e["text"] for e in delivered))
        self.assertEqual(0, gateway.join("Late")["cursor"], "Reconnect must preserve backfill")

    def test_empty_feed_cannot_advance_past_write_interleaved_after_query(self):
        self._join("Early")
        self._join("Late")
        gateway = self.server.gateway
        start_cursor = gateway.cursor("Late")
        original_read = gateway._read
        triggered = []

        class CompletedRows:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class InterleavedRead:
            def __init__(self, db):
                self.db = db

            def execute(self, query, params=()):
                cursor = self.db.execute(query, params)
                if "FROM event_deliveries d JOIN events e" in query and not triggered:
                    rows = cursor.fetchall()  # Empty snapshot is already taken.
                    triggered.append(True)
                    gateway.engine.act("Early", "say COMMITTED_BETWEEN_READ_AND_REPLY",
                                       action_id="feed-race-1")
                    return CompletedRows(rows)
                return cursor

            def close(self):
                self.db.close()

        with patch.object(gateway, "_read", side_effect=lambda: InterleavedRead(original_read())):
            first = gateway.feed("Late", start_cursor)
        self.assertTrue(triggered)
        self.assertEqual([], first["events"])
        self.assertEqual(start_cursor, first["cursor"], "Never acknowledge unread concurrent writes")
        second = gateway.feed("Late", first["cursor"])
        self.assertTrue(any("COMMITTED_BETWEEN_READ_AND_REPLY" in e["text"] for e in second["events"]))


if __name__ == "__main__":
    unittest.main()
