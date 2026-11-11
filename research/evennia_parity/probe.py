"""Run a strictly local, disposable, two-install Evennia 6.1 parity smoke test.

This is *not* a REALM feature-parity certification. It measures only basic
installation isolation, port separation, boot cost, and accessible listeners.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
VENV_BIN = ROOT / ".venv" / "bin"
EVENNIA = str(VENV_BIN / "evennia")
PYTHON = str(VENV_BIN / "python")
ENV = dict(os.environ, PATH=f"{VENV_BIN}:{os.environ.get('PATH', '')}")


def run(world: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [EVENNIA, *args],
        cwd=ROOT / world,
        env=ENV,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def rss_kib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, PermissionError):
        return None
    return None


def live_processes(world: str) -> dict[str, dict[str, int | None]]:
    result = {}
    for name in ("portal", "server"):
        pidfile = ROOT / world / "server" / f"{name}.pid"
        if not pidfile.exists():
            result[name] = {"pid": None, "rss_kib": None}
            continue
        pid = int(pidfile.read_text().strip())
        result[name] = {"pid": pid, "rss_kib": rss_kib(pid)}
    return result


def account_count(world: str, username: str) -> dict[str, int | bool]:
    code = (
        "import os,json;os.environ.setdefault('DJANGO_SETTINGS_MODULE','server.conf.settings');"
        "import django;django.setup();from evennia.accounts.models import AccountDB;"
        "print(json.dumps({'count':AccountDB.objects.count(),"
        "'present':AccountDB.objects.filter(username=" + repr(username) + ").exists()}))"
    )
    outcome = subprocess.run(
        [PYTHON, "-c", code], cwd=ROOT / world, env=ENV, capture_output=True,
        text=True, timeout=10, check=True,
    )
    return json.loads(outcome.stdout)


def connect_loopback(port: int) -> dict[str, str | bool]:
    with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
        sock.settimeout(4)
        preview = sock.recv(2048)
    return {"connected": True, "received_bytes": str(len(preview))}


def main() -> None:
    sequential = "--sequential" in sys.argv
    result: dict[str, object] = {"version": "6.1.0", "sequential": sequential, "worlds": {}}
    started = []
    try:
        # Scratch commands run in separate PID namespaces: previous one-shot
        # launchers leave game-local pidfiles containing recycled PIDs (13/18).
        # Remove only these fixture-owned stale files before *either* world
        # starts. Otherwise world_b can mistake world_a's live PID for its own.
        for world in ("world_a", "world_b"):
            for name in ("portal", "server"):
                pidfile = ROOT / world / "server" / f"{name}.pid"
                if pidfile.exists():
                    pidfile.unlink()
        for world, port in (("world_a", 41400), ("world_b", 42400)):
            t0 = time.monotonic()
            started.append(world)  # also stop partial startup on failure
            started_result = run(world, "start")
            elapsed = round(time.monotonic() - t0, 3)
            assert started_result.returncode == 0, (world, started_result.stdout)
            assert "Evennia running." in started_result.stdout, (world, started_result.stdout)
            result["worlds"][world] = {
                "start_s": elapsed,
                "processes": live_processes(world),
                "port": port,
                "telnet": connect_loopback(port),
                "own_account": account_count(world, "realm_spike_a" if world == "world_a" else "realm_spike_b"),
                "other_account": account_count(world, "realm_spike_b" if world == "world_a" else "realm_spike_a"),
            }
            if sequential:
                stopped_result = run(world, "stop")
                result.setdefault("intermediate_shutdown", {})[world] = stopped_result.returncode
                started.remove(world)
        a = result["worlds"]["world_a"]
        b = result["worlds"]["world_b"]
        assert a["own_account"]["present"] and not a["other_account"]["present"]
        assert b["own_account"]["present"] and not b["other_account"]["present"]
        result["memory_sample_complete"] = all(
            p["rss_kib"] is not None for x in (a, b) for p in x["processes"].values()
        )
        result["smoke_result"] = (
            "PASS: separate installs/ports/accounts in sequence"
            if sequential else "PASS: two concurrent running installs/ports/account databases"
        )
    finally:
        result["shutdown"] = {}
        for world in reversed(started):
            stopped_result = run(world, "stop")
            result["shutdown"][world] = {"exit_code": stopped_result.returncode, "output": stopped_result.stdout[-500:]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
