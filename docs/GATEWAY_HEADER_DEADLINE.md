# Absolute request-header deadline (loopback gateway)

The local HTTP `GatewayServer` already has a 32-worker default cap,
15-second socket inactivity timeout, and 10-second absolute POST-body
deadline. Inactivity alone is insufficient during initial request parsing:
an HTTP peer can send one byte before each socket timeout and retain one
bounded worker indefinitely. A patient peer can occupy all worker slots.

This source patch adds `header_deadline_seconds`, a finite, positive
host setting bounded to 120 seconds (default **10 seconds**). It measures
elapsed time from the **first byte of each request** until the complete
request line and headers have passed Python's built-in HTTP parser.

- A watchdog interrupts incomplete request/header reads after the absolute
  deadline; the handler closes rather than dispatching a partially parsed
  request. An interrupted connection might receive no HTTP response. This is
  an availability boundary, not a guarantee of status 408 delivery.
- The watchdog is cancelled before a successfully parsed request body or
  game command executes. The separately bounded 10-second POST-body deadline
  continues to own that phase.
- The first-byte wait remains governed by the socket inactivity timeout,
  so an idle HTTP/1.1 keep-alive connection does **not** consume its next
  request's header budget. Request-pipelined buffered data remains readable.
- Each admitted worker owns at most one temporary watchdog timer; the existing
  active-worker semaphore also bounds simultaneous watchdogs. Timer races are
  resolved using a lock and a terminal expired flag checked before dispatch.
- Defaults retain the existing loopback-only bind and authentication paths.
  No public port, account service, credential issuance, or network proxy is
  installed by this patch.

## Evidence and residual limitations

`tests/test_gateway_header_deadline.py` drives real loopback sockets with
partial request lines, partial headers, byte trickles below inactivity
timeout, and worker-pool overflow/recovery. It also ensures delayed POST
bodies and keep-alive reuse remain valid and that truncated headers cannot
execute a request after the deadline.

> This is not a complete public-service defense. Idle connections can still
> occupy a bounded slot until the socket inactivity timeout. A proxy/load
> balancer, TLS, admission quotas, per-IP controls, protocol hardening,
> connection lifetime bounds, and remote load qualification remain separate.
> Leave the server bound to loopback; do not port-forward it.
