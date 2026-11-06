# REALM Terminal — Android source prototype

**Status: source scaffold, not a built or installed APK.** This is a separate
client lane for the self-hostable REALM engine. No server, worldpack, NPC model,
token, private fiction, or world authority is embedded in the app.

## Implemented source boundaries

- Native Kotlin/Compose terminal surface with host profiles, scrollback,
  adjustable monospaced text, TalkBack-friendly status, masked token input,
  command entry and keyboard Send.
- **HTTPS by default.** An explicitly selected host/world/player triple uses
  the current provisional `/api/v1/health`, `/join`, `/command` endpoints over
  `HttpsURLConnection`. Platform certificate/hostname checks remain enabled;
  HTTP redirects and plaintext fallback are refused. A player bearer token
  must be entered per session and is never saved to profile preferences.
- **Raw Telnet only for loopback compatibility testing**, with manual plaintext
  warning and acknowledgement. Valid hosts are `localhost`, `127.0.0.1`, or
  `::1`. `10.0.2.2` is accepted only for an explicit debug-emulator session,
  not in saved profiles. No remote plain-Telnet profile can be created.
- Incremental Telnet IAC refusal for WILL/WONT/DO/DONT and bounded SB frames;
  incremental UTF-8 decoding across packet fragments.
- A safe ANSI visual subset: reset/bold/16-color SGR; OSC, OSC 52 clipboard,
  DCS, terminal title changes, cursor motion and clear-screen operations are
  stripped as inert data. No embedded web view or remote code execution.
- Endpoint profiles persist **only** display label, host, port, world ID and
  transport mode. Android backups are disabled. Terminal content is in-memory,
  bounded and not persisted; local Telnet input is never echoed to transcript.

## $0 local build

1. Install the free Android Studio, Android SDK Platform 36 and JDK 17 on an
   authorized development machine. Open the `clients/android` folder.
2. Allow Gradle to resolve the documented public Android/Compose/Kotlin
   artifacts; the project does not bundle a Gradle wrapper JAR or binaries.
3. Run `:app:testDebugUnitTest` and `:app:assembleDebug` in Android Studio (or
   `gradle :app:testDebugUnitTest :app:assembleDebug` if a suitable Gradle
   executable is already installed).
4. Inspect the local result under `app/build/outputs/apk/debug/`; record its
   SHA-256 and source Git head before any physical-device testing.

This environment did **not** have an Android SDK, Gradle or Kotlin compiler,
so no APK or Android unit-test pass is claimed by the source-only commit.
The pure-Kotlin JUnit tests are written but require the actual Android toolchain
to execute.

## Before connecting Patrick's phone

The current Python REALM HTTP and Telnet adapters bind to **Lappy's
`127.0.0.1`**, which is not the phone's loopback. There is currently no
authorized, independently qualified HTTPS edge for a physical Android phone.
Do not port-forward or turn on a public listener to make this app connect.
An owner-authorized future step must provide a TLS-authenticated gateway,
player-scoped permission tests, and physical-phone install/test approval.

The current player-token scheme is long-lived and host-provisioned, not a
general account signup, expiring-session, or recovery system. The UI's player
field is only an assertion; the server must derive actual action authority
from its bearer token. No model-generated character statement is authoritative.

## References

- [Android's native Compose setup](https://developer.android.com/develop/ui/compose/setup-compose-dependencies-and-compiler)
- [Android network security configuration](https://developer.android.com/privacy-and-security/security-config)
- [RFC 854 — Telnet NVT](https://www.rfc-editor.org/rfc/rfc854.html)
- [REALM Android acceptance gates](../../docs/ANDROID_CLIENT.md)
