"""Security regression tests for opt-in, world-scoped bearer credentials."""

from __future__ import annotations

import json
import http.client
import contextlib
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.gateway import Gateway, GatewayServer, main as gateway_main
from realm.scoped_credentials import CredentialStateError, ScopedCredentialStore


class MutableUTC:
    def __init__(self):
        self.value = datetime(2031, 2, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class ScopedCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "world.sqlite3"
        self.anchor = Path(self.tmp.name) / "external-credential-witness.json"
        self.clock = MutableUTC()
        self.gateway = Gateway(self.db_path, world_id="sandbox")
        self.store = ScopedCredentialStore(
            self.gateway.engine, "sandbox", guard_path=self.anchor, clock=self.clock
        )

    def store_again(self):
        return ScopedCredentialStore(
            self.gateway.engine, "sandbox", guard_path=self.anchor, clock=self.clock
        )

    def test_utc_fields_digest_scope_and_no_plaintext(self):
        token = self.store.provision("Patrick", ttl_seconds=300)
        self.assertEqual("Patrick", self.store.authenticate(token))
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT world_id,player_name,token_digest,issued_utc,expires_utc "
                "FROM main.scoped_player_credentials"
            ).fetchone()
        self.assertEqual(("sandbox", "Patrick"), row[:2])
        self.assertNotEqual(token, row[2])
        self.assertNotIn(token, json.dumps(row))
        self.assertEqual("2031-02-01T12:00:00+00:00", row[3])
        self.assertEqual("2031-02-01T12:05:00+00:00", row[4])
        self.assertEqual("Patrick", self.store_again().authenticate(token))

    def test_expiry_forward_jump_and_backward_rollback_fail_closed(self):
        token = self.store.provision("Vera", ttl_seconds=30)
        self.clock.advance(seconds=29)
        self.assertEqual("Vera", self.store.authenticate(token))
        self.clock.advance(seconds=1)
        self.assertIsNone(self.store.authenticate(token))
        self.clock.advance(hours=-1)
        with self.assertRaises(CredentialStateError):
            self.store.authenticate(token)
        with self.assertRaises(CredentialStateError):
            self.store.provision("Patrick")
        self.clock.advance(hours=2)
        self.assertIsNone(self.store.authenticate(token))

    def test_observed_expiry_tombstone_prevents_token_resurrection(self):
        token = self.store.provision("Patrick", ttl_seconds=30)
        self.clock.advance(seconds=30)
        self.assertIsNone(self.store.authenticate(token))
        with closing(sqlite3.connect(self.db_path)) as db:
            revision = db.execute(
                "SELECT revision FROM main.scoped_auth_guard"
            ).fetchone()[0]
            revoked = db.execute(
                "SELECT revoked_us FROM main.scoped_player_credentials"
            ).fetchone()[0]
        self.assertIsNotNone(revoked)
        witness = self.anchor.read_bytes()
        for _ in range(20):
            self.assertIsNone(self.store_again().authenticate(token))
        self.assertEqual(witness, self.anchor.read_bytes())
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(revision, db.execute(
                "SELECT revision FROM main.scoped_auth_guard"
            ).fetchone()[0])
        self.clock.advance(seconds=-10)
        with self.assertRaises(CredentialStateError):
            self.store_again().authenticate(token)

    def test_wrong_world_rejected_without_creating_identity(self):
        token = self.store.provision("Patrick")
        other_db = Path(self.tmp.name) / "second.sqlite3"
        second = Gateway(other_db, world_id="another")
        second_store = ScopedCredentialStore(
            second.engine, "another", guard_path=Path(self.tmp.name) / "second.anchor", clock=self.clock
        )
        self.assertIsNone(second_store.authenticate(token))
        with self.assertRaises(ValueError):
            ScopedCredentialStore(self.gateway.engine, "another", guard_path=self.anchor, clock=self.clock)

    def test_rotation_consumes_old_token_once_with_concurrent_callers(self):
        token = self.store.provision("Patrick")
        def attempt(_):
            try:
                return self.store_again().rotate(token)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = list(filter(None, pool.map(attempt, range(2))))
        self.assertEqual(1, len(winners))
        self.assertIsNone(self.store.authenticate(token))
        self.assertEqual("Patrick", self.store.authenticate(winners[0]))
        self.assertNotEqual(winners[0], token)
        self.assertTrue(self.store.revoke("PATRICK"))
        self.assertIsNone(self.store.authenticate(winners[0]))

    def test_revoke_racing_rotation_cannot_leave_live_token(self):
        token = self.store.provision("Patrick")
        with ThreadPoolExecutor(max_workers=2) as pool:
            rotated = pool.submit(self._try_rotate, token)
            revoked = pool.submit(self.store.revoke, "Patrick")
            next_token = rotated.result()
            revoked.result()
        self.assertIsNone(self.store.authenticate(token))
        if next_token:
            self.assertIsNone(self.store.authenticate(next_token))

    def _try_rotate(self, token):
        try:
            return self.store_again().rotate(token)
        except ValueError:
            return None

    def test_restore_of_database_without_external_anchor_fails_closed(self):
        old = self.store.provision("Patrick")
        backup = Path(self.tmp.name) / "world-backup.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        new = self.store.rotate(old)
        self.assertEqual("Patrick", self.store.authenticate(new))
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(self.db_path)) as target:
            source.backup(target)
        with self.assertRaises(CredentialStateError):
            self.store_again()
        with self.assertRaises(CredentialStateError):
            self.store.authenticate(old)

    def test_missing_or_corrupt_anchor_denies_existing_credential(self):
        token = self.store.provision("Patrick")
        self.anchor.unlink()
        with self.assertRaises(CredentialStateError):
            self.store_again()
        with self.assertRaises(CredentialStateError):
            self.store.authenticate(token)

    def test_invalid_principal_ttl_and_replay_provision(self):
        for principal in ("../admin", "", "☠"):
            with self.assertRaises(ValueError):
                self.store.provision(principal)
        for ttl in (0, 3601, -1, True, 1.2):
            with self.assertRaises(ValueError):
                self.store.provision("Patrick", ttl_seconds=ttl)
        token = self.store.provision("Patrick")
        with self.assertRaises(ValueError):
            self.store.provision("Patrick")
        self.assertTrue(self.store.revoke("Patrick"))
        self.assertIsNone(self.store.authenticate(token))
        with self.assertRaises(ValueError):
            self.store.provision("Patrick")

    def test_read_path_does_not_write_on_valid_or_forged_bearers(self):
        valid = self.store.provision("Patrick")
        before_file = self.anchor.read_bytes()
        with closing(sqlite3.connect(self.db_path)) as db:
            before_row = db.execute(
                "SELECT revision,nonce,max_seen_us FROM main.scoped_auth_guard"
            ).fetchone()
        self.clock.advance(seconds=20)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda index: self.store_again().authenticate(
                    valid if index % 2 == 0 else "x" * 43
                ), range(80),
            ))
        self.assertEqual(40, sum(value == "Patrick" for value in results))
        self.assertEqual(40, sum(value is None for value in results))
        self.assertEqual(before_file, self.anchor.read_bytes())
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(before_row, db.execute(
                "SELECT revision,nonce,max_seen_us FROM main.scoped_auth_guard"
            ).fetchone())

    def test_capability_matrix_and_non_escalating_rotation(self):
        token = self.store.provision("Patrick", capabilities=("feed:read",))
        self.assertEqual("Patrick", self.store.authenticate(token, required_capability="feed:read"))
        for denied in ("world:join", "room:read", "map:read", "command:write", "ship:depart"):
            self.assertIsNone(self.store.authenticate(token, required_capability=denied))
        rotated = self.store.rotate(token)
        self.assertIsNone(self.store.authenticate(token, required_capability="feed:read"))
        self.assertEqual("Patrick", self.store.authenticate(rotated, required_capability="feed:read"))
        self.assertIsNone(self.store.authenticate(rotated, required_capability="command:write"))
        with self.assertRaises(ValueError):
            self.store.provision("Vera", capabilities=("admin",))

    def test_temp_schema_cannot_shadow_world_scope(self):
        token = self.store.provision("Patrick")
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TEMP TABLE world_meta (key TEXT,value TEXT)")
            db.execute("INSERT INTO temp.world_meta VALUES ('world_id','another')")
            db.execute("CREATE TEMP TABLE scoped_player_credentials (player_name TEXT,token_digest TEXT)")
            self.assertEqual("sandbox", db.execute(
                "SELECT value FROM main.world_meta WHERE key='world_id'"
            ).fetchone()[0])
        self.assertEqual("Patrick", self.store.authenticate(token))

    def test_gateway_opt_in_feed_and_commands_and_default_legacy(self):
        original = self.gateway.credentials.provision("Patrick")
        self.assertEqual("Patrick", self.gateway.credentials.authenticate(original))
        opt_gateway = Gateway(
            Path(self.tmp.name) / "gateway.sqlite3", world_id="sandbox", scoped_credentials=True,
            scoped_guard_path=Path(self.tmp.name) / "gateway.anchor",
        )
        scoped = opt_gateway.scoped_credentials.provision("Patrick")
        server = GatewayServer(opt_gateway, (), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        url = f"http://127.0.0.1:{server.server_port}"

        def fetch(path, token, payload=None):
            data = json.dumps(payload).encode() if payload else None
            headers = {"Authorization": "Bearer " + token}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = request.Request(url + path, data=data, headers=headers)
            try:
                with request.urlopen(req, timeout=3) as res:
                    return res.status, json.loads(res.read())
            except error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())

        self.assertEqual(401, fetch("/api/v1/feed?after=0", original)[0])
        self.assertEqual(200, fetch("/api/v1/join", scoped, {"player": "Patrick"})[0])
        self.assertEqual(200, fetch("/api/v1/command", scoped, {
            "player": "Patrick", "command": "say Authenticated", "action_id": "scoped-1"
        })[0])
        status, feed = fetch("/api/v1/feed?after=0", scoped)
        self.assertEqual(200, status)
        self.assertTrue(any("Authenticated" in event["text"] for event in feed["events"]))
        self.assertEqual(403, fetch("/api/v1/feed?player=Vera", scoped)[0])
        opt_gateway.scoped_credentials.revoke("Patrick")
        self.assertEqual(401, fetch("/api/v1/feed?after=0", scoped)[0])
        self.assertEqual("Patrick", self.gateway.credentials.authenticate(original))

    def test_gateway_feed_only_token_does_not_allow_commands(self):
        opt_gateway = Gateway(
            Path(self.tmp.name) / "scope-gateway.sqlite3", world_id="sandbox", scoped_credentials=True,
            scoped_guard_path=Path(self.tmp.name) / "scope-gateway.anchor",
        )
        feed_only = opt_gateway.scoped_credentials.provision(
            "Patrick", capabilities=("feed:read",)
        )
        self.assertEqual("Patrick", opt_gateway.authenticate_remote(
            feed_only, required_capability="feed:read"
        ))
        self.assertIsNone(opt_gateway.authenticate_remote(
            feed_only, required_capability="command:write"
        ))
        self.assertIsNone(opt_gateway.authenticate_remote(
            feed_only, required_capability="ship:depart"
        ))
        server = GatewayServer(opt_gateway, (), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        base = f"http://127.0.0.1:{server.server_port}"
        read = request.Request(base + "/api/v1/feed", headers={
            "Authorization": "Bearer " + feed_only,
        })
        with self.assertRaises(error.HTTPError) as allowed_auth:
            request.urlopen(read, timeout=3)
        self.assertEqual(400, allowed_auth.exception.code)  # valid auth; not joined yet
        allowed_auth.exception.close()
        denied = request.Request(base + "/api/v1/command", data=json.dumps({
            "player": "Patrick", "command": "say Unauthorized"
        }).encode(), headers={
            "Authorization": "Bearer " + feed_only,
            "Content-Type": "application/json",
        })
        with self.assertRaises(error.HTTPError) as denied_auth:
            request.urlopen(denied, timeout=3)
        self.assertEqual(401, denied_auth.exception.code)
        denied_auth.exception.close()

    def test_scoped_cli_refuses_legacy_credential_management(self):
        for operation in ("--provision-player", "--rotate-player", "--revoke-player"):
            with self.subTest(operation=operation), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    gateway_main(["--db", str(self.db_path), "--world-id", "sandbox",
                                  "--scoped-credentials", operation, "Patrick"])
                self.assertEqual(2, raised.exception.code)

    def test_revoked_scoped_token_rechecked_after_delayed_post_body(self):
        gateway = Gateway(
            Path(self.tmp.name) / "race.sqlite3", world_id="sandbox",
            scoped_credentials=True, scoped_guard_path=Path(self.tmp.name) / "race.anchor",
        )
        token = gateway.scoped_credentials.provision("Alice")
        gateway.join("Alice")
        checked = threading.Event()
        original = gateway.scoped_credentials.authenticate

        def watched(token_value, *, required_capability=None):
            result = original(token_value, required_capability=required_capability)
            checked.set()
            return result

        gateway.scoped_credentials.authenticate = watched
        server = GatewayServer(gateway, (), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        body = json.dumps({"player": "Alice", "command": "say Post-revoke race"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        self.addCleanup(conn.close)
        conn.connect()
        conn.putrequest("POST", "/api/v1/command")
        conn.putheader("Authorization", "Bearer " + token)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        self.assertTrue(checked.wait(3), "First authorization not observed")
        self.assertTrue(gateway.scoped_credentials.revoke("Alice"))
        conn.send(body)
        response = conn.getresponse()
        self.assertEqual(401, response.status)
        response.read()
        with closing(sqlite3.connect(gateway.engine.db_path)) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM main.events WHERE actor='Alice' AND kind='say'"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
