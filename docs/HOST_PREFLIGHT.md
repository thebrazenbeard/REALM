# Read-only local host preflight

The optional `realm.host_preflight` check reports static prerequisites for one
local REALM world. It **does not** start the HTTP/Telnet servers or UTC clock,
create or migrate a database, install software, inspect credentials, qualify
TLS, or authorize Internet access. A passing check is not a deployment gate.

From the repository root:

```sh
PYTHONPATH=src python -m realm.host_preflight --db data/world.sqlite3 --expected-world-id my_world --json
```

On Windows PowerShell:

```powershell
$env:PYTHONPATH = 'src'
py -3 -m realm.host_preflight --db 'D:\VERA\realm-data\world.sqlite3' --expected-world-id my_world --json
```

Local listeners must use exactly `127.0.0.1`. `--json` returns `ready`, `mode`
and path-redacted findings; exit code `0` means no fatal **static** finding,
and exit code `2` means at least one fatal finding.

A missing database is a warning: a separately authorized first startup may
create it. Missing directories, corrupt SQLite, altered core schema, a broken
symlink, or mismatched world identity are fatal observations. An existing
unbound world is a warning; this check never binds or rebinds it.

> Existing SQLite `-wal` or `-shm` sidecars make the check **unverified** and
> therefore fatal. Immutable read-only inspection may miss uncheckpointed
> records. Stop the owning process through its normal shutdown protocol;
> never delete sidecars or coerce a passing report.

Permissions checked with `os.access()` are advisory, especially on Windows
ACLs. This check does not bind a probe port (which would change host state),
test a remote client, prove host TLS credentials, or qualify model inference.

Focused verification:

```sh
python -m unittest discover -s tests -p test_host_preflight.py -v
```
