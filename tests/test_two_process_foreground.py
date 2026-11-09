"""Exercise one foreground world with independent HTTP and Telnet OS clients.

This is loopback-only synthetic transport qualification. The Telnet shared
password is a local access gate, not proof of a player's durable identity.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.engine import Engine
from realm.gateway import Gateway, GatewayServer
from realm.local_host_runner import run_local_host
from realm.telnet import LocalTelnetServer
from realm.world_clock import WorldClock


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

HTTP_CLIENT = r'''
import json, sys, time
from urllib import request, error
port, token = int(sys.argv[1]), sys.argv[2]
def call(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=2) as r:
            return r.status, json.load(r)
    except error.HTTPError as r:
        return r.code, json.loads(r.read())
joined = call("/api/v1/join", {"player": "Captain"})
said = call("/api/v1/command", {"command": "say HTTP process online",
                               "action_id": "process-http-once"})
denied = call("/api/v1/ship/depart", {"ship_id": "test_ship", "destination": "moon",
                                    "action_id": "unauthorized-ship"})
deadline = time.monotonic() + 6
seen = []
while time.monotonic() < deadline:
    status, feed = call("/api/v1/feed?after=0")
    if status != 200:
        break
    seen = [e["text"] for e in feed["events"]]
    if "Navigator: Telnet process online" in seen and "ClockBot: Host UTC tick" in seen:
        break
    time.sleep(.025)
print(json.dumps({"joined": joined[0], "said": said[0], "denied": denied[0],
                  "events": seen}))
'''

TELNET_CLIENT = r'''
import json, socket, sys
port = int(sys.argv[1])
with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
    s.settimeout(3)
    def until(marker):
        data = bytearray()
        while marker not in data:
            part = s.recv(4096)
            if not part:
                raise RuntimeError("Premature Telnet close before %r" % marker)
            data.extend(part)
        return bytes(data)
    until(b"Token: ")
    s.sendall(b"local-only-two-process\n")
    until(b"Name: ")
    s.sendall(b"Navigator\n")
    until(b"REALM>")
    s.sendall(b"say Telnet process online\n")
    output = until(b"REALM>")
print(json.dumps({"said": b"You say" in output}))
'''


class TwoIndependentProcessForegroundTests(unittest.TestCase):
    def test_http_and_telnet_os_clients_share_once_only_host_utc_history(self):
        with tempfile.TemporaryDirectory(prefix="realm_process_transport_") as tmp:
            db_path = Path(tmp) / "world.sqlite3"
            gateway = Gateway(db_path, world_id="two_process", scoped_credentials=True)
            gateway.join("Captain")
            token = gateway.scoped_credentials.provision(
                "Captain", capabilities=("world:join", "feed:read", "command:write")
            )
            clock = WorldClock(db_path)
            clock.enable()
            clock.schedule("clock-once", due_at=NOW, kind="host_say", payload={})
            http = GatewayServer(gateway, (), port=0)
            telnet = LocalTelnetServer(Engine(db_path), "local-only-two-process", port=0)
            stop = threading.Event()
            outcomes = []

            def say_on_tick(db, event):
                db.execute(
                    "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    (NOW.isoformat(), "ClockBot", "atrium", "say",
                     json.dumps({"text": "Host UTC tick"})),
                )

            def host():
                try:
                    outcomes.append(run_local_host(
                        http, telnet, clock, {"host_say": say_on_tick},
                        pulses=100, interval_seconds=.05, now=lambda: NOW,
                        stop_event=stop,
                    ))
                except BaseException as exc:
                    outcomes.append(exc)

            runner = threading.Thread(target=host)
            runner.start()
            processes = []
            try:
                for _ in range(150):
                    try:
                        with request.urlopen(
                            f"http://127.0.0.1:{http.server_port}/api/v1/health",
                            timeout=.5,
                        ):
                            break
                    except OSError:
                        time.sleep(.01)
                else:
                    self.fail("Foreground HTTP listener did not start")
                for source, args in (
                    (HTTP_CLIENT, [str(http.server_port), token]),
                    (TELNET_CLIENT, [str(telnet.server_address[1])]),
                ):
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", source, *args],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    ))
                self.assertEqual(3, len({os.getpid(), *(p.pid for p in processes)}))
                results = []
                for child in processes:
                    out, err = child.communicate(timeout=12)
                    self.assertEqual(0, child.returncode, err)
                    results.append(json.loads(out))
                http_result, telnet_result = results
                self.assertEqual(200, http_result["joined"], http_result)
                self.assertEqual(200, http_result["said"])
                self.assertEqual(401, http_result["denied"])
                self.assertTrue(telnet_result["said"])
                self.assertEqual(1, http_result["events"].count(
                    "Navigator: Telnet process online"))
                self.assertEqual(1, http_result["events"].count(
                    "ClockBot: Host UTC tick"))
                with closing(sqlite3.connect(db_path)) as db:
                    self.assertEqual(1, db.execute(
                        "SELECT COUNT(*) FROM clock_events WHERE event_id='clock-once' "
                        "AND processed_utc IS NOT NULL"
                    ).fetchone()[0])
                    # A denied ship request must not initialize optional ship
                    # geography in this setting-neutral HTTP/Telnet world.
                    self.assertIsNone(db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='geo_ship_events'"
                    ).fetchone())
                    self.assertEqual(1, db.execute(
                        "SELECT COUNT(*) FROM events WHERE actor='ClockBot' AND kind='say'"
                    ).fetchone()[0])
            finally:
                stop.set()
                for child in processes:
                    if child.poll() is None:
                        child.kill()
                    child.communicate()
                runner.join(timeout=5)
                http.server_close()
                telnet.server_close()
            self.assertFalse(runner.is_alive())
            self.assertEqual(1, len(outcomes))
            self.assertNotIsInstance(outcomes[0], BaseException)
            self.assertEqual(("clock-once",), outcomes[0].event_ids)
            self.assertEqual([], clock.tick(NOW, lambda db, event: self.fail(
                "Committed UTC event must not replay")))
            for port in (http.server_port, telnet.server_address[1]):
                with self.assertRaises(OSError):
                    with closing(socket.create_connection(("127.0.0.1", port), timeout=.2)):
                        pass
