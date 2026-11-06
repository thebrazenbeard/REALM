# REALM Android client — native terminal design and acceptance gate

**Status:** native Kotlin source scaffold in [`clients/android/`](../clients/android/README.md),
**not built or installed**. [Issue #18](https://github.com/thebrazenbeard/REALM/issues/18)
is an independent client-only build lane. The source currently includes a
bounded raw Telnet *local-only* compatibility adapter and an HTTPS-first v1
gateway adapter. This is **not** evidence of an APK, a reachable remote REALM
host, or a test on Patrick's physical phone.

REALM means **Roleplaying Engine for Autonomous Living MUDs**. Its eventual
neural residents, drives, memory, and simulation happen on the authoritative
world host. The Android app only presents text-world observations and submits
actions as an authorized player. It must work for any compatible creator-owned
world, not carry a specific worldpack, Driftspace canon, model, or Python server.

## Architectural decision

Use **native Kotlin + Jetpack Compose** for the first dedicated Android APK.
Keep the UI terminal-shaped: selectable monospaced transcript, legible ASCII
map, command prompt, history, shortcuts, high-contrast themes, adjustable text
size, and honest connection status. Compose `LazyColumn` can virtualize a large
scrollback, `BasicTextField` supports soft/hardware keyboard input and IME
actions, and Compose semantics support TalkBack. An ASCII map must preserve
spacing, scroll horizontally when necessary, and not be silently reflowed.

Reference: [Compose lists](https://developer.android.com/develop/ui/compose/lists),
[text input](https://developer.android.com/develop/ui/compose/text/user-input),
and [accessibility semantics](https://developer.android.com/develop/ui/compose/accessibility/semantics).

Proposed long-term client module boundaries (the current source scaffold is
smaller, inside `clients/android/app/`):

```text
clients/android/
  app/                 # Compose presentation, input, accessibility
  protocol/            # Versioned request/reply DTOs, RealmTransport
  security/            # Host profiles, token protection, TLS policy
  cache/               # Scoped, nonauthoritative transcript and drafts
  tests/               # Fake transport, failure/reconnect, UI contracts
```

Neither a folder nor a separate APK creates a network server. The eventual
connection is `Android UI -> authenticated HTTPS transport -> qualified REALM
host gateway -> engine validator/transaction -> one world SQLite instance`.
Keep `RealmTransport` independent of rendering and models, so HTTP polling can
eventually be replaced or supplemented by authenticated WebSocket push without
changing gameplay UI or trusting the client to resolve state conflicts.

| Approach | Decision | Reason |
| --- | --- | --- |
| Kotlin/Compose | **Build lane** | Native IME, accessible text, app-lifecycle control, explicit secret handling and transport boundaries. |
| WebView wrapping `docs/app.js` | Preview only | Reuses existing browser work but adds JS-origin, file-access and native-bridge risks; not the target APK implementation. |
| Termux/plain Telnet | Local compatibility smoke only | It is not REALM's own Android client, and current Telnet is plaintext with a shared connection gate, not durable player authentication. |

If a WebView experiment is later authorized, load packaged assets through
[WebViewAssetLoader](https://developer.android.com/develop/ui/views/layout/webapps/load-local-content),
avoid a JavaScript interface exposing tokens, and never bypass TLS errors. See
[Android's native-bridge risk guidance](https://developer.android.com/privacy-and-security/risks/insecure-webview-native-bridges).

## Current host contract versus proposed client behavior

The current source of truth is [`docs/GATEWAY_PROTOCOL.md`](GATEWAY_PROTOCOL.md),
[`TERMINAL.md`](../TERMINAL.md), and executable host tests, not this proposal.

| Capability | Current REALM host | Android proposal / blocker |
| --- | --- | --- |
| World identity | One SQLite file per instance; gateway reports configured `world_id`. | Host profile includes verified endpoint, world ID, player identity; never share cache/credentials across profiles. |
| Player credential | Host-provisioned, player-bound bearer token; digest stored server-side; rotation/revocation supported. | Secret entry and protected per-profile storage; token only in `Authorization` header. |
| Commands | Provisional `/api/v1/join`, `/command`, `/room`, `/feed`, `/map`; optional `action_id` on commands. | Typed, cancellable HTTP adapter for v1 **only after safe endpoint exists**; plan explicit `/api/v2` handshake/versioning. |
| Events | Room-scoped polling (up to 100 per response), not server push. | Poll by cursor while active, deduplicate event IDs, detect gaps; don't claim exhaustive world history. |
| Online presence | Persisted last location is not an online/offline signal. | Show connection state accurately; do not infer other players are online from `who`. |
| Remote transport | HTTP and Telnet bind `127.0.0.1` on **the host**; neither is qualified for public/LAN exposure. | Block real remote connection testing until a separately qualified TLS gateway, host identity, limits and authorization exist. |
| NPCs / deep memory / real-time simulation | Future goals, not shipped neural NPCs or push streams. | Render server-committed events when available; no local model/world authority. |

`127.0.0.1` on an Android phone points to the **phone**, not Lappy. Hosting
static HTML on GitHub Pages also does not run the Python engine. Do not
port-forward, tunnel, or expose the current development listeners simply to
make the APK connect.

### Proposed versioned transport API

`RealmTransport` needs operations resembling `health`, `join`, `command`,
`currentRoom`, `feedAfter`, `map`, and `disconnect`, with typed results and
explicit errors. The current v1 payload is provisional; do **not** silently
invent a stable v2 endpoint or confuse JSON command bodies with direct SQL.

- Every mutation has a client-generated, unique `action_id`; a retry uses the
  **same** ID and **same** command. A different command with an old ID must
  fail. A timeout does not prove that a command failed or succeeded.
- Attach a client-side connection generation to requests. On host switch,
  player switch, reconnect, or token rotation, cancel outstanding requests and
  discard any reply from an earlier generation. This is UI race protection,
  not substitute authorization.
- A new session must first authenticate, confirm the actual world/character,
  fetch authoritative current room, and reconcile events against its scoped
  cursor. If the server cannot prove a complete replay, label the transcript
  partial and fetch current state; never synthesize missing history.
- A future `wss://` stream may carry event IDs and resume cursors; it still
  needs gap/backfill logic, bounded queues, replay tests, and server-side
  authorization. WebSocket push is **not implemented**.

## Credentials, transport, and Android permissions

1. Accept only explicitly configured, authenticated `https://` host URLs for
   remote play (eventually `wss://` for push). Reject `http://`/`ws://`, bad
   certificates, hostname mismatch, and unexpected redirects. Never expose a
   button to continue past a TLS trust failure. A VPN/overlay by itself is not
   proof that the app authenticated the intended REALM host.
2. Store each host/player bearer credential as an encrypted, app-private blob
   using an Android Keystore-backed symmetric key (for example AES-GCM), or
   require fresh entry if secure persistence is not configured. Exclude secrets
   from **both cloud backup and device-to-device transfer**. Handle key loss
   after reinstall/restore by asking the host to reprovision, not inventing a
   reusable backup token. AndroidX `EncryptedSharedPreferences` is deprecated;
   don't select it uncritically for a new implementation.
3. Never put tokens in URLs, saved command history, analytics, logcat,
   exception reports, Git, QR-code screenshots or bundled assets. Keep host
   profiles independent. Redact the token before writing any diagnostic.
4. Use Android Network Security Configuration with cleartext disabled. Keep
   debug-only trust anchors isolated from release builds; no `trustAll` TLS
   clients. A valid HTTPS certificate and authorized player do not eliminate
   the need for host-side rate limits, moderation, session lifecycle and
   permission review.
5. Android 17 (API 37) apps **targeting SDK 37 or later** require the runtime
   `ACCESS_LOCAL_NETWORK` permission for direct LAN sockets, unless using an
   appropriate system-mediated connection picker. For apps targeting SDK 36
   or earlier, `INTERNET` currently carries a temporary implicit grant; do
   not rely on that for a future target bump. Explain the LAN requirement in
   context, handle denial and revocation, and distinguish permission failures
   from host/network failures. Test LAN, internet, and any private overlay
   independently; the permission behavior of a particular Tailscale route is
   not established by this document.

Primary sources: [Android Keystore](https://developer.android.com/privacy-and-security/keystore),
[Auto Backup and exclusion rules](https://developer.android.com/identity/data/autobackup),
[deprecated EncryptedSharedPreferences](https://developer.android.com/reference/androidx/security/crypto/EncryptedSharedPreferences),
[Network Security Configuration](https://developer.android.com/privacy-and-security/security-config),
and [Android local network permission](https://developer.android.com/privacy-and-security/local-network-permission).

## Offline-first means *readable*, not locally authoritative

Only the host commits gameplay state. Offline mode may show a **clearly stale**
last-observed transcript/map and let a user prepare an unsent command draft.
It must not simulate inventory changes, award loot, claim online presence,
continue NPC actions, or automatically replay arbitrary queued mutations.
Android's generic [offline-first guidance](https://developer.android.com/topic/architecture/data-layer/offline-first)
describes write queues, but applying one naively to contested multiplayer
actions would create duplicate or misleading outcomes.

Cache keys must bind at least `{verified host identity, world_id, player,
protocol version}`. Cached text is a *view*, not a second state database.
Cap retention/size, treat world prose as sensitive, support per-profile purge,
and exclude cached world history from backup by default. Show `Offline — last
seen <timestamp>` with unambiguous stale styling. Switching profiles must
clear the visible prior transcript before new authorization.

On resumed connectivity, reauthenticate, compare the host/world/player
identity, refresh the room and poll from a valid cursor. If a `take` request
timed out, query the host/receipt before ever offering a same-ID retry. The
screen must not optimistically declare ownership of a contested item.

## Terminal text and optional compatibility protocols

Render remote room descriptions, player speech and future model-generated
NPC text as **inert Compose text**. Do not interpret HTML, hyperlinks, terminal
control sequences, shell commands or file URLs from the world. If cosmetic
ANSI support is added, allow only strictly bounded visual SGR colors and
reset, after parsing/escaping; reject OSC, DCS, cursor movement, screen
erase, title changes and OSC 52 clipboard requests. ASCII maps should be
plain glyph data with known bounds, not an arbitrary terminal program.

Reference: [ECMA-48](https://ecma-international.org/publications-and-standards/standards/ecma-48/),
[xterm control sequences](https://www.x.org/docs/xterm/ctlseqs.pdf), and
[OWASP log injection](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html).

**Do not make Telnet or GMCP a dependency of native Android v1.** The current
Telnet adapter rejects negotiated options, sends a shared plaintext gate
token, and does not provide durable player authentication or GMCP. It is
loopback-only. For future classic-client compatibility:

- [RFC 854](https://www.rfc-editor.org/rfc/rfc854.html) defines Telnet NVT,
  IAC escaping and CRLF/CRNUL; [RFC 855](https://www.rfc-editor.org/rfc/rfc855.html)
  defines WILL/WONT/DO/DONT; [RFC 1143](https://www.rfc-editor.org/rfc/rfc1143.html)
  gives a negotiation state machine to avoid option loops. Test chunk-split
  escapes and redundant acknowledgements before claiming compliance.
- [GMCP](https://mudstandards.org/mud/gmcp/) is a *community* Telnet option
  201 extension for UTF-8 JSON side-channel messages. The
  [Core package](https://mudstandards.org/gmcp/core/) covers `Core.Hello`,
  `Core.Supports.Set`, ping and goodbyes. Package names are not JSON-key
  names; version/encoding and message-size validation are mandatory.
- [IANA's Telnet option registry](https://www.iana.org/assignments/telnet-options)
  lists 141–254 as unassigned: option 201 is de facto MUD interoperability,
  **not** an IANA-registered standard. Don't advertise implemented GMCP now.

## Implementation sequence (no deployment implied)

1. **UI contract first:** fake `RealmTransport` with recorded, noncanonical
   fixture replies; test long text, ANSI attacks, ASCII alignment, keyboard,
   theme, accessibility and disconnected presentation without any host.
2. **Security/data layer:** separate per-host/player profiles; Keystore-backed
   secret, no-backup scoped cache, cleartext rejection, certificate failure,
   stale-response cancellation, event deduplication and receipt reconciliation.
3. **Host qualification (separate owner-approved work):** design/implement an
   authenticated TLS `/api/v2` or carefully versioned adapter, host identity,
   limits, token/session lifecycle and remote two-player tests. The present
   loopback gateway alone does not meet this gate.
4. **Physical device acceptance:** locally build a debug APK, install only on
   Patrick's explicitly authorized Android phone, record exact APK hash,
   source revision, Android version, network path, server revision and observed
   pass/fail evidence. Emulator tests are useful but cannot substitute.
5. **Later:** authenticated WebSocket push with backfill; optional classic
   Telnet/GMCP client compatibility if independently useful. Neither belongs
   in the first Android APK acceptance gate.

Local `$0` build path: Android SDK/JDK/Gradle wrapper, `assembleDebug` and
`adb install` after device consent. Official references:
[command-line build](https://developer.android.com/build/building-cmdline)
and [ADB](https://developer.android.com/tools/adb). No Google Play listing,
paid signing, cloud inference, telemetry or production host is part of this
plan.

## Acceptance matrix — proposed tests, not results

| Gate | Procedure | Passing evidence |
| --- | --- | --- |
| A1: identity | Two invited players on independent clients; attempt wrong-player query/action and host switch while request is pending. | 401/403 or explicit denial for wrong identity; zero cross-profile events/cache; stale request discarded. |
| A2: contested action | Disconnect immediately after submitting a `take` with `action_id`; reconnect and reconcile on host. | Exactly one committed item transfer; no duplicate receipt/effect or optimistic victory. |
| A3: transport | Try plaintext, invalid CA, wrong hostname, captive portal and server redirect. | All untrusted endpoints fail closed, credentials never sent to them. |
| A4: token lifecycle | Rotate/revoke a token; recover from device key invalidation. | Old token rejected; new authorization required; no plaintext tokens in logs/backup. |
| A5: offline | Airplane mode before and during commands; resume, switch to second host, kill process and restore. | Stale cache labeled, drafts unsent, no hidden mutation replay, no prior identity content on another profile. |
| A6: events | Deliver duplicate, missing, delayed, out-of-order and old-session feed replies; change room while offline. | Ordered/deduplicated visible events, gap or partial-history warning, authoritative room refresh. |
| A7: terminal safety | Feed OSC 52, ANSI clear-screen, split ESC sequences, HTML, giant Unicode/graphemes and long ASCII maps. | No clipboard/title/link side effects, no UI crash or map corruption; safe bounded rendering. |
| A8: Android LAN | Test API 37 target with permission granted, denied and revoked; compare WAN, LAN and any overlay. | Explicit permission handling and accurate failure state; no undocumented connection bypass. |
| A9: phone UX | Physical Android phone: soft/hardware keyboard, IME Send, rotation, background/resume, large fonts and TalkBack. | Documented pass/fail observations and screenshots/logs with secrets redacted. |
| A10: compatibility (later) | Telnet fragmented IAC/CR sequences, unknown options and GMCP handshake/frame fuzz. | No negotiation loop or unbounded frame; do not count as passed unless adapter supports the feature. |

**Release blockers:** no authenticated remote TLS host yet, no Android APK
source/build yet, no physical-phone evidence, and no completed real-time or
neural-NPC protocol. A design review or emulator cannot elevate these claims.
