# World clock callback atomicity

`WorldClock.tick()` owns one `BEGIN IMMEDIATE` transaction for each selected
batch. The trusted handler, optional `begin_host_tick`, and optional
`verify_final_precommit` receive that connection. A failed callback must not
commit an effect independently of the corresponding `clock_events` receipt.

## Corrected direct-call escape

Before the direct callback guard, a handler could insert a gameplay effect,
call `db.commit()`, and then raise. The tick raised and its event remained
pending, yet the gameplay effect had committed. A retry could repeat that
effect, leaving two committed effects for one acknowledged event. The
`run_foreground` dispatcher already rejected COMMIT/ROLLBACK, but direct callers
of `tick()` had no equivalent check.

The clock now composes a temporary SQLite authorizer around each direct trusted
callback. It denies `SQLITE_TRANSACTION` (including `Connection.commit()`,
`Connection.rollback()`, and `executescript()`'s implicit COMMIT), plus
`SQLITE_ATTACH`/`SQLITE_DETACH`, which could take effects outside the one-world
SQLite custody boundary. A callback cannot hide a denied attempt by catching
the SQLite error. Ordinary nested SQLite `SAVEPOINT`s remain allowed.

Registered foreground callbacks retain their stricter finalizer read-only
contract and its existing diagnostics. Host-installed SQLite authorizers are
composed, not replaced, by the temporary clock guard.

This protection addresses **misconfigured trusted callbacks**. It is not a
sandbox for arbitrary Python code: a trusted callback can deliberately use
the unbound SQLite C API, create another SQLite connection, or perform external
I/O. Network, model, message, and RPC effects are not transactional; use a
durable outbox and an idempotent consumer for those. Callers still must own
actor authorization and event-kind registration.

## Verification

`tests/test_clock_direct_callback_atomicity.py` checks direct COMMIT, ROLLBACK,
implicit `executescript` COMMIT, swallowed denials, ATTACH, begin/finalizer
hooks, event retry, and foreground runner compatibility against real temporary
SQLite world files. The broader clock and REALM suites remain the release
verification gates; passing these checks alone does not install or deploy any
runtime service.
