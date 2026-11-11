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

### Optional Mudlet / ANSI presentation

After authentication, enter `color on` to enable colors for **this socket only**.
Use `palette` (`colors` or `colours`) to preview named swatches; `color off`
(or `colour off`) restores plain text. New sessions start without ANSI.
Room titles, exits, items and inventory use distinct colors. Named colors in
**room descriptions only** are highlighted, including `copper`, `amber`,
`pink`, `cyan`, and `light grey`; player chat is not semantically recolored.
No room, player, credential or SQLite data changes when toggling colors.

This generic, opt-in renderer adapts the palette concept from
[Driftspace's public-safe terminal PR #4](https://github.com/thebrazenbeard/driftspace/pull/4),
without importing its setting, pilot-only social receipts or AI crew claims.
It is not automatic Telnet capability negotiation or remote-transport approval.

An **opt-in host-provisioned player-identity mode** accepts a distinct token
per player and never accepts a network-supplied player name:

```bash
PYTHONPATH=src python -m realm.telnet --db data/demo.sqlite3 --port 7777 --player-credentials
```

The host first provisions tokens against the same world database using
`CredentialStore`. The server checks credentials at login, before commands,
and inside the writer transaction. See
[`docs/TELNET_PLAYER_IDENTITY.md`](docs/TELNET_PLAYER_IDENTITY.md) for the
provisioning and revocation contract.

On PowerShell, set `$env:PYTHONPATH='src'` before launching the module.

## Non-negotiable limitations

- **Loopback only (`127.0.0.1`).** The server has no option to bind publicly,
  even if the environment or invoking client is configured differently.
- **Telnet is plaintext**, including the connection token. Do not expose this
  port via tunnels, port forwarding, reverse proxies, or public interfaces.
- In **legacy mode**, the shared token gates local server access, **not player identity**. Anyone who
  knows it can choose an existing player's name *after that player disconnects*.
  An active local socket now exclusively reserves its case-insensitive name;
  that reservation is released on `quit`, EOF, timeout, or socket error. This
  is **not** durable account ownership or authenticated player identity.
  Opt-in player credentials improve server-side identity and revocation but
  **do not encrypt Telnet** or make it safe for public deployment.
- No automatic live push of other players' actions in this initial adapter;
  issue `listen` to see durable room-local events. No SSH/GMCP protocol yet.
- Access is capped to eight simultaneous local connections; oversized input
  is rejected; Telnet option negotiation is refused and stripped.
- This adapter is included in the integrated candidate alongside the Python
  engine, HTTP gateway, and browser source; no public internet service or
  real-browser compatibility is qualified by local regression tests. Telnet
  uses the shared command router for optional host-provisioned crafting
  commands without automatically installing crafting fixtures.

Run socket-level tests with `python -m unittest discover -s tests -v`.

For actual remote players, design a distinct authenticated TLS/WebSocket or
SSH endpoint; use player-bound sessions, rate limits, authorization, and audit
before granting access. GitHub Pages remains a static browser client and
cannot host this server.
