"""Disposable REALM smoke with two independently authenticated OS clients.

Run from the repository root: python examples/synthetic_multiprocess_play.py
All networking remains on loopback. No installed service or private world is used.

The current player API exposes room events, not geo_ship_events. Therefore the
clients witness NPC gathering while the host verifies the UTC ship arrival in
SQLite; this fixture must not be called a client-visible ship-arrival feed.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.clock_runner import run_foreground
from realm.crafting import Crafting
from realm.event_stream import FeedWaitServer
from realm.gateway import Gateway
from realm.npc_game_theory import ResourceSite
from realm.npc_utc_policy import HostNpcGatherPolicy, make_host_npc_gather_handler
from realm.world_clock import WorldClock
from realm.worldpack import import_worldpack

UTC = timezone.utc
HOST_KEY = b"synthetic-multiprocess-test-key-only"


def _child_main() -> None:
    """Each persistent child owns one bearer and makes its own HTTP requests."""
    setup = json.loads(sys.stdin.readline())
    token, port = setup["token"], setup["port"]
    print(json.dumps({"pid": os.getpid(), "ready": True}), flush=True)
    for line in sys.stdin:
        query = json.loads(line)
        if query.get("stop"):
            print(json.dumps({"stopped": True}), flush=True)
            return
        payload = query.get("payload")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(
            f"http://127.0.0.1:{port}{query['path']}", data=body, headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                packet = {"status": response.status, "body": json.loads(response.read())}
        except error.HTTPError as exc:
            packet = {"status": exc.code, "body": json.loads(exc.read())}
        except Exception as exc:
            packet = {"error": type(exc).__name__ + ": " + str(exc)}
        print(json.dumps(packet), flush=True)


class _Client:
    def __init__(self, port: int, token: str):
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--client"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self.responses: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self._send({"port": port, "token": token})
            ready = self._receive()
            if not ready.get("ready") or ready.get("pid") != self.process.pid:
                raise RuntimeError("Client process did not establish a distinct session")
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=5)
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
            raise

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def _send(self, data: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(data) + "\n")
        self.process.stdin.flush()

    def _receive(self) -> dict:
        try:
            line = self.responses.get(timeout=10)
        except queue.Empty as exc:
            raise TimeoutError("Independent client did not answer within 10s") from exc
        if line is None:
            raise RuntimeError(f"Client exited unexpectedly: {self.process.poll()}")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def request(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        if self.process.poll() is not None:
            raise RuntimeError("Client process is no longer running")
        self._send({"path": path, "payload": payload})
        response = self._receive()
        return response["status"], response["body"]

    def close(self) -> int:
        if self.process.poll() is None:
            try:
                self._send({"stop": True})
                if not self._receive().get("stopped"):
                    raise RuntimeError("Client refused stop")
                self.process.wait(timeout=5)
            except (BrokenPipeError, OSError, RuntimeError, TimeoutError,
                    subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.reader.join(timeout=2)
        return self.process.returncode


def _expect(client: _Client, path: str, status: int = 200,
            payload: dict | None = None) -> dict:
    actual, value = client.request(path, payload)
    if actual != status or (status == 200 and not value.get("ok")):
        raise AssertionError(f"Expected HTTP {status}, received {actual}: {value}")
    return value


def _arrival(db: sqlite3.Connection, event, gateway: Gateway) -> None:
    if event.kind != "ship_arrival":
        raise ValueError("Unexpected scheduled event")
    gateway.geography.arrive_in_transaction(
        db, event.payload["ship_id"],
        now=int(event.due_at.timestamp()), action_id=event.event_id,
    )


def run_demo() -> dict:
    world_directory: Path | None = None
    with tempfile.TemporaryDirectory(prefix="realm_multiprocess_play_") as tmp:
        world_directory = Path(tmp)
        db_path = world_directory / "demo.sqlite3"
        pack_path = world_directory / "worldpack.json"
        pack_path.write_text(json.dumps({
            "schema": "realm.worldpack.v1", "world_id": "multiprocess_play",
            "name": "Disposable Multi-Process World", "start_room": "atrium",
            "rooms": [{"id": "atrium", "name": "Synthetic Atrium",
                       "description": "A disposable test room.", "exits": {}}],
            "items": [],
        }), encoding="utf-8")
        imported = import_worldpack(db_path, pack_path)
        gateway = Gateway(db_path, world_id="multiprocess_play", scoped_credentials=True)
        for actor in ("Captain", "Navigator", "Gatherer", "Scout"):
            gateway.join(actor)
        geo = gateway.geography
        geo.add_place("synthetic_universe", "Synthetic Universe", "universe")
        geo.add_place("synthetic_system", "Synthetic System", "system", "synthetic_universe")
        geo.add_place("origin", "Synthetic Origin", "planet", "synthetic_system")
        geo.add_place("destination", "Synthetic Destination", "planet", "synthetic_system")
        geo.link("origin", "destination", "ship", seconds=30)
        geo.register_ship("test_ship", "Synthetic Ship", "Captain", "origin")
        Crafting(db_path).place_node("atrium", "iron", stock=6, batch_yield=3)
        Crafting(db_path).place_node("atrium", "copper", stock=6, batch_yield=3)
        clock = WorldClock(db_path)
        clock.enable()
        captain_token = gateway.scoped_credentials.provision(
            "Captain", capabilities=("room:read", "feed:read", "command:write", "ship:depart")
        )
        navigator_token = gateway.scoped_credentials.provision(
            "Navigator", capabilities=("room:read", "feed:read", "command:write")
        )
        server = FeedWaitServer(gateway, (), port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        clients: list[_Client] = []
        exit_codes: list[int] = []
        try:
            captain = _Client(server.server_port, captain_token)
            clients.append(captain)
            navigator = _Client(server.server_port, navigator_token)
            clients.append(navigator)
            pids = [client.process.pid for client in clients]
            if len(set(pids + [os.getpid()])) != 3:
                raise AssertionError("Two clients must be distinct OS processes")
            for client in clients:
                _expect(client, "/api/v1/room")
            _expect(captain, "/api/v1/feed?player=Navigator", status=403)
            _expect(navigator, "/api/v1/ship/depart", status=401,
                    payload={"ship_id": "test_ship", "destination": "destination",
                             "action_id": "ungranted_depart"})
            cursor = gateway.cursor()
            departure = {"ship_id": "test_ship", "destination": "destination",
                         "action_id": "multiprocess_depart_1"}
            original = _expect(captain, "/api/v1/ship/depart", payload=departure)
            replay = _expect(captain, "/api/v1/ship/depart", payload=departure)
            if original != replay:
                raise AssertionError("Ship departure replay was not idempotent")

            npc_at = datetime.now(UTC) + timedelta(seconds=1)
            policy = HostNpcGatherPolicy(
                policy_id="multiprocess_policy", world_id="multiprocess_play",
                actor="Gatherer", opponent="Scout",
                sites=(ResourceSite("atrium", "iron"),
                       ResourceSite("atrium", "copper")),
                seed=7, event_id="multiprocess_npc_1",
                not_before_utc=npc_at - timedelta(seconds=1),
                expires_at_utc=npc_at + timedelta(minutes=10),
            )
            mac = hmac.new(
                HOST_KEY, b"REALM/NPC-HOST-PERMIT/v1\x00" + policy.signing_bytes(),
                hashlib.sha256,
            ).hexdigest()
            clock.schedule("multiprocess_npc_1", due_at=npc_at,
                           kind="npc_economic_gather",
                           payload={"policy_id": policy.policy_id,
                                    "world_id": "multiprocess_play"})
            handler = make_host_npc_gather_handler(
                engine=gateway.engine, crafting=Crafting(db_path),
                host_key=HOST_KEY,
                grants_by_event_id={"multiprocess_npc_1": (policy, mac)},
                now=lambda: npc_at,
            )
            report = run_foreground(
                clock, {"npc_economic_gather": handler}, pulses=1,
                now=lambda: npc_at, monotonic=lambda: 0.0,
                sleep=lambda _: None,
            )
            if report.event_ids != ("multiprocess_npc_1",):
                raise AssertionError("UTC NPC event not acknowledged exactly once")
            witnessed = {}
            for actor, client in (("Captain", captain), ("Navigator", navigator)):
                feed = _expect(client, f"/api/v1/feed/wait?after={cursor}&wait_ms=0")
                found = [(e["id"], e["kind"]) for e in feed["events"]
                         if e["actor"] == "Gatherer" and e["kind"] == "gather"]
                if len(found) != 1:
                    raise AssertionError(f"{actor} did not witness one NPC gather: {found}")
                witnessed[actor] = found[0][0]
                # A replayed prior cursor re-delivers; the consumed cursor does not.
                again = _expect(client, f"/api/v1/feed/wait?after={cursor}&wait_ms=0")
                if not any(e["id"] == found[0][0] for e in again["events"]):
                    raise AssertionError("At-least-once cursor replay was lost")
                empty = _expect(client, f"/api/v1/feed/wait?after={found[0][0]}&wait_ms=0")
                if empty["events"]:
                    raise AssertionError("Consumed cursor replayed a handled event")
            if len(set(witnessed.values())) != 1:
                raise AssertionError("Independent clients observed different NPC event IDs")

            with closing(sqlite3.connect(db_path)) as db:
                scheduled = db.execute(
                    "SELECT event_id,due_utc FROM clock_events WHERE kind='ship_arrival' "
                    "AND processed_utc IS NULL"
                ).fetchone()
            if scheduled is None:
                raise AssertionError("Ship arrival was not scheduled")
            arrival_at = datetime.fromisoformat(scheduled[1])
            processed = clock.tick(arrival_at, lambda db, e: _arrival(db, e, gateway))
            if [e.event_id for e in processed] != [scheduled[0]]:
                raise AssertionError("UTC arrival receipt missing")
            if clock.tick(arrival_at, lambda db, e: _arrival(db, e, gateway)):
                raise AssertionError("Ship arrival repeated after retry")
            ship = geo.ship_status("test_ship")
            if ship["state"] != "docked" or ship["planet"] != "destination":
                raise AssertionError("Ship did not arrive at the destination")
            with closing(sqlite3.connect(db_path)) as db:
                ship_events = dict(db.execute(
                    "SELECT kind,COUNT(*) FROM geo_ship_events GROUP BY kind"
                ).fetchall())
                arrival_epoch = db.execute(
                    "SELECT occurred_at FROM geo_ship_events WHERE kind='arrive'"
                ).fetchone()[0]
                clock_receipts = dict(db.execute(
                    "SELECT event_id,processed_utc FROM clock_events "
                    "WHERE event_id IN (?,?)",
                    ("multiprocess_npc_1", scheduled[0]),
                ).fetchall())
                receipts = {
                    "npc": db.execute(
                        "SELECT COUNT(*) FROM action_receipts WHERE actor='Gatherer'"
                    ).fetchone()[0],
                    "ship_arrival": db.execute(
                        "SELECT COUNT(*) FROM geo_ship_receipts WHERE actor='system'"
                    ).fetchone()[0],
                }
            if ship_events != {"depart": 1, "arrive": 1}:
                raise AssertionError(f"Unexpected ship event duplication: {ship_events}")
            if arrival_epoch != int(arrival_at.timestamp()):
                raise AssertionError("Ship arrival was not recorded at due UTC")
            if set(clock_receipts) != {"multiprocess_npc_1", scheduled[0]} or any(
                stamp is None or datetime.fromisoformat(stamp).utcoffset() != timedelta(0)
                for stamp in clock_receipts.values()
            ):
                raise AssertionError("NPC and ship clock receipts lack committed UTC")
            if receipts != {"npc": 1, "ship_arrival": 1}:
                raise AssertionError(f"Unexpected durable receipts: {receipts}")
            # No authorized ship-event read route exists yet: both clients can
            # read NPC room events, but cannot claim to witness geo_ship_events.
            for client in clients:
                after = _expect(client, f"/api/v1/feed/wait?after={witnessed['Captain']}&wait_ms=0")
                if any("ship" in e["kind"] for e in after["events"]):
                    raise AssertionError("Unexpected public ship feed projection")
            result = {
                "world": "multiprocess_play", "clients": ["Captain", "Navigator"],
                "client_process_ids": pids, "host_process_id": os.getpid(),
                "worldpack_sha256": imported["sha256"],
                "shared_npc_event_id": witnessed["Captain"],
                "ship_departed_by_captain": original["ok"],
                "ship_arrived_host_utc": ship["planet"],
                "committed_utc_clock_receipts": len(clock_receipts),
                "native_ship_feed_visible": False,
                "scope_checks": {"wrong_player": 403, "navigator_ship_depart": 401},
                "ship_events": ship_events, "receipts": receipts,
            }
        finally:
            for client in reversed(clients):
                exit_codes.append(client.close())
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        if sorted(exit_codes) != [0, 0] or thread.is_alive():
            raise AssertionError("Client or server did not shut down cleanly")
    if world_directory is None or world_directory.exists():
        raise AssertionError("Disposable world directory survived cleanup")
    result["client_exit_codes"] = sorted(exit_codes)
    result["temporary_world_cleanup"] = "verified"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.client:
        _child_main()
    else:
        print(json.dumps(run_demo(), indent=2, sort_keys=True))
