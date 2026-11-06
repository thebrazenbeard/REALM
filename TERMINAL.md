# REALM local terminal transport (experimental)

REALM's existing `python -m realm` client runs in the local terminal and shares
the same SQLite world file. The optional adapter in `realm.telnet` accepts
classic Telnet-style clients without installing third-party dependencies.

```bash
PYTHONPATH=src python -m realm.telnet --db data/demo.sqlite3 --port 7777
telnet 127.0.0.1 7777
```

The server displays an ephemeral access token **in the terminal where the
server started**. Enter it at the `Token:` prompt, then enter a player name.
Set `REALM_TELNET_TOKEN` in the server process environment if you need a stable
test token. Run two clients to share movement, speech, item ownership and
history. Use `help`, `look`, `n`, `say hello`, `listen`, `who`, `quit`.

On PowerShell, set `$env:PYTHONPATH='src'` before launching the module.

## Non-negotiable limitations

- **Loopback only (`127.0.0.1`).** The server has no option to bind publicly,
  even if the environment or invoking client is configured differently.
- **Telnet is plaintext**, including the connection token. Do not expose this
  port via tunnels, port forwarding, reverse proxies, or public interfaces.
- The token gates local server access, **not player identity**. Anyone who
  knows it can choose an existing player's name *after that player disconnects*.
  An active local socket now exclusively reserves its case-insensitive name;
  that reservation is released on `quit`, EOF, timeout, or socket error. This
  is **not** durable account ownership or authenticated player identity. There
  is no account password, permission system, encryption, or moderation yet.
- No automatic live push of other players' actions in this initial adapter;
  issue `listen` to see durable room-local events. No SSH/GMCP protocol yet.
- Access is capped to eight simultaneous local connections; oversized input
  is rejected; Telnet option negotiation is refused and stripped.
- `main` still contains only the original speculative README. This transport
  stacks on the first playable engine Draft PR #1. It must be integrated and
  retested with the separately proposed HTTP bridge/browser console.

Run socket-level tests with `python -m unittest discover -s tests -v`.

For actual remote players, design a distinct authenticated TLS/WebSocket or
SSH endpoint; use player-bound sessions, rate limits, authorization, and audit
before granting access. GitHub Pages remains a static browser client and
cannot host this server.
