# REALM live PR currentness preflight

This source-only tool **fails closed** when the branch gate's dated snapshot is not
provably current against GitHub. The original PR #93 branch gate remains intact.

```powershell
py -3 -m research.pr_stack_live
py -3 -m unittest discover -s tests -p test_pr_stack_live.py
```

The wrapper checks:
- Effective `origin` is exactly canonical HTTPS `github.com/thebrazenbeard/REALM[.git]`, not a lookalike, SSH or a user-info URL.
- Canonical public GitHub REST open-PR metadata: number, open, draft, exact
  base and head repo/ref/SHA; malformed/unavailable API state yields UNVERIFIED.
- Canonical GitHub `Link: rel="next"` pagination is followed even if the
  preceding page contains fewer than 100 records. All separate HTTP Link
  header fields are combined before validation; first-header-only parsing can
  conceal later pages. Foreign, cyclic, skipped, malformed and duplicated
  Link relations fail closed. A missing Link header
  on a dishonest truncated upstream response cannot be independently detected
  by this source-only check; its output remains evidence, not authority.
- Every manifest PR must match. Newly opened PRs numbered within the snapshot's
  bounded sequence (PR #61+) yield `UNTRACKED_OPEN_PR` and HOLD.
- Exact live git branch tips and ancestry are still checked by the PR #93 gate.
The live public API is queried without embedded credentials. An API/network rate
limit, exception, moved head, missing branch, non-draft PR, or unauthorized origin
is NOT a green verdict. Dated snapshots and GitHub `mergeable` are not authority.

**Observed on 2026-11-07:** the PR #93 27-PR snapshot lacks newer
PRs #92 onward; PR #62 also omits latest PR #61 parent ancestry.
The live gate therefore returns `source_gate=HOLD`, as intended.

This gate cannot certify that a local git object was fetched from an incorruptible
remote, that an untracked older PR is safe, or that a green outcome grants owner
consent, rights, canon, merge authority, irreversible death or deployment.
It does not mutate production/world state, Git refs, or remote PRs.
