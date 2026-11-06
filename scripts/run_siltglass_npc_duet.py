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


def verify_artifact(path: Path, actor: str) -> str:
    """Require the exact reviewed GGUF bytes; no automated downloads."""
    filename, size, digest = EXPECTED[actor]
    if path.name != filename or not path.is_file() or path.stat().st_size != size:
        raise ValueError(f"{actor}: missing or unexpected model name/size")
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("Model files must remain outside the published Git working tree")
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    if h.hexdigest() != digest:
        raise ValueError(f"{actor}: GGUF SHA-256 verification failed")
    return digest


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


def local_provider(actor: str, model: Path, completion_exe: Path, *, timeout_s: int = 60):
    """Call only an explicit host-owned executable with strict runtime bounds."""
    if actor not in EXPECTED:
        raise ValueError("Unknown experiment actor")
    if completion_exe.name.casefold() != "llama-completion.exe" or not completion_exe.is_file():
        raise FileNotFoundError(f"Expected an installed llama-completion.exe: {completion_exe}")
    if timeout_s < 5 or timeout_s > 120:
        raise ValueError("Model inference timeout must be 5–120 seconds")

    def propose(ctx: DialogueContext) -> dict[str, str]:
        if ctx.speaker != actor or ctx.room_id != "tidegate":
            raise ValueError("Model context is not the expected actor/room")
        args = [
            str(completion_exe), "--offline", "-m", str(model), "-t", "2",
            "-c", "512", "-n", "40", "-ngl", "0", "--no-display-prompt",
            "--no-conversation",
        ]
        if actor == "CrossingInspector":
            args += [
                "--reasoning", "off", "--temp", "0.7", "--top-p", "0.9",
                "--repeat-penalty", "1.05",
            ]
        args.extend(["-p", _prompt(ctx)])
        result = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{actor}: local llama-completion exited with code {result.returncode}")
        if len(result.stdout) > 4096:
            raise ValueError(f"{actor}: model output exceeds bounded stdout window")
        return {"command": f"say {parse_model_speech(result.stdout)}"}

    return propose


def run_fresh_duet(
    db_path: Path, completion_exe: Path, models: dict[str, Path], *,
    turns: int = 2, session_id: str = "local_duet_trial",
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
    digests = {actor: verify_artifact(models[actor], actor) for actor in EXPECTED}
    if completion_exe.name.casefold() != "llama-completion.exe" or not completion_exe.is_file():
        raise FileNotFoundError("Expected llama-completion.exe executable not present")
    world = import_worldpack(db_path, FLAGSHIP)
    engine = Engine(db_path)
    register_npc_pair(engine, ROSTER, expected_world_id="siltglass-reach")
    providers = {
        actor: local_provider(actor, models[actor], completion_exe)
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
    parser.add_argument("--llama-completion", required=True, type=Path,
                        help="Existing local llama-completion.exe (not interactive llama-cli.exe)")
    parser.add_argument("--stealth-model", required=True, type=Path)
    parser.add_argument("--soup-model", required=True, type=Path)
    parser.add_argument("--session", default="local_duet_trial")
    parser.add_argument("--turns", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args(argv)
    try:
        result = run_fresh_duet(
            args.db, args.llama_completion,
            {"FerrySteward": args.stealth_model, "CrossingInspector": args.soup_model},
            turns=args.turns, session_id=args.session,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, AssertionError) as exc:
        parser.exit(2, f"REALM local duet failed: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
