"""Two-client, disposable, source-only REALM foreground play smoke.

From the repository root: py -3 examples/synthetic_foreground_play.py
Add --interactive to issue ordinary commands as either local client afterward.
This imports no private lore, binds loopback only, and installs no service.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request

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
HOST_KEY = b"synthetic-local-demo-only-not-a-deployment-key"


def _http(port: int, token: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, headers=headers,
        method="POST" if body is not None else "GET",
    )
    with request.urlopen(req, timeout=5) as response:
        value = json.loads(response.read())
        if response.status != 200 or not value.get("ok"):
            raise RuntimeError(f"HTTP {response.status}: {value}")
        return value


def _arrival(db: sqlite3.Connection, event, gateway: Gateway) -> None:
    if event.kind != "ship_arrival":
        raise ValueError(f"Unrecognized event kind: {event.kind}")
    gateway.geography.arrive_in_transaction(
        db, event.payload["ship_id"],
        now=int(event.due_at.timestamp()), action_id=event.event_id,
    )


def _play(port: int, tokens: dict[str, str], gateway: Gateway) -> None:
    print("Foreground only. Select '1' or '2'; commands: look, say ..., north, feed, ship, quit")
    actors = ("Captain", "Navigator")
    actor = actors[0]
    serial = 0
    while True:
        try:
            raw = input(f"{actor}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if raw in {"quit", "exit"}:
            return
        if raw in {"1", "2"}:
            actor = actors[int(raw) - 1]
            continue
        if raw == "ship":
            print(json.dumps(gateway.geography.ship_status("demo_ship"), sort_keys=True))
            continue
        if raw == "feed":
            result = _http(port, tokens[actor], "/api/v1/feed?after=0")
            print(json.dumps(result["events"], indent=2))
            continue
        if not raw:
            continue
        serial += 1
        result = _http(port, tokens[actor], "/api/v1/command", {
            "command": raw, "action_id": f"synthetic_manual_{serial}",
        })
        print(result["text"])


def run_demo(*, interactive: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="realm_synthetic_play_") as tmp:
        db_path = Path(tmp) / "demo.sqlite3"
        worldpack_path = Path(tmp) / "generated_worldpack.json"
        worldpack_path.write_text(json.dumps({
            "schema": "realm.worldpack.v1", "world_id": "synthetic_play",
            "name": "Disposable Synthetic Playground", "start_room": "atrium",
            "rooms": [
                {"id": "atrium", "name": "Synthetic Atrium",
                 "description": "A harmless fixture for two local clients.",
                 "exits": {"north": "lookout"}},
                {"id": "lookout", "name": "Synthetic Lookout",
                 "description": "A second room; no canon attached.",
                 "exits": {"south": "atrium"}},
            ],
            "items": [],
        }), encoding="utf-8")
        imported = import_worldpack(db_path, worldpack_path)
        gateway = Gateway(db_path, world_id="synthetic_play", scoped_credentials=True)
        for actor in ("Captain", "Navigator", "Gatherer", "Scout"):
            gateway.join(actor)
        geo = gateway.geography
        geo.add_place("demo_universe", "Synthetic Universe", "universe")
        geo.add_place("demo_system", "Synthetic System", "system", "demo_universe")
        geo.add_place("demo_earth", "Synthetic Origin", "planet", "demo_system")
        geo.add_place("demo_moon", "Synthetic Destination", "planet", "demo_system")
        geo.link("demo_earth", "demo_moon", "ship", seconds=30)
        geo.register_ship("demo_ship", "Synthetic Ship", "Captain", "demo_earth")
        Crafting(db_path).place_node("atrium", "iron", stock=6, batch_yield=3)
        Crafting(db_path).place_node("atrium", "copper", stock=6, batch_yield=3)
        clock = WorldClock(db_path)
        clock.enable()

        tokens = {
            actor: gateway.scoped_credentials.provision(
                actor, capabilities=("feed:read", "command:write", "ship:depart")
                if actor == "Captain" else ("feed:read", "command:write"),
            ) for actor in ("Captain", "Navigator")
        }
        server = FeedWaitServer(gateway, (), port=0)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        thread.start()
        try:
            port = server.server_port
            departure = _http(port, tokens["Captain"], "/api/v1/ship/depart", {
                "ship_id": "demo_ship", "destination": "demo_moon",
                "action_id": "synthetic_depart_1",
            })
            if gateway.geography.ship_status("demo_ship")["state"] != "in_transit":
                raise AssertionError("Scoped ship departure did not begin transit")

            npc_at = datetime.now(UTC) + timedelta(seconds=1)
            policy = HostNpcGatherPolicy(
                policy_id="synthetic_gather_policy", world_id="synthetic_play",
                actor="Gatherer", opponent="Scout",
                sites=(ResourceSite("atrium", "iron"),
                       ResourceSite("atrium", "copper")),
                seed=7, event_id="synthetic_npc_1",
                not_before_utc=npc_at - timedelta(seconds=1),
                expires_at_utc=npc_at + timedelta(minutes=10),
            )
            mac = hmac.new(
                HOST_KEY, b"REALM/NPC-HOST-PERMIT/v1\x00" + policy.signing_bytes(),
                hashlib.sha256,
            ).hexdigest()
            clock.schedule(
                "synthetic_npc_1", due_at=npc_at, kind="npc_economic_gather",
                payload={"policy_id": policy.policy_id, "world_id": "synthetic_play"},
            )
            cursor = gateway.cursor()
            handler = make_host_npc_gather_handler(
                engine=gateway.engine, crafting=Crafting(db_path),
                host_key=HOST_KEY, grants_by_event_id={"synthetic_npc_1": (policy, mac)},
                now=lambda: npc_at,
            )
            report = run_foreground(
                clock, {"npc_economic_gather": handler}, pulses=1,
                now=lambda: npc_at, monotonic=lambda: 0.0,
                sleep=lambda _: None,
            )
            if report.event_ids != ("synthetic_npc_1",):
                raise AssertionError(f"NPC gather receipt missing: {report}")
            audiences = {}
            for actor, token in tokens.items():
                feed = _http(port, token, f"/api/v1/feed/wait?after={cursor}&wait_ms=0")
                audiences[actor] = sum(
                    event["kind"] == "gather" and event["actor"] == "Gatherer"
                    for event in feed["events"]
                )
            if audiences != {"Captain": 1, "Navigator": 1}:
                raise AssertionError(f"Two-client NPC feed delivery failed: {audiences}")

            with closing(sqlite3.connect(db_path)) as db:
                row = db.execute(
                    "SELECT event_id, due_utc FROM clock_events "
                    "WHERE kind='ship_arrival' AND processed_utc IS NULL"
                ).fetchone()
                if row is None:
                    raise AssertionError("Ship arrival was not scheduled atomically")
            arrival = datetime.fromisoformat(row[1])
            processed = clock.tick(arrival, lambda db, event: _arrival(db, event, gateway))
            if [event.event_id for event in processed] != [row[0]]:
                raise AssertionError("Host UTC arrival was not processed")
            ship = gateway.geography.ship_status("demo_ship")
            if ship["state"] != "docked" or ship["planet"] != "demo_moon":
                raise AssertionError(f"Ship arrival did not dock at destination: {ship}")
            if clock.tick(arrival, lambda db, event: _arrival(db, event, gateway)):
                raise AssertionError("Arrival replay applied twice")
            with closing(sqlite3.connect(db_path)) as db:
                receipts = {
                    "npc": db.execute(
                        "SELECT COUNT(*) FROM action_receipts WHERE actor='Gatherer'"
                    ).fetchone()[0],
                    "arrival": db.execute(
                        "SELECT COUNT(*) FROM geo_ship_events WHERE kind='arrive'"
                    ).fetchone()[0],
                }
            summary = {
                "world": "synthetic_play", "clients": list(tokens),
                "validated_worldpack_sha256": imported["sha256"],
                "npc_feed_events_each": audiences, "ship_departed": departure["ok"],
                "ship_arrived_at": ship["planet"], "receipts": receipts,
                "temporary_world_cleanup": "on_exit",
            }
            if interactive:
                print(json.dumps(summary, indent=2, sort_keys=True))
                _play(port, tokens, gateway)
            return summary
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--interactive", action="store_true")
    args = cli.parse_args()
    summary = run_demo(interactive=args.interactive)
    if not args.interactive:
        print(json.dumps(summary, indent=2, sort_keys=True))
