# Browser behavior under local host overload

REALM's static HTML client can receive HTTP `429` (per-player request
budget), `503` (gateway admission capacity), or `408` (POST body timeout).
This document describes the **source behavior**, not a deployed endpoint or
qualification for public Internet access.

## Feed GETs

- The client keeps an established connection after initial `503`/`408`
  responses and retries only the **idempotent feed GET**.
- Each subsequent transient response increases a bounded pause: 4.4, 8.8,
  then 17.6 seconds with the default 2.2-second poll interval. A valid
  `Retry-After` on `503` can extend the pause up to 30 seconds; an invalid
  value never disables polling indefinitely.
- On a fourth consecutive `503`/`408`, the client disconnects clearly
  instead of keeping a false-looking live session forever. A successful
  feed response resets that temporary-error counter.
- HTTP `429` continues to honor its independently bounded
  `Retry-After` interval. Network errors and other status codes retain
  the existing three-failures disconnect policy.
- An empty or non-JSON error body on a `503` still preserves its HTTP
  status for backoff. A malformed **successful** response is still rejected.

## Commands and identity

- **Never automatically repeat a mutating POST.** A `503`, `408`, or
  interrupted command response leaves the typed command visible and
  re-enables the Send control. The player decides whether to retry.
- Manual retry of **identical** command text reuses its original
  `action_id` to allow the server's durable duplicate-action protection.
  Editing the text starts a new logical command and uses a new ID.
- A definitive validation/auth rejection clears that pending retry.
- Disconnect destroys the in-memory token and pending retry state. Even
  when an old fetch implementation ignores cancellation, a late response
  from that old session cannot update a new transcript or restore controls.
- Bearer credentials still never enter local storage, URLs, or query strings;
  HTTP is rejected for non-loopback hosts.

## Boundaries and evidence

Browser tests in `tests/browser/test-client.cjs` exercise empty-body 503
handling, 408 recovery, timed backoff, bounded disconnect, action-ID reuse
after explicit retries, and late-response isolation. They also execute the
existing Python loopback-gateway smoke fixture. No production browser,
TLS proxy, remote host, persistent login, or network interruption has been
qualified by these fixture tests.

The client does not authorize a retry if the host changed its character
permissions. Even with a stable action ID, the **host's authenticated
transaction** decides whether the command is valid and whether a receipt
can be replayed.
