# REALM draft PR stack-currentness gate

Source-only, read-only engineering guard. Snapshot captured **2026-11-07** from live GitHub PR metadata; it is not dynamic authority. It does **not** prove code quality, PR approval, merge safety, canon release, medical finality, user consent, or deployment.

Run on an authorized clone with access to `origin`:

```powershell
py -3 -m research.pr_stack_gate
py -3 -m unittest tests.test_pr_stack_gate -v
```

The command gets branch tips using `git ls-remote` and checks that each **current base branch head is an ancestor of the current head** using local git objects. It also checks exact manifest SHA binding; a moved head/base voids previous review subject even when GitHub says `mergeable=true`. Missing objects/refs fail closed. It makes **no remote or working-tree writes**, performs no fetch and never self-authorizes a release. Refresh the manifest only after independently verifying revised PR heads/branches and re-running the source/host tests.

**Known observed HOLD (2026-11-07):** REALM PR #61 current head `33bcac7`, but stacked PR #62 base metadata still `a431e391`; `git merge-base --is-ancestor 33bcac7 f431946` exited **1**. PR #62 therefore lacks its current parent revision, despite a GitHub `mergeable=true` response. Creator descendants #65→#73→#77→#78 require post-reconciliation qualification, even though their immediate recorded bases have not moved.

**All PRs are release HOLD.** A green *branch graph* would only show this narrow topology at an exact moment. It is not a sign-off to merge, run private Wreckforge ingestion, stage canon, deploy VN2, expose the HTTP server, finalize irreversible death, or spend money. Owner release and independent reviewed runtime evidence are separate gates.
