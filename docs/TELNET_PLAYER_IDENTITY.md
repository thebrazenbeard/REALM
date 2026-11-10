# REALM Telnet player identity (opt-in)

REALM's Telnet transport has two distinct modes. Neither is suitable for direct
public-internet exposure: Telnet transmits credentials and game text **in
plaintext**. The listener is hard-bound to `127.0.0.1`. Do not port-forward or
bind a public-facing proxy to it. A secure transport, authentication policy,
rate limits, and a reviewed deployment configuration would be required for a
remotely accessible service.

## Player-bound local mode

To provision a player **locally on the host**, use the same world database that
the server will open. Keep the returned bearer credential private:

```python
from realm.auth import CredentialStore
from realm.engine import Engine

store = CredentialStore(Engine("world.sqlite3"))
alice_token = store.provision("Alice")
# Deliver alice_token privately to Alice. Do not put it in logs, Git, or chat.
```

Then start a local listener:

```sh
PYTHONPATH=src python -m realm.telnet --db world.sqlite3 --player-credentials
```

The Telnet socket asks for a `Player token:` and resolves the canonical player
name server-side from that world's `CredentialStore`. It **never** asks the
network client which name to assume. Each successful login owns one active
socket slot for that case-insensitive player name on this server instance.
The `CommandRouter` executes the same engine and optional crafting commands
as other transports.

The host can call `store.rotate("Alice")` to issue a replacement credential,
or `store.revoke("Alice")` to invalidate it. A connected socket is checked
again before every command; importantly, the credential is also revalidated
**inside the SQLite writer transaction** for join, world actions, and crafting.
A rotation/revocation that commits before the next world action's writer
transaction rejects that action. Already committed actions cannot be undone;
no guarantee is made about revoking bytes already sent to the network.

Credentials are scoped to the selected SQLite world database. A token issued
for one world is not an authorization for another world. A shared database or
copied credentials table is intentionally the *same* credential authority,
not a separate security domain.

## Legacy compatibility (insecure player identity)

The original positional API remains supported for existing local clients:

```python
server = LocalTelnetServer(engine, "shared-local-token", 7777)
```

Without `--player-credentials`, the local client supplies a **shared server
token** and chooses a player **Name**. It does not authenticate ownership of
that name: anyone holding the shared token can impersonate an offline player.
Do not treat the name lock on an active socket as a durable identity guarantee.

## Explicit non-goals

- No account creation, registration, password reset, or player credential
  retrieval over Telnet.
- No claim of TLS, network isolation beyond loopback binding, abuse throttling,
  or remote-user authentication suitable for production.
- No automatic migration from client-chosen legacy names to authenticated
  player ownership. Hosts must explicitly provision player credentials.
- No cross-process active-player locking; the session exclusivity lock is per
  listener instance. The durable transaction guard checks credential validity
  on the world database, not global socket uniqueness.

Socket-level tests in `tests/test_telnet_player_binding.py` cover canonical
speaker assignment, invalid and cross-world tokens, active-session name
exclusivity, revocation, rotation across the precheck/write gap, and crafting
dispatch. The preexisting legacy tests remain mandatory for compatibility.
