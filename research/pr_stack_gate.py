"""Read-only REALM draft-PR branch-tip and ancestor gate; never release authority."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate(manifest):
    if manifest.get("schema") != "realm.pr_stack_gate.v1":
        raise ValueError("unsupported manifest schema")
    if manifest.get("release_authority") != "HOLD_NOT_GRANTED":
        raise ValueError("manifest cannot grant release")
    prs = manifest.get("prs")
    if not isinstance(prs, list) or not prs:
        raise ValueError("empty PR snapshot")
    seen = set()
    branches = set()
    for pr in prs:
        if not isinstance(pr.get("n"), int) or pr["n"] in seen:
            raise ValueError("duplicate/invalid PR number")
        seen.add(pr["n"])
        for field in ("head_sha", "base_sha"):
            if not _SHA.fullmatch(pr.get(field, "")):
                raise ValueError("invalid SHA")
        for field in ("head_ref", "base_ref"):
            ref = pr.get(field, "")
            if not _REF.fullmatch(ref) or ".." in ref or "//" in ref:
                raise ValueError("invalid branch ref")
        if pr.get("release") != "DRAFT_UNMERGED_HOLD":
            raise ValueError("PR lacks release HOLD")
        if pr["head_ref"] in branches:
            raise ValueError("duplicate PR head branch")
        branches.add(pr["head_ref"])
    return prs


def assess(manifest, branch_tips, is_ancestor):
    """Return evidence only; passing a branch gate never authorizes deployment."""
    records = []
    for pr in validate(manifest):
        h = branch_tips.get(pr["head_ref"])
        b = branch_tips.get(pr["base_ref"])
        flags = []
        if not h or not b:
            flags.append("BRANCH_NOT_OBSERVED")
        else:
            if h != pr["head_sha"]:
                flags.append("HEAD_MOVED_REVIEW_STALE")
            if b != pr["base_sha"]:
                flags.append("BASE_MOVED_REVIEW_STALE")
            try:
                if not is_ancestor(b, h):
                    flags.append("CURRENT_PARENT_NOT_ANCESTOR")
            except (OSError, subprocess.SubprocessError, ValueError):
                flags.append("ANCESTRY_UNVERIFIED")
        records.append({"pr": pr["n"], "head": h, "base": b,
                        "status": "PASS_BRANCH_EVIDENCE" if not flags else "HOLD",
                        "findings": flags})
    return {"schema": manifest["schema"], "release": "HOLD_NO_OWNER_AUTHORITY",
            "branch_gate": "PASS" if all(r["status"] == "PASS_BRANCH_EVIDENCE"
                                         for r in records) else "HOLD",
            "results": records,
            "limitations": ["git refs/ancestry only; no GitHub PR state or approvals",
                            "not a source code test, human canon release, merge or deployment"]}


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=30, check=False)
    return p.returncode, p.stdout, p.stderr


def live_tips(repo, refs):
    names = sorted(set(refs))
    rc, out, err = git(repo, "ls-remote", "origin",
                       *(f"refs/heads/{ref}" for ref in names))
    if rc != 0:
        raise RuntimeError(f"git remote lookup failed: {err.strip()}")
    tips = {}
    for row in out.splitlines():
        parts = row.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if _SHA.fullmatch(sha) and ref.startswith("refs/heads/"):
            tips[ref[len("refs/heads/"):]] = sha
    return tips


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path,
                        default=Path(__file__).with_name("pr_stack_20261107.json"))
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        prs = validate(manifest)
        names = [pr[key] for pr in prs for key in ("head_ref", "base_ref")]
        tips = live_tips(args.repo, names)
        def ancestor(parent, child):
            rc, _, stderr = git(args.repo, "merge-base", "--is-ancestor", parent, child)
            if rc == 0:
                return True
            if rc == 1:
                return False
            raise RuntimeError(stderr.strip())
        result = assess(manifest, tips, ancestor)
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"branch_gate": "UNVERIFIED", "release": "HOLD",
                          "error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["branch_gate"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
