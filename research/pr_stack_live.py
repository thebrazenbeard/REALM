"""Read-only canonical live GitHub preflight for the REALM PR ancestry gate.

No merge, deployment, publication or release authority is conveyed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from research.pr_stack_gate import assess, git, live_tips, validate

REPO = "thebrazenbeard/REALM"
API = "https://api.github.com/repos/thebrazenbeard/REALM/pulls"
SHA = re.compile(r"^[0-9a-f]{40}$")


def canonical_origin(value: str) -> bool:
    try:
        url = urlsplit(value.strip())
        return (url.scheme == "https" and url.netloc == "github.com"
                and url.username is None and url.password is None
                and url.port is None and not url.query and not url.fragment
                and url.path in ("/thebrazenbeard/REALM",
                                 "/thebrazenbeard/REALM.git"))
    except (ValueError, AttributeError):
        return False


def validate_live_pr(pr: dict):
    if (not isinstance(pr, dict) or type(pr.get("number")) is not int
            or pr["number"] < 1 or pr.get("state") not in ("open", "closed")
            or type(pr.get("draft")) is not bool):
        raise ValueError("malformed GitHub PR identity/state")
    for key in ("head", "base"):
        v = pr.get(key)
        if (not isinstance(v, dict) or not isinstance(v.get("ref"), str)
                or not SHA.fullmatch(v.get("sha", ""))):
            raise ValueError(f"malformed GitHub PR {key}")
        repo = v.get("repo")
        if not isinstance(repo, dict) or not isinstance(repo.get("full_name"), str):
            raise ValueError(f"unbound GitHub PR {key} repo")
    return pr


def assess_live(manifest: dict, observed: dict[int, dict]):
    expected = {p["n"]: p for p in validate(manifest)}
    findings = []
    for number, snapshot in expected.items():
        pr = observed.get(number)
        if pr is None:
            findings.append(f"LIVE_PR_MISSING:{number}")
            continue
        validate_live_pr(pr)
        if pr["state"] != "open" or pr["draft"] is not True or pr.get("merged_at"):
            findings.append("NOT_OPEN_DRAFT")
        if (pr["head"]["repo"]["full_name"] != REPO
                or pr["base"]["repo"]["full_name"] != REPO):
            findings.append(f"FOREIGN_PR_REPO:{number}")
        for part in ("head", "base"):
            if pr[part]["sha"] != snapshot[f"{part}_sha"]:
                findings.append(f"LIVE_{part.upper()}_MOVED" +
                                (f":{number}" if part == "base" else ""))
            if pr[part]["ref"] != snapshot[f"{part}_ref"]:
                findings.append(f"LIVE_{part.upper()}_REF_MOVED:{number}")
    # This manifest explicitly audits PR #61 onward; older drafts are outside it.
    lower_bound = min(expected)
    for number in sorted(set(observed) - set(expected)):
        if number >= lower_bound:
            findings.append(f"UNTRACKED_OPEN_PR:{number}")
    return {"pr_gate": "HOLD" if findings else "PASS",
            "release": "HOLD_NO_OWNER_AUTHORITY",
            "observed_open": len(observed), "scope_from_pr": lower_bound,
            "manifest_count": len(expected), "findings": findings}
def fetch_live_open_prs(api: str = API):
    """Query the canonical public HTTPS API, fail closed on errors or truncation."""
    observed = {}
    for page in range(1, 11):
        uri = f"{api}?state=open&per_page=100&page={page}"
        req = Request(uri, headers={"User-Agent": "REALM-source-review-gate",
                                    "Accept": "application/vnd.github+json"})
        with urlopen(req, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"GitHub API HTTP {response.status}")
            payload = json.load(response)
        if not isinstance(payload, list):
            raise ValueError("GitHub API did not return PR list")
        for item in payload:
            pr = validate_live_pr(item)
            number = pr["number"]
            if number in observed:
                raise ValueError("duplicate GitHub PR in API pages")
            observed[number] = pr
        if len(payload) < 100:
            return observed
    raise RuntimeError("GitHub open-PR pagination cap exceeded")


def verify_origin(repo: Path):
    rc, out, err = git(repo, "remote", "get-url", "origin")
    if rc or not canonical_origin(out.strip()):
        raise RuntimeError("uncanonical/unverifiable git origin; refuse lookup")
    return out.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path,
                        default=Path(__file__).with_name("pr_stack_20261107.json"))
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        records = validate(manifest)
        origin = verify_origin(args.repo)
        observed = fetch_live_open_prs()
        github_result = assess_live(manifest, observed)
        refs = [rec[key] for rec in records for key in ("head_ref", "base_ref")]
        tips = live_tips(args.repo, refs)
        def is_ancestor(parent, child):
            rc, _, err = git(args.repo, "merge-base", "--is-ancestor", parent, child)
            if rc == 0:
                return True
            if rc == 1:
                return False
            raise RuntimeError(f"Git ancestry unavailable: {err.strip()}")
        branch_result = assess(manifest, tips, is_ancestor)
        result = {"schema": "realm.pr_stack_live.v1", "origin": origin,
                  "source": github_result, "branches": branch_result,
                  "release": "HOLD_NO_OWNER_AUTHORITY",
                  "source_gate": "PASS" if github_result["pr_gate"] == "PASS"
                     and branch_result["branch_gate"] == "PASS" else "HOLD"}
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        result = {"schema": "realm.pr_stack_live.v1", "source_gate": "UNVERIFIED",
                  "release": "HOLD_NO_OWNER_AUTHORITY", "error": str(exc)}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["source_gate"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
