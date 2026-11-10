# Repository folder-open execution and fake asset guard

REALM's runtime is not intended to run a command merely because a developer
opens the repository in an editor. That applies to the editor's workspace
configuration as well as to Python, JavaScript, and worldpack content.

## Why this check exists

A later draft-review branch substituted an executable JavaScript file for a
`.ttf` font asset and added a hidden VS Code `folderOpen` task invoking that
asset through Node. The same branch enabled workspace automatic tasks in
settings. The attacker-controlled bytes **must not be executed to establish
the defect**: a folder-open command and a non-font executable masquerading as
font data are independently detectable from source.

The trusted local-source baseline is wave-6 commit
`fbfc9d1dc97665260aa918f00f311f7384098a46`. The suspect replacement
heads reported during review were `bf0aa5d48ca7e5e6d7e3dd32ee9e00a8e76efac8`
and `e6ba4c391d3ea5a8e7dd0d51c8cb86e0ae7fb8f0`. These are *source
identifiers*, not authorization to check out or run a suspect branch.

## Static regression

From a trusted REALM checkout:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_repo_autorun_asset_guard.py' -q
```

`tests/test_repo_autorun_asset_guard.py` reads but never executes repository
content. It rejects:

- VS Code JSON configurations in `.vscode/` that contain a `folderOpen`
  command-run setting or enable automatic task approval (`true` or `"on"`).
  Linked JSON configs and Unicode-escaped JSON config values/keys also fail
  closed pending manual review.
- Font-named files under `public/fonts/` whose structure does not match a
  plausible TrueType/OpenType table directory and `head` table. WOFF and WOFF2
  assets fail closed pending an independent structural font parser and manual
  review: matching only their magic/length fields admits JavaScript polyglots.
  Linked font files also fail closed.

To inspect a separate, **untrusted** checkout without importing or executing
anything from it, keep the test in a trusted source tree and set
`REALM_STATIC_SECURITY_ROOT` to the path under inspection. The environment
override is for offline evidence gathering only. Do not use the editor's
"Open Folder" workflow on an untrusted checkout to run this check.

## Authority and coverage limits

The check is a bounded repository regression, **not** a full font parser,
complete malware scan, binary authenticity verification, or proof a checkout was never
opened by an affected editor. Attackers can hide executable behavior in other
assets, package hooks, extensions, CI scripts, or application code. Review
incoming branch diffs, check exact Git provenance, and run tests using a
trusted interpreter outside automatic editor task machinery.

When a compromised branch is encountered, preserve its immutable commit hash
for analysis, do not execute its workspace commands, and rebuild review work
from the last verified clean tree. Do not merge, deploy, publish credentials,
or automatically install any code until source custody is resolved.
