"""Same valid actor bearer on two HTTP sockets, long-poll stale room response.

A returned room feed can be changed after its snapshot and audience checks,
during the host's scoped-presence refresh. Only ephemeral loopback networking,
synthetic tokens and disposable SQLite are used.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib import error,request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from realm.gateway import Gateway
from realm.event_stream import FeedWaitServer


class WaitingFeedEpochTest(unittest.TestCase):
    scoped = False

    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gateway=Gateway(
            Path(tmp.name)/"world.sqlite3",world_id="same_actor_wait_epoch",
            scoped_credentials=self.scoped,
            scoped_guard_path=Path(tmp.name)/"scoped-guard.json",
        )
        store=self.gateway.scoped_credentials if self.scoped else self.gateway.credentials
        self.alice=store.provision("Alice")
        self.bob=store.provision("Bob")
        self.server=FeedWaitServer(self.gateway,(),port=0)
        self.thread=threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval":0.02},daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.close)
        self.assertEqual(200,self.http(self.alice,"/api/v1/join",{"player":"Alice"})[0])
        self.assertEqual(200,self.http(self.bob,"/api/v1/join",{"player":"Bob"})[0])
        self.secret="SECRET_SPOKEN_WHEN_ALICE_IN_ATRIUM"
        self.assertEqual(200,self.http(self.bob,"/api/v1/command",{
            "command":"say "+self.secret,"action_id":"bob-private-old-room",
        })[0])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def http(self,token,path,body=None):
        headers={"Authorization":"Bearer "+token}
        data=None
        if body is not None:
            headers["Content-Type"]="application/json"
            data=json.dumps(body).encode()
        req=request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            headers=headers,data=data,
        )
        try:
            with request.urlopen(req,timeout=6) as response:
                return response.status,json.loads(response.read())
        except error.HTTPError as exc:
            with exc:
                return exc.code,json.loads(exc.read())

    def _run(self, *, move:bool, wait_ms:int):
        source_seen=threading.Event()
        wait_started=threading.Event()
        refresh_seen=threading.Event()
        release=threading.Event()
        self.addCleanup(release.set)
        original_feed=self.gateway.feed
        original_refresh=self.server.refresh_scoped_presence
        after_cursor = self.gateway.cursor() if wait_ms else 0
        expected_secret = self.secret + "_AFTER_REAL_WAIT" if wait_ms else self.secret

        def witness_feed(actor,after):
            result=original_feed(actor,after)
            if actor=="Alice":
                if expected_secret in json.dumps(result["events"]):
                    source_seen.set()
                elif wait_ms and not wait_started.is_set():
                    wait_started.set()
            return result
        def hold_final_refresh(principal,presented,capability):
            if capability=="feed:read" and principal=="Alice" and source_seen.is_set() and not refresh_seen.is_set():
                refresh_seen.set()
                if not release.wait(4):
                    raise RuntimeError("Wait response was not released")
            return original_refresh(principal,presented,capability)
        self.gateway.feed=witness_feed
        self.server.refresh_scoped_presence=hold_final_refresh
        response=[]
        first=threading.Thread(
            target=lambda:response.append(self.http(
                self.alice,f"/api/v1/feed/wait?after={after_cursor}&wait_ms={wait_ms}"
            )),daemon=True,
        )
        first.start()
        try:
            if wait_ms:
                # Prove the server really had an empty initial feed and
                # entered its poll loop before Bob creates a new event.
                self.assertTrue(wait_started.wait(3), "No actual wait cycle observed")
                bob_status, _ = self.http(self.bob, "/api/v1/command", {
                    "command": "say " + expected_secret,
                    "action_id": "bob-speech-after-wait-admission",
                })
                self.assertEqual(200, bob_status)
            self.assertTrue(source_seen.wait(3),"Old long-poll feed was not built")
            self.assertTrue(refresh_seen.wait(3),"Old request did not pause at final refresh")
            if move:
                status,changed=self.http(self.alice,"/api/v1/command",{
                    "command":"north","action_id":"second-wait-client-move",
                })
                self.assertIn("Corridor",changed["text"])
            else:
                status,changed=self.http(self.alice,"/api/v1/join",{"player":"Alice"})
                self.assertGreater(changed["room_entry_cursor"],0)
            self.assertEqual(200,status)
        finally:
            release.set()
            first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(1,len(response))
        status,payload=response[0]
        self.assertEqual(409,status,"Stale wait response must not send old-room event")
        self.assertNotIn(expected_secret,json.dumps(payload))
        # Fresh read after the same-actor move/rejoin excludes old speech.
        status,fresh=self.http(self.alice,"/api/v1/feed/wait?after=0&wait_ms=0")
        self.assertEqual(200,status)
        self.assertNotIn(expected_secret,json.dumps(fresh))

    def test_move_during_immediate_wait_response(self):
        self._run(move=True,wait_ms=0)

    def test_move_during_waiting_feed_response(self):
        self._run(move=True,wait_ms=1000)

    def test_rejoin_during_immediate_wait_response(self):
        self._run(move=False,wait_ms=0)

    def test_rejoin_during_waiting_feed_response(self):
        self._run(move=False,wait_ms=1000)


class ScopedWaitingFeedEpochTest(WaitingFeedEpochTest):
    scoped=True


if __name__=="__main__":
    unittest.main()
