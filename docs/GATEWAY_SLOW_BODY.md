# Loopback gateway worker and body admission bounds

This source candidate strengthens the local REALM HTTP gateway. It does **not**
qualify public hosting, TLS, account registration, or a reverse proxy.

## Concrete failure prevented

Before this change, an authenticated client could send `POST /api/v1/join`
with a valid bearer and `Content-Length: 4096`, then stop sending bytes.
`GatewayHandler.do_POST` waited inside an unbounded `rfile.read` with no
socket timeout. Each connection retained a `ThreadingHTTPServer` worker.
The authenticated request-rate bucket did not bound these *in-progress*
requests, and unauthenticated clients could consume workers before reaching
the authentication gate.

## New source controls

- **Accepted worker cap:** `GatewayServer` admits at most 32 active
  connections by default, including incomplete headers and keepalive sockets.
  The next accepted socket receives a small best-effort HTTP `503` without
  allocating a worker. Admission uses a nonblocking semaphore; thread
  completion, including exceptions, releases the slot.
- **Socket inactivity timeout:** accepted sockets receive a 15-second
  default idle timeout. Read timeouts are enforced below public handlers and
  persist across HTTP/1.1 keepalive requests.
- **Absolute POST body deadline:** after validated headers and an existing
  player-bearer gate, the server reads up to 4096 declared bytes in
  incremental `read1` calls. The total body budget is 10 seconds by default
  even when a client sends one byte repeatedly. A timed-out body gets
  HTTP `408` if the socket still accepts a response. No partial command is
  submitted to the engine.
- **Bounded length parsing:** a numerically huge `Content-Length` is rejected
  as `413` without converting an unbounded decimal string to an integer.

Host constructors may lower/raise the cap using `max_active_requests` (1–1024),
`socket_idle_timeout_seconds` and `body_deadline_seconds` (finite seconds in
(0,120]). This changes no player-credential, worldpack, command, or public
wire request schema.

## Evidence / residual release hold

Disposable loopback socket tests include two deliberately stalled authenticated
POSTs, overflow `503`, worker reclamation, a trickle-fed incomplete body,
and a 10,000-digit `Content-Length` denial. Source syntax and existing
gateway/event-stream/browser suites must also pass on the final integrated tree.

> This is **not** a complete public DoS defense. Accepted connections are
> capped but a trickle-fed request *header* can still occupy one of those
> bounded worker slots for a long time. There is no total connection lifetime
> budget, per-IP unauthenticated quota, distributed limit coordination,
> production TLS, public admission proxy, or independent load test. A
> deliberate peer can starve a small pool until the upstream deployment layer
> imposes its own bounded header deadlines and connection policies. Keep the
> server on loopback; do not port-forward or publish it.
