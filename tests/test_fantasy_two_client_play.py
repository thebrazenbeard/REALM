"""Cedarwatch is playable through real HTTP and Telnet OS-client processes.

No private fiction, remote listener, optional ship subsystem, or neural model
is required.  Telnet's shared password remains a LOCAL transport gate, not
player-bound authentication; only the HTTP client uses a scoped bearer.
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
from realm.worldpack import import_worldpack


WORLD = Path(__file__).resolve().parents[1] / "examples" / "cedarwatch-world.json"
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
        with request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except error.HTTPError as response:
        return response.code, json.loads(response.read())

joined = call("/api/v1/join", {"player": "Rowan"})
deadline = time.monotonic() + 5
seen_mira = False
while time.monotonic() < deadline:
    status, people = call("/api/v1/command", {"command": "who"})
    if status == 200 and "Mira" in people["text"]:
        seen_mira = True
        break
    time.sleep(.02)
said = call("/api/v1/command", {"command": "say Rowan greets Mira",
                                 "action_id": "fantasy-rowan-speech"})
retried = call("/api/v1/command", {"command": "say Rowan greets Mira",
                                    "action_id": "fantasy-rowan-speech"})
unconfigured_map = call("/api/v1/map")
ship_denied = call("/api/v1/ship/events?ship_id=nonexistent")
woods = call("/api/v1/command", {"command": "north", "action_id": "fantasy-wood"})
back = call("/api/v1/command", {"command": "south", "action_id": "fantasy-return"})
room = call("/api/v1/room")
seen = []
while time.monotonic() < deadline:
    status, feed = call("/api/v1/feed?after=0")
    if status != 200:
        break
    seen = [event["text"] for event in feed["events"]]
    if "Mira: Mira returned to the green" in seen:
        break
    time.sleep(.02)
print(json.dumps({
    "joined": joined, "seen_mira": seen_mira, "said": said, "retried": retried,
    "map": unconfigured_map, "ship": ship_denied, "woods": woods,
    "back": back, "room": room, "feed": seen,
}))
'''


TELNET_CLIENT = r'''
import json, socket, sys, time
port = int(sys.argv[1])
with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
    sock.settimeout(4)
    def until(marker):
        data = bytearray()
        while marker not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("Telnet disconnected before %r" % marker)
            data.extend(chunk)
        return data.decode("utf-8", errors="replace")
    until(b"Token: ")
    sock.sendall(b"local-fantasy-test-only\n")
    until(b"Name: ")
    sock.sendall(b"Mira\n")
    entered = until(b"REALM>")
    def command(line):
        sock.sendall(line.encode() + b"\n")
        return until(b"REALM>")
    first = command("say Mira is here")
    deadline = time.monotonic() + 5
    seen_rowan = False
    while time.monotonic() < deadline:
        who = command("who")
        if "Rowan" in who:
            seen_rowan = True
            break
        time.sleep(.02)
    greeting = command("say Mira greets Rowan")
    tavern = command("west")
    taken = command("take oak_staff")
    inventory = command("inventory")
    dropped = command("drop oak_staff")
    green = command("east")
    returned = command("say Mira returned to the green")
    sock.sendall(b"quit\n")
    goodbye = until(b"Goodbye")
print(json.dumps({
    "entered": entered, "first": first, "seen_rowan": seen_rowan,
    "greeting": greeting, "tavern": tavern, "taken": taken,
    "inventory": inventory, "dropped": dropped, "green": green,
    "returned": returned, "goodbye": goodbye,
}))
'''


class FantasyTwoClientPlayTests(unittest.TestCase):
    def test_cedarwatch_two_os_players_and_host_utc_without_ship_schema(self):
        with tempfile.TemporaryDirectory(prefix="realm_fantasy_play_") as directory:
            path = Path(directory) / "village.sqlite3"
            import_worldpack(path, WORLD)
            gateway = Gateway(path, world_id="cedarwatch-example", scoped_credentials=True)
            token = gateway.scoped_credentials.provision(
                "Rowan", capabilities=("world:join", "command:write", "room:read",
                                       "feed:read", "map:read")
            )
            clock = WorldClock(path)
            clock.enable()
            clock.schedule("fantasy-noon-bell", due_at=NOW, kind="bell_say", payload={})
            http = GatewayServer(gateway, (), port=0)
            telnet = LocalTelnetServer(Engine(path), "local-fantasy-test-only", port=0)
            stop = threading.Event()
            outcomes = []

            def bell_on_tick(db, event):
                db.execute(
                    "INSERT INTO events(occurred_at,actor,room_id,kind,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    (NOW.isoformat(), "Bellkeeper", "village_green", "say",
                     json.dumps({"text": "Noon bell"})),
                )

            def host():
                try:
                    outcomes.append(run_local_host(
                        http, telnet, clock, {"bell_say": bell_on_tick},
                        pulses=200, interval_seconds=.03, now=lambda: NOW,
                        stop_event=stop,
                    ))
                except BaseException as exc:
                    outcomes.append(exc)

            runner = threading.Thread(target=host)
            runner.start()
            children = []
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
                    self.fail("Cedarwatch HTTP listener not ready")
                for source, args in (
                    (HTTP_CLIENT, [str(http.server_port), token]),
                    (TELNET_CLIENT, [str(telnet.server_address[1])]),
                ):
                    children.append(subprocess.Popen(
                        [sys.executable, "-c", source, *args],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    ))
                self.assertEqual(3, len({os.getpid(), *(child.pid for child in children)}))
                result = []
                for child in children:
                    stdout, stderr = child.communicate(timeout=12)
                    self.assertEqual(0, child.returncode, stderr)
                    result.append(json.loads(stdout))
                browser, terminal = result
                self.assertEqual(200, browser["joined"][0], browser)
                self.assertTrue(browser["seen_mira"], browser)
                self.assertTrue(terminal["seen_rowan"], terminal)
                self.assertIn("Village Green", browser["joined"][1]["text"])
                self.assertIn("Village Green", terminal["entered"])
                self.assertEqual(200, browser["said"][0])
                self.assertEqual(browser["said"], browser["retried"])
                self.assertEqual(404, browser["map"][0])
                self.assertEqual(401, browser["ship"][0])
                self.assertIn("Woodland Path", browser["woods"][1]["text"])
                self.assertIn("Village Green", browser["back"][1]["text"])
                self.assertIn("Village Green", browser["room"][1]["text"])
                self.assertIn("The Lantern Room", terminal["tavern"])
                self.assertIn("You take Oak walking staff.", terminal["taken"])
                self.assertIn("Oak walking staff", terminal["inventory"])
                self.assertIn("You drop Oak walking staff.", terminal["dropped"])
                self.assertIn("Village Green", terminal["green"])
                self.assertIn("Goodbye", terminal["goodbye"])
                self.assertIn("Mira: Mira returned to the green", browser["feed"])
                with closing(sqlite3.connect(path)) as db:
                    # The UTC bell is durable even if emitted before Rowan's
                    # most recent return to the green. Entry-bound privacy
                    # permits observation only when it follows that arrival.
                    bell_id = db.execute(
                        "SELECT id FROM events WHERE actor='Bellkeeper' AND kind='say'"
                    ).fetchone()[0]
                    arrival_id = db.execute(
                        "SELECT MAX(id) FROM events WHERE actor='Rowan' "
                        "AND room_id='village_green' AND kind IN ('join','move')"
                    ).fetchone()[0]
                    self.assertEqual(
                        bell_id >= arrival_id,
                        "Bellkeeper: Noon bell" in browser["feed"],
                    )
                    schema = {r[0] for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertFalse(any(name.startswith(("geo_", "atm_", "ship_"))
                                         for name in schema), schema)
                    self.assertEqual(("tavern", None), db.execute(
                        "SELECT room_id, held_by FROM items WHERE id='oak_staff'").fetchone())
                    self.assertEqual(("village_green",), db.execute(
                        "SELECT room_id FROM players WHERE id='Rowan'").fetchone())
                    self.assertEqual(("village_green",), db.execute(
                        "SELECT room_id FROM players WHERE id='Mira'").fetchone())
                    self.assertEqual(1, db.execute(
                        "SELECT COUNT(*) FROM events WHERE actor='Rowan' AND kind='say'").fetchone()[0])
                    self.assertEqual(1, db.execute(
                        "SELECT COUNT(*) FROM clock_events WHERE event_id='fantasy-noon-bell' "
                        "AND processed_utc IS NOT NULL").fetchone()[0])
            finally:
                stop.set()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.communicate()
                runner.join(timeout=5)
                http.server_close()
                telnet.server_close()
            self.assertFalse(runner.is_alive())
            self.assertEqual(1, len(outcomes))
            self.assertNotIsInstance(outcomes[0], BaseException)
            self.assertEqual(("fantasy-noon-bell",), outcomes[0].event_ids)
            self.assertEqual([], clock.tick(NOW, lambda db, event: self.fail(
                "UTC bell must never replay")))


if __name__ == "__main__":
    unittest.main()
