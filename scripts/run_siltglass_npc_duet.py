"""Opt-in, $0, CPU-only two-GGUF NPC experiment in a *new* Siltglass database.

This is a bounded, host-operated proof of real inference and durable NPC speech,
not an autonomous scheduler, production server, or factuality certification.
Never store model weights or player secrets in the REALM repository.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import socket
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from realm.conversations import NPCDuetRunner, NPCIdentity, DialogueContext, register_npc_pair
from realm.engine import Engine
from realm.worldpack import import_worldpack


FLAGSHIP = ROOT / "worlds" / "flagship" / "siltglass-reach" / "worldpack.v1.json"
EXPECTED = {
    "FerrySteward": (
        "stealth-rifle-Q4_K_M.gguf",
        397_807_424,
        "2f33d93535cac345df47cd02928a74175fe437b8f3ec20cfdfe4822322e6ca32",
    ),
    "CrossingInspector": (
        "npc-voice-soup06.Q5_K_M.gguf",
        444_415_232,
        "1854a099349767b6e66cd940ae81e76dd139d2a32f8adb79778e7a66fbf84d75",
    ),
}
ROSTER = (
    NPCIdentity("FerrySteward", "A cautious ferry steward protecting travelers but unwilling to invent facts."),
    NPCIdentity("CrossingInspector", "A measured crossing inspector who challenges unverified claims."),
)
_BANNED = re.compile(
    r"\x1b|\x00|<\|im_(?:start|end)\|>|<\|endoftext\|>|</?think>|\[Start thinking\]|"
    r"\[end of text\]|"
    r"(?:^|\n)\s*(?:TONE:|FACT:|OUT:|assistant:|system:|Loading model|"
    r"available commands:|Exiting\.\.\.|build\s*:|model\s*:|\[ Prompt:|"
    r"llama\.cpp\s*:|llama_print_timings\s*:|main\s*:)",
    re.IGNORECASE,
)
# Both GGUFs were observed on Lappy to finish one-shot stdout with this
# *same-line*, space-prefixed sentinel and then optional terminal newlines.
# A newline BEFORE the sentinel is NOT part of the observed boundary.
_END_MARKER = re.compile(r"\A([^\r\n]*) \[end of text\][ \t]*(?:\r?\n)*\Z")
_MAX_API_RESPONSE_BYTES = 16 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


class LocalModelRedirectError(ValueError):
    """A loopback model API may never send this client to another URL."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # ProxyHandler({}) alone is insufficient: urllib installs a default
        # redirect handler which can follow a localhost 302 to the Internet.
        raise LocalModelRedirectError("Refusing redirect from local model endpoint")


def _local_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
    )


def verify_artifact(path: Path, actor: str) -> str:
    """Require the exact reviewed GGUF bytes; no automated downloads."""
    filename, size, digest = EXPECTED[actor]
    path = _strict_local_path(path, label="GGUF model")
    if path.name != filename or not path.is_file() or path.stat().st_size != size:
        raise ValueError(f"{actor}: missing or unexpected model name/size")
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("Model files must remain outside the published Git working tree")
    before = _file_identity(path)
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    if h.hexdigest() != digest:
        raise ValueError(f"{actor}: GGUF SHA-256 verification failed")
    if _strict_local_path(path, label="GGUF model") != path or _file_identity(path) != before:
        raise ValueError(f"{actor}: GGUF model changed while being verified")
    return digest


