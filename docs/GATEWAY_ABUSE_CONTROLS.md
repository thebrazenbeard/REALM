# Gateway authenticated-request rate budget

REALM's local HTTP `GatewayServer` admits authenticated requests through a
**process-local, per-player token bucket**. The same gate is inherited by
`FeedWaitHandler`; one long-poll HTTP request spends one admission token, not
one for each internal poll. This does not publish or start a server.

## Contract

- Defaults: **120 requests/minute sustained** per authenticated player,
  **60-request burst**, at most **256 tracked player buckets**. Normal browser
  polling (~2.2 seconds) and short command/map/room bursts fit this budget.
- The key is the canonical, authenticated player name, case-folded. It is
  **not** the presented token, caller-asserted player, socket IP, Origin,
  or untrusted `X-Forwarded-For`. Rotation cannot reset the quota.
- A full principal table expires idle buckets after at least the time needed
  to refill a burst (minimum 60 seconds); otherwise valid newly seen players
  get 429, without evicting another active player's bucket.
- HTTP 429 has generic JSON, an integer `Retry-After` header, and closes the
  socket. Invalid bearer 401 and forbidden Origin 403 retain priority. An
  already exhausted principal can receive 429 before endpoint-level actor
  mismatch 403 checks run. Public `/api/v1/health` and OPTIONS are exempt.
- All authenticated GET and POST endpoints—including ship routes and
  `/api/v1/feed/wait`—share the same player bucket. Rejected requests never
  reach command dispatch or another gameplay action. No SQLite transaction
  remains open during rate-limiter admission.
- A host can configure `rate_limit_requests_per_minute`, `rate_limit_burst`,
  and `rate_limit_max_principals` with finite positive integer limits. Defaults
  apply to the CLI and derived `FeedWaitServer` unless a trusted host overrides.

## Deliberately unqualified protections

> These controls do not authorize public networking. A companion gateway
> worker cap and POST-body deadline now bound admitted worker count and body
> wait time ([source contract](GATEWAY_SLOW_BODY.md)). A client can still
> occupy one of those bounded slots with slow request headers, or flood
> unauthenticated requests. This limiter is not an IP firewall, global CPU
> cap, durable quota, or cross-process rate limit.

- Invalid bearer requests still cause authentication SQLite reads.
- Each server process has its own bucket state; restart resets the budget.
- A token bucket permits the configured burst immediately. Other simultaneous
  requests may use replenished tokens before a waiting client retries.
- HTTP credential revocation cannot retract bytes already authorized and
  in-flight between the final check and socket write. Source tests that pause
  before the final check cannot establish atomic revocation.
- TLS/private authenticated transport, total header deadlines, distributed
  quotas, remote penetration testing and an actual live remote host remain
  outstanding. In-flight requests are bounded per process but not globally.

Evidence: `python -m unittest discover -s tests -p test_gateway_rate_limit.py -q`
drives real local HTTP requests, concurrent quota admission, credential
rotation, GET/POST effects, and long-poll inheritance. It is not a public
denial-of-service qualification.
