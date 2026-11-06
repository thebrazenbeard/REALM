"""Offline lexical triage of externally captured tiny-NPC model responses.

Never loads a model, opens a game DB, contacts a provider, or creates receipts.
Model responses and timing claims are untrusted input, not REALM events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path


MAX_FILE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 280
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
CONTROL = re.compile(r"<\|(?:im_start|im_end|endoftext)\|>|</?think>|\[end of text\]|\[start thinking\]", re.I)


class EvalInputError(ValueError):
    """A malformed benchmark receipt is not a passing or failing NPC."""


def _read_small(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise EvalInputError("Input missing or exceeds the 1 MB offline limit")
    return path.read_text(encoding="utf-8")


def load_cases(path: Path) -> dict:
    try:
        data = json.loads(_read_small(path))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise EvalInputError("Invalid case JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != "realm.tiny-npc-eval.v1":
        raise EvalInputError("Unexpected case schema")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalInputError("No cases")
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", case["id"])):
            raise EvalInputError("Invalid case ID")
        if case["id"] in ids:
            raise EvalInputError("Duplicate case ID")
        ids.add(case["id"])
        for key in ("model_id", "prompt", "track"):
            if not isinstance(case.get(key), str) or not case[key]:
                raise EvalInputError("Malformed case field: " + key)
        checks = case.get("checks")
        if not isinstance(checks, dict):
            raise EvalInputError("Missing lexical checks")
        for key in ("required_all", "forbidden"):
            if not isinstance(checks.get(key, []), list) or not all(
                isinstance(term, str) and term for term in checks.get(key, [])
            ):
                raise EvalInputError("Invalid case lexical terms")
    return data


def load_responses(path: Path, *, known_ids: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in _read_small(path).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalInputError("Invalid receipt JSONL") from exc
        if (not isinstance(row, dict) or not isinstance(row.get("case_id"), str)
                or row["case_id"] not in known_ids):
            raise EvalInputError("Unknown response case ID")
        case_id = row["case_id"]
        if case_id in out:
            raise EvalInputError("Duplicate response case ID")
        out[case_id] = row
    return out


def _valid_metadata(case: dict, receipt: dict, expected_sha256: str | None) -> list[str]:
    failures = []
    if receipt.get("model_id") != case["model_id"]:
        failures.append("model_id_mismatch")
    if receipt.get("logical_prompt_sha256") != hashlib.sha256(case["prompt"].encode("utf-8")).hexdigest():
        failures.append("logical_prompt_sha256_mismatch")
    rendered = receipt.get("rendered_prompt_sha256")
    if not isinstance(rendered, str) or not SHA256.fullmatch(rendered):
        failures.append("rendered_prompt_sha256_missing_or_invalid")
    artifact_sha = receipt.get("artifact_sha256")
    if not isinstance(artifact_sha, str) or not SHA256.fullmatch(artifact_sha):
        failures.append("artifact_sha256_missing_or_invalid")
    elif expected_sha256 and artifact_sha != expected_sha256:
        failures.append("artifact_sha256_mismatch")
    if not isinstance(receipt.get("source_head"), str) or not COMMIT.fullmatch(receipt["source_head"]):
        failures.append("source_head_missing_or_invalid")
    if not isinstance(receipt.get("backend"), str) or not receipt["backend"].strip():
        failures.append("backend_missing")
    if not isinstance(receipt.get("backend_version"), str) or not receipt["backend_version"].strip():
        failures.append("backend_version_missing")
    millis = receipt.get("latency_ms")
    if isinstance(millis, bool) or not isinstance(millis, (int, float)) or not 0 <= millis <= 3_600_000:
        failures.append("latency_ms_missing_or_invalid")
    if receipt.get("stop_type") not in ("eos", "word"):
        failures.append("generation_did_not_complete")
    elif receipt.get("stop_type") == "word" and receipt.get("stopping_word") != ".":
        failures.append("unapproved_stop_delimiter")
    return failures


def assess(case: dict, receipt: dict, *, expected_sha256: str | None = None) -> dict:
    """Lexical checks only. PASS is *never* a semantic model qualification."""
    violations = _valid_metadata(case, receipt, expected_sha256)
    text = receipt.get("text")
    checks = case["checks"]
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_OUTPUT_CHARS:
        violations.append("speech_missing_or_overlong")
    else:
        if not all(ch.isprintable() for ch in text):
            violations.append("nonprintable_or_terminal_control")
        if "\r" in text or "\n" in text:
            violations.append("multiline_speech")
        if CONTROL.search(text):
            violations.append("model_control_tokens")
        max_words = checks.get("max_words", 25)
        if not isinstance(max_words, int) or isinstance(max_words, bool) or not 1 <= max_words <= 100:
            raise EvalInputError("Invalid max_words")
        if len(text.split()) > max_words:
            violations.append("word_limit")
        if checks.get("one_sentence", True):
            # Narrow orthographic check: punctuation != semantic sentence count.
            if not re.search(r"[.!?][\"']?\Z", text.strip()) or len(re.findall(r"[.!?](?=\s|$)", text)) != 1:
                violations.append("sentence_boundary")
        folded = text.casefold()
        for term in checks.get("required_all", []):
            if term.casefold() not in folded:
                violations.append("missing_required_fact:" + term)
        for term in checks.get("forbidden", []):
            if term.casefold() in folded:
                violations.append("forbidden_text:" + term)
    return {
        "case_id": case["id"],
        "model_id": case["model_id"],
        "result": "FAIL_LEXICAL" if violations else "PASS_LEXICAL",
        "violations": violations,
        "manual_review": case.get("manual_review", []),
    }


def score(corpus: dict, responses: dict[str, dict], *, expected_sha256: str | None = None) -> dict:
    cases = corpus["cases"]
    rows = []
    for case in cases:
        if case["id"] not in responses:
            rows.append({"case_id": case["id"], "model_id": case["model_id"],
                         "result": "UNSCORED", "violations": ["missing_receipt"],
                         "manual_review": case.get("manual_review", [])})
        else:
            rows.append(assess(case, responses[case["id"]], expected_sha256=expected_sha256))
    latencies = [float(responses[r["case_id"]]["latency_ms"])
                 for r in rows if r["result"] == "PASS_LEXICAL"
                 and isinstance(responses[r["case_id"]].get("latency_ms"), (int, float))]
    latencies.sort()
    return {
        "schema": "realm.tiny-npc-eval-result.v1",
        "evidence": "UNTRUSTED_INPUT_LEXICAL_TRIAGE_ONLY",
        "source_revision": corpus.get("revision", "unknown"),
        "total_cases": len(cases),
        "received": len(responses),
        "lexical_pass": sum(r["result"] == "PASS_LEXICAL" for r in rows),
        "lexical_fail": sum(r["result"] == "FAIL_LEXICAL" for r in rows),
        "unscored": sum(r["result"] == "UNSCORED" for r in rows),
        "reported_latency_ms": {
            "p50": statistics.median(latencies) if latencies else None,
            "p95_nearest_rank": latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)] if latencies else None,
            "source": "unverified_external_receipt_not_host_measurement",
        },
        "rows": rows,
        "qualified_for_live_realm": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.v1.json"))
    parser.add_argument("--responses", type=Path, required=True, help="Externally captured JSONL, no inference here")
    parser.add_argument("--model-id", help="Restrict to one corpus model for single-artifact comparison")
    parser.add_argument("--expected-sha256", help="Optional exact GGUF artifact SHA-256 binding")
    args = parser.parse_args(argv)
    try:
        if args.expected_sha256 and not SHA256.fullmatch(args.expected_sha256):
            raise EvalInputError("Invalid expected SHA-256")
        if args.expected_sha256 and not args.model_id:
            raise EvalInputError("A single-artifact SHA requires --model-id")
        cases = load_cases(args.cases)
        if args.model_id:
            subset = [c for c in cases["cases"] if c["model_id"] == args.model_id]
            if not subset:
                raise EvalInputError("Unknown --model-id for this corpus")
            cases = {**cases, "cases": subset}
        responses = load_responses(args.responses, known_ids={c["id"] for c in cases["cases"]})
        report = score(cases, responses, expected_sha256=args.expected_sha256)
    except (EvalInputError, OSError, UnicodeError) as exc:
        print("Evaluation input rejected: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["lexical_fail"] or report["unscored"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