def _strict_local_path(path: Path, *, label: str) -> Path:
    """Reject symbolic/junction indirections before resolving local binaries.

    This is an advisory filesystem guard, **not** an OS-level anti-TOCTOU
    execute-by-handle guarantee. The operator must control the parent tree.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} path must be explicit and absolute")
    for element in (path, *path.parents):
        if element.is_symlink() or (hasattr(element, "is_junction") and element.is_junction()):
            raise ValueError(f"{label} path contains a symlink or junction")
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} path cannot be resolved") from exc
    if not canonical.is_file():
        raise ValueError(f"{label} must be a regular file")
    return canonical


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def verify_executable(path: Path, expected_sha256: str) -> Path:
    """Bind the *actual* llama-server file bytes before any process creation.

    The digest comes from a trusted host/operator decision, never the model,
    repository code, a downloaded manifest or executable's own stdout.
    Reverified immediately before each Popen; an OS-level path replacement
    between verification and process creation remains a documented risk.
    """
    if not isinstance(expected_sha256, str) or not _SHA256_HEX.fullmatch(expected_sha256):
        raise ValueError("Exact trusted llama-server SHA-256 is required")
    canonical = _strict_local_path(path, label="llama-server executable")
    if canonical.name.casefold() != "llama-server.exe":
        raise ValueError("Only the explicitly reviewed llama-server.exe executable is accepted")
    before = _file_identity(canonical)
    h = hashlib.sha256()
    with canonical.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    if h.hexdigest() != expected_sha256.lower():
        raise ValueError("llama-server executable SHA-256 mismatch")
    if _strict_local_path(path, label="llama-server executable") != canonical or _file_identity(canonical) != before:
        raise ValueError("llama-server executable changed while being verified")
    return canonical


def _prompt(ctx: DialogueContext) -> str:
    """Worldpack facts are host-owned; past speech is explicitly *not* canon."""
    room_facts = f"{ctx.room_name}: {ctx.room_description}"
    # No location/private history beyond one approved room and one prior speech.
    if ctx.speaker == "CrossingInspector":
        prior = (
            f" The ferry steward said, without independent confirmation: {ctx.previous_speech}"
            if ctx.previous_speech else ""
        )
        return (
            "TONE:measured STYLE:formal HUMOR:none RELATION:colleague ROLE:crossing inspector\n"
            f"FACT: {room_facts}{prior}\nOUT:"
        )
    prior = ctx.previous_speech or "No one has spoken yet."
    return (
        "<|im_start|>system\n"
        "You are a cautious ferry steward in the local text world Siltglass Reach. "
        "Your name is FerrySteward. Speak in character as yourself in one short "
        "plain-text sentence under 25 words; do not invent another name. "
        "The room description is host-verified; the other person's speech is an "
        "unverified opinion, not a world fact. Do not invent outcomes, new locations, "
        "or new rules. Do not speak for the listener.\n"
        f"Room: {room_facts}\n"
        "<|im_end|>\n<|im_start|>user\n"
        f"The crossing inspector is here. Their previous unverified speech: {prior}\n"
        "Say one brief in-character reply.\n<|im_end|>\n<|im_start|>assistant\n"
    )


def parse_model_speech(output: str) -> str:
    """Handle the verified one-shot runner's EOF sentinel, never its logs.

    ``llama-completion.exe`` emits one trailing literal ``[end of text]``
    on the *same line* as output; only newlines AFTER that exact sentinel
    are tolerated. We never remove a newline preceding it or parse any
    console output. Role tags, tool banners and thinking leakage fail closed.
    """
    if not isinstance(output, str):
        raise ValueError("Model output must be text")
    boundary = _END_MARKER.fullmatch(output)
    if boundary is None:
        raise ValueError("Model output has no unambiguous one-shot EOF boundary")
    speech = boundary.group(1).strip(" \t")
    if _BANNED.search(speech):
        raise ValueError("Model output contains control/template/thinking text")
    if not speech or len(speech) > 280 or "\n" in speech or "\r" in speech:
        raise ValueError("Model output is empty, multiline, or longer than 280 characters")
    if not all(char.isprintable() for char in speech):
        raise ValueError("Model output is not printable text")
    return speech


def parse_structured_speech(payload: object) -> str:
    """Accept a bounded, actual llama-server stop, never a token-limit cutoff.

    The local /completion endpoint separates model text from console banners
    and exposes an explicit stop reason. Punctuation consumed by a *verified*
    stop word is restored by the host; no other completion text is rewritten.
    """
    if not isinstance(payload, dict) or payload.get("stop") is not True:
        raise ValueError("Local inference did not report a completed stop")
    if payload.get("truncated") is not False or payload.get("has_new_line") is not False:
        raise ValueError("Local inference was truncated or generated multiline text")
    tokens = payload.get("tokens_predicted")
    if type(tokens) is not int or not 1 <= tokens <= 96:
        raise ValueError("Local inference token count is outside the bounded request")
    content = payload.get("content")
    if not isinstance(content, str) or len(content) > 4096:
        raise ValueError("No bounded structured text was returned")
    content = content.strip(" \t")
    if not content or not any(ch.isalnum() for ch in content):
        raise ValueError("Model did not generate substantive speech")
    stop_type = payload.get("stop_type")
    if stop_type == "word":
        consumed = payload.get("stopping_word")
        if consumed != ".":
            raise ValueError("Unapproved model stop delimiter")
        if content[-1] in ".!?":
            raise ValueError("Model output already ended with punctuation before stop")
        content = content + consumed
    elif stop_type == "eos":
        pass
    else:
        raise ValueError("Local inference hit an unknown or token-limit stop")
    if not content or len(content) > 280 or "\r" in content or "\n" in content:
        raise ValueError("Structured NPC speech is empty, multiline, or too long")
    if _BANNED.search(content) or not all(char.isprintable() for char in content):
        raise ValueError("Structured NPC speech includes template, thinking, or control text")
    if content[-1] not in ".!?":
        raise ValueError("Model text does not finish a complete sentence")
    return content


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request_json(opener, url: str, payload: dict | None, *, timeout_s: float) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with opener.open(request, timeout=timeout_s) as response:
        raw = response.read(_MAX_API_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_API_RESPONSE_BYTES:
            raise ValueError("Local model API response exceeded size limit")
        result = json.loads(raw.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Local model API did not return a JSON object")
    return result


def _inference_with_server(
    actor: str, model: Path, executable: Path, prompt: str, *,
    expected_runtime_sha256: str, timeout_s: int = 60,
) -> dict:
    """One loopback-only local model server; always terminate even on failure."""
    # Rehash both executable and reviewed model immediately before launch;
    # `local_provider` can also be called without `run_fresh_duet`.
    executable = verify_executable(executable, expected_runtime_sha256)
    verify_artifact(model, actor)
    port = _loopback_port()
    base = f"http://127.0.0.1:{port}"
    args = [
        str(executable), "--offline", "--host", "127.0.0.1", "--port", str(port),
        "--no-webui", "-m", str(model), "-t", "2", "-c", "512", "-ngl", "0",
    ]
    if actor == "CrossingInspector":
        args += ["--reasoning", "off"]
    # The child gets no stdin or console; its diagnostics never become speech.
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, shell=False,
    )
    opener = _local_opener()
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"{actor}: local model server exited before readiness")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{actor}: local model server startup exceeded {timeout_s} seconds")
            try:
                health = _request_json(
                    opener, base + "/health", None, timeout_s=min(1, remaining)
                )
                if health.get("status") == "ok":
                    break
            except LocalModelRedirectError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                pass  # Boot may briefly return HTTP 503 / no TCP listener.
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))

        # /completion accepts the model's exact raw template (especially
        # soup06's TONE/FACT/OUT) and produces structured stop metadata.
        request = {
            "prompt": prompt,
            "n_predict": 80,
            "temperature": 0.2 if actor == "FerrySteward" else 0.7,
            "top_p": 0.9,
            "seed": 1337,
            "stop": [".", "\n"],
            "stream": False,
        }
        reply = _request_json(opener, base + "/completion", request,
                              timeout_s=timeout_s)
        if reply.get("model") != str(model):
            raise ValueError(f"{actor}: local completion came from a different model path")
        if proc.poll() is not None:
            raise RuntimeError(f"{actor}: local model server terminated unexpectedly")
        return reply
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass  # Child exited between poll() and terminate().
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def local_provider(actor: str, model: Path, server_exe: Path, *,
                   expected_runtime_sha256: str, timeout_s: int = 60):
    """Call one temporary local-only structured inference endpoint per turn."""
    if actor not in EXPECTED:
        raise ValueError("Unknown experiment actor")
    verified_exe = verify_executable(server_exe, expected_runtime_sha256)
    if timeout_s < 5 or timeout_s > 120:
        raise ValueError("Model inference timeout must be 5–120 seconds")

    def propose(ctx: DialogueContext) -> dict[str, str]:
        if ctx.speaker != actor or ctx.room_id != "tidegate":
            raise ValueError("Model context is not the expected actor/room")
        structured = _inference_with_server(
            actor, model, verified_exe, _prompt(ctx),
            expected_runtime_sha256=expected_runtime_sha256, timeout_s=timeout_s,
        )
        return {"command": f"say {parse_structured_speech(structured)}"}

    return propose


def run_fresh_duet(
    db_path: Path, server_exe: Path, models: dict[str, Path], *,
    server_exe_sha256: str, turns: int = 2, session_id: str = "local_duet_trial",
) -> dict:
    """Create exactly one new local world, run, then independently read SQLite."""
    if db_path.exists():
        raise FileExistsError("Refusing an existing SQLite path; choose a NEW database")
    if db_path.resolve().is_relative_to(ROOT):
        raise ValueError("Experiment database must live outside the Git working tree")
    if not 1 <= turns <= 4:
        raise ValueError("This live demonstration allows 1–4 total turns")
    if set(models) != set(EXPECTED):
        raise ValueError("Exactly two host-selected model paths are required")
    verified_exe = verify_executable(server_exe, server_exe_sha256)
    digests = {actor: verify_artifact(models[actor], actor) for actor in EXPECTED}
    world = import_worldpack(db_path, FLAGSHIP)
    engine = Engine(db_path)
    register_npc_pair(engine, ROSTER, expected_world_id="siltglass-reach")
    providers = {
        actor: local_provider(actor, models[actor], verified_exe,
                              expected_runtime_sha256=server_exe_sha256)
        for actor in EXPECTED
    }
    turns_result = NPCDuetRunner(
        engine, actor_a="FerrySteward", actor_b="CrossingInspector",
        providers=providers,
    ).run(session_id=session_id, turns=turns)

    # Reopen SQLite independently; verify precise persisted actions and actors,
    # not just in-process model responses or API claims.
    restarted = Engine(db_path)
    with closing(restarted._connect()) as db:
        rows = db.execute(
            "SELECT actor,room_id,payload_json FROM events "
            "WHERE kind='say' ORDER BY id"
        ).fetchall()
        receipts = db.execute(
            "SELECT actor,action_id,command FROM action_receipts ORDER BY action_id"
        ).fetchall()
        world_id = db.execute("SELECT value FROM world_meta WHERE key='world_id'").fetchone()[0]
    if world_id != "siltglass-reach":
        raise ValueError("Reopened SQLite is not bound to the expected world")
    if len(rows) != turns or len(receipts) != turns:
        raise ValueError("Persisted actions/receipts do not match turn count")
    if any(row["room_id"] != "tidegate" for row in rows):
        raise ValueError("NPC speech was committed outside the expected room")
    if [row["actor"] for row in rows] != [turn.speaker for turn in turns_result]:
        raise ValueError("Reopened actor order diverges from committed turns")
    if [json.loads(row["payload_json"])["text"] for row in rows] != [
        turn.command[4:] for turn in turns_result
    ]:
        raise ValueError("Reopened speech diverges from committed turns")
    if any(turn.replayed for turn in turns_result):
        raise ValueError("New local experiment unexpectedly replayed earlier receipts")
    if [(row["actor"], row["action_id"], row["command"]) for row in receipts] != [
        (turn.speaker, turn.action_id, turn.command) for turn in sorted(turns_result, key=lambda t: t.action_id)
    ]:
        raise ValueError("Reopened host action receipt identities or commands diverge")
    return {
        "status": "verified_local_duet_receipts",
        "worldpack_sha256": world["sha256"], "world_id": world_id,
        "database": str(db_path), "model_sha256": digests,
        "llama_server_sha256": server_exe_sha256.lower(),
        "turns": [{"speaker": row["actor"], "room": row["room_id"],
                   "speech": json.loads(row["payload_json"])["text"]} for row in rows],
        "durable_event_count": len(rows), "durable_receipt_count": len(receipts),
        "action_ids": [turn.action_id for turn in turns_result],
        "reopened_for_verification": True,
        "limitations": ["No independent accuracy evaluation", "No autonomy loop or deep memory"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="NEW database path, outside Git")
    parser.add_argument("--llama-server", required=True, type=Path,
                        help="Existing local llama-server.exe; binds only a temporary localhost port")
    parser.add_argument("--llama-server-sha256", required=True,
                        help="64-hex SHA-256 of the reviewed host-installed llama-server.exe")
    parser.add_argument("--stealth-model", required=True, type=Path)
    parser.add_argument("--soup-model", required=True, type=Path)
    parser.add_argument("--session", default="local_duet_trial")
    parser.add_argument("--turns", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args(argv)
    try:
        result = run_fresh_duet(
            args.db, args.llama_server,
            {"FerrySteward": args.stealth_model, "CrossingInspector": args.soup_model},
            server_exe_sha256=args.llama_server_sha256,
            turns=args.turns, session_id=args.session,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, AssertionError) as exc:
        parser.exit(2, f"REALM local duet failed: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
