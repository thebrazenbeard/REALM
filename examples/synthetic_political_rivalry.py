"""Two real OS-process REALM players contest resources, betray, and reconnect.

Run from repo root: py -3 examples/synthetic_political_rivalry.py
Only disposable worlds and loopback sockets. No public service or user secrets.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from synthetic_multiprocess_play import _Client, _expect
from realm.gateway import Gateway, GatewayServer
from realm.political_oaths import WorldPolitics
from realm.political_leverage import WorldLeverage


def run_demo() -> dict:
    """No command is sent to the world database by either client directly."""
    clients: list[_Client] = []
    exit_codes: list[int] = []
    server = None
    thread = None
    root_path = None
    with tempfile.TemporaryDirectory(prefix="realm_political_rivalry_") as tmp:
        root_path = Path(tmp)
        db_path = root_path / "world.sqlite3"
        gateway = Gateway(db_path, world_id="rivalry_demo", scoped_credentials=True)
        for actor in ("Alice", "Bob"):
            gateway.join(actor)
        politics = WorldPolitics(db_path, world_id="rivalry_demo")
        politics.install()
        politics.create_faction("north", "Northern League")
        politics.create_faction("south", "Southern Compact")
        leverage = WorldLeverage(db_path, world_id="rivalry_demo")
        leverage.install()
        leverage.define_site("council", "Council Chamber", room_id="atrium")
        leverage.configure_batch(
            sites=(),
            resources=(("token", 1), ("wrench", 2)),
        )
        permissions = ("room:read", "feed:read", "command:write")
        alpha_token = gateway.scoped_credentials.provision("Alice", capabilities=permissions)
        beta_token = gateway.scoped_credentials.provision("Bob", capabilities=permissions)
        server = GatewayServer(gateway, (), port=0)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        thread.start()
        try:
            alpha = _Client(server.server_port, alpha_token)
            clients.append(alpha)
            beta = _Client(server.server_port, beta_token)
            clients.append(beta)
            os_pids = [alpha.process.pid, beta.process.pid]
            if len({os.getpid(), *os_pids}) != 3:
                raise AssertionError("Two genuine independent player OS processes required")

            def command(client: _Client, value: str, action_id: str | None = None) -> str:
                payload = {"command": value}
                if action_id is not None:
                    payload["action_id"] = action_id
                return _expect(client, "/api/v1/command", payload=payload)["text"]

            if "pledged allegiance" not in command(alpha, "pledge north", "join-north"):
                raise AssertionError("Alpha could not take the northern oath")
            if "pledged allegiance" not in command(beta, "pledge south", "join-south"):
                raise AssertionError("Beta could not take the southern oath")
            if "You take Brass token." not in command(alpha, "take token"):
                raise AssertionError("Alpha did not acquire a real resource")
            first = command(alpha, "offer token to council", "alpha-offer")
            if "offered Brass token" not in first:
                raise AssertionError("Alpha resource offering was not acknowledged")
            north = command(beta, "control council")
            if "Controlled by Northern League" not in north:
                raise AssertionError("North failed to control a real scored site")

            for movement in ("north", "east", "take wrench", "west", "south"):
                message = command(beta, movement)
                if message.startswith("Unknown") or message.startswith("Cannot"):
                    raise AssertionError(f"Beta movement/custody denied: {movement}: {message}")
            offered = command(beta, "offer wrench to council", "beta-offer")
            if "offered Worn wrench" not in offered:
                raise AssertionError("Beta physical resource did not transfer")
            south = command(alpha, "control council")
            if "Controlled by Southern Compact" not in south:
                raise AssertionError("Second player did not overturn political control")

            denial = _expect(beta, "/api/v1/command", status=403, payload={
                "player": "Alice", "command": "renounce",
            })
            if denial.get("ok", True):
                raise AssertionError("Cross-actor political command forged")
            if command(alpha, "offer token to council", "alpha-offer") != first:
                raise AssertionError("Action replay changed prior economic receipt")

            if "broke allegiance" not in command(beta, "renounce", "betray-south"):
                raise AssertionError("Beta could not betray original faction")
            if "pledged allegiance" not in command(beta, "pledge north", "new-oath"):
                raise AssertionError("Beta could not join rival faction after betrayal")
            if "Controlled by Southern Compact" not in command(
                beta, "control council"
            ):
                raise AssertionError("Betrayal improperly transferred sunk faction resources")
            with closing(sqlite3.connect(db_path)) as db:
                offerings = db.execute("SELECT COUNT(*) FROM political_offerings").fetchone()[0]
                events = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='political_offer'"
                ).fetchone()[0]
                spent = db.execute(
                    "SELECT COUNT(*) FROM items WHERE id IN ('token','wrench')"
                ).fetchone()[0]
            if (offerings, events, spent) != (2, 2, 0):
                raise AssertionError("World ledger, emitted events or spent inventory disagree")
            for child in reversed(clients):
                exit_codes.append(child.close())
            clients.clear()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            if thread.is_alive():
                raise RuntimeError("First server did not shut down")
            server = None
            thread = None

            # A new Gateway/server instance must recover the *same* world:
            # tokens remain attached to their actual principals; no new join.
            recovered = Gateway(db_path, world_id="rivalry_demo", scoped_credentials=True)
            server = GatewayServer(recovered, (), port=0)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.02}, daemon=True,
            )
            thread.start()
            reconnected = _Client(server.server_port, alpha_token)
            clients.append(reconnected)
            status = _expect(reconnected, "/api/v1/command", payload={
                "command": "control council",
            })["text"]
            if "Controlled by Southern Compact" not in status:
                raise AssertionError("Site owner vanished after server restart")
            betrayed = _expect(reconnected, "/api/v1/command", payload={
                "command": "allegiance Bob",
            })["text"]
            if "Northern League" not in betrayed or "broken oaths: 1" not in betrayed:
                raise AssertionError("Public betrayal lost after restart")
            result = {
                "clients": ["Alice", "Bob"],
                "host_process_id": os.getpid(),
                "client_process_ids": os_pids,
                "control_sequence": ["Northern League", "Southern Compact"],
                "control_after_restart": "Southern Compact",
                "distinct_item_offerings": offerings,
                "faction_revisions": events,
                "cross_actor_denial": 403,
                "betrayal_persists_after_restart": True,
                "item_remaining_after_use": spent,
                "host_public_listener": False,
            }
        finally:
            for child in reversed(clients):
                exit_codes.append(child.close())
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=3)
                if thread.is_alive():
                    raise AssertionError("Second server did not shut down")
    if root_path is None or root_path.exists():
        raise AssertionError("Disposable world was not cleaned up")
    if sorted(exit_codes) != [0, 0, 0]:
        raise AssertionError(f"Independent clients failed to close: {exit_codes}")
    result["client_exit_codes"] = sorted(exit_codes)
    result["temporary_world_cleanup"] = "verified"
    return result


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2, sort_keys=True))
