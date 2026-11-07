import copy
import gc
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from realm.engine import Engine
from realm.worldpack import import_worldpack
from realm.world_expansion import ExpansionError, ExpansionHeld, inspect_expansion, apply_expansion, _apply_synthetic_transaction


def seed_world(root):
    db = root / "realm.sqlite3"
    pack = {
        "schema": "realm.worldpack.v1", "world_id": "sample", "name": "Sandbox",
        "start_room": "shore",
        "rooms": [
            {"id": "shore", "name": "Shore", "description": "A beach.", "exits": {"east": "grove"}},
            {"id": "grove", "name": "Grove", "description": "Old trees.", "exits": {"west": "shore"}},
        ],
        "items": [{"id": "stone_1", "name": "Stone", "description": "A stone.", "room": "shore"}],
    }
    source = root / "sample.json"
    source.write_text(json.dumps(pack), encoding="utf-8")
    import_worldpack(db, source)
    engine = Engine(db)
    engine.join("Alice")
    engine.join("Bob")
    engine.act("Alice", "take stone_1", action_id="a1")
    engine.act("Bob", "go east", action_id="b1")
    with sqlite3.connect(db) as conn:
        bind = conn.execute("SELECT value FROM world_meta WHERE key='worldpack_sha256'").fetchone()[0]
    proposal = {
        "schema": "realm.world-expansion.v1", "world_id": "sample",
        "base_worldpack_sha256": bind, "expected_revision": 0, "expansion_id": "new_cave",
        "provenance": {"source_id": "synthetic-only", "source_sha256": "a" * 64},
        "rooms": [{"id": "cave", "name": "Cave", "description": "A new cave."}],
        "exits": [
            {"room_id": "shore", "direction": "north", "destination": "cave"},
            {"room_id": "cave", "direction": "south", "destination": "shore"},
        ],
        "items": [{"id": "crystal_1", "name": "Crystal", "description": "A crystal.", "room": "cave"}],
    }
    return db, engine, proposal


def snapshot(db):
    with sqlite3.connect(db) as conn:
        return {
            name: conn.execute("SELECT * FROM main." + name + " ORDER BY 1").fetchall()
            for name in ("players", "events", "action_receipts", "rooms", "exits", "items")
        }


def synthetic_owner_authorizer(review):
    # Only a test witness; never an owner signature or a release credential.
    return review["world_id"] == "sample" and review["provenance"]["source_id"] == "synthetic-only"


class LiveWorldExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(lambda: (gc.collect(), self.temp.cleanup()))
        self.db, self.engine, self.proposal = seed_world(Path(self.temp.name))

    def test_preview_is_read_only_and_default_apply_held(self):
        before = snapshot(self.db)
        preview = inspect_expansion(self.engine, self.proposal)
        self.assertEqual(preview["additions"]["rooms"], ["cave"])
        self.assertTrue(preview["held"])
        self.assertEqual(snapshot(self.db), before)
        with self.assertRaises(ExpansionHeld):
            apply_expansion(self.engine, self.proposal)
        self.assertEqual(snapshot(self.db), before)

    def test_atomic_additive_apply_preserves_two_clients_history_and_replay(self):
        before = snapshot(self.db)
        review = _apply_synthetic_transaction(self.engine, self.proposal, owner_authorizer=synthetic_owner_authorizer)
        self.assertEqual(review["revision"], 1)
        after = snapshot(self.db)
        for table in ("players", "events", "action_receipts"):
            self.assertEqual(after[table], before[table])
        self.assertEqual(len(after["rooms"]), len(before["rooms"]) + 1)
        self.assertEqual(len(after["exits"]), len(before["exits"]) + 2)
        self.assertEqual(len(after["items"]), len(before["items"]) + 1)
        self.assertEqual(_apply_synthetic_transaction(self.engine, self.proposal, owner_authorizer=synthetic_owner_authorizer)["revision"], 1)
        self.assertEqual(snapshot(self.db), after)
        with sqlite3.connect(self.db) as conn:
            rec = conn.execute("SELECT source_id,source_sha256,proposal_sha256 FROM main.world_expansion_ledger").fetchone()
        self.assertEqual(rec[:2], ("synthetic-only", "a" * 64))
        self.assertEqual(len(rec[2]), 64)

    def test_refuses_rewrites_dangling_crossworld_revision_and_changed_replay(self):
        cases = [
            {"rooms": [{"id": "shore", "name": "Evil", "description": "Overwrite"}]},
            {"exits": [{"room_id": "shore", "direction": "east", "destination": "cave"}]},
            {"exits": [{"room_id": "shore", "direction": "north", "destination": "missing"}]},
            {"world_id": "other"},
            {"base_worldpack_sha256": "0" * 64},
            {"expected_revision": 9},
            {"rooms": [] , "exits": [], "items": []},
            {"start_room": "cave"},
        ]
        before = snapshot(self.db)
        for changes in cases:
            p = copy.deepcopy(self.proposal)
            p.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ExpansionError):
                inspect_expansion(self.engine, p)
        self.assertEqual(snapshot(self.db), before)
        _apply_synthetic_transaction(self.engine, self.proposal, owner_authorizer=synthetic_owner_authorizer)
        tampered = copy.deepcopy(self.proposal)
        tampered["rooms"][0]["description"] = "Different history"
        with self.assertRaises(ExpansionError):
            _apply_synthetic_transaction(self.engine, tampered, owner_authorizer=synthetic_owner_authorizer)

    def test_concurrent_revision_cas_and_rollback_on_db_trigger(self):
        a = copy.deepcopy(self.proposal)
        _apply_synthetic_transaction(self.engine, a, owner_authorizer=synthetic_owner_authorizer)
        b = copy.deepcopy(a)
        b["expansion_id"] = "second"
        b["rooms"] = [{"id": "second", "name": "Second", "description": "Another room."}]
        b["exits"] = []
        b["items"] = []
        with self.assertRaises(ExpansionError):
            _apply_synthetic_transaction(self.engine, b, owner_authorizer=synthetic_owner_authorizer)
        b["expected_revision"] = 1
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER main.fail_second BEFORE INSERT ON main.rooms WHEN new.id='second' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        before = snapshot(self.db)
        with self.assertRaises(sqlite3.DatabaseError):
            _apply_synthetic_transaction(self.engine, b, owner_authorizer=synthetic_owner_authorizer)
        self.assertEqual(snapshot(self.db), before)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM main.world_expansion_ledger").fetchone()[0], 1)
            conn.execute("DROP TRIGGER main.fail_second")
        result = _apply_synthetic_transaction(self.engine, b, owner_authorizer=synthetic_owner_authorizer)
        self.assertEqual(result["revision"], 2)

    def test_rejects_connection_temp_schema_spoof(self):
        normal_connect = self.engine._connect
        def poisoned():
            conn = normal_connect()
            conn.execute("CREATE TEMP TABLE world_meta (key TEXT,value TEXT)")
            return conn
        self.engine._connect = poisoned
        with self.assertRaises(ExpansionError):
            inspect_expansion(self.engine, self.proposal)

    def test_true_concurrent_different_expansions_exactly_one_cas_winner(self):
        second = copy.deepcopy(self.proposal)
        second["expansion_id"] = "other_cave"
        second["rooms"] = [{"id": "other", "name": "Other", "description": "Second candidate."}]
        second["exits"] = []
        second["items"] = []
        barrier = threading.Barrier(2)
        results = []
        def compete(candidate):
            barrier.wait()
            try:
                receipt = _apply_synthetic_transaction(self.engine, candidate, owner_authorizer=synthetic_owner_authorizer)
                results.append(("won", receipt["revision"]))
            except ExpansionError as exc:
                results.append(("lost", str(exc)))
        a = threading.Thread(target=compete, args=(self.proposal,))
        b = threading.Thread(target=compete, args=(second,))
        a.start()
        b.start()
        a.join(5)
        b.join(5)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(sorted(kind for kind, _ in results), ["lost", "won"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM main.world_expansion_ledger").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT value FROM main.world_meta WHERE key='expansion_revision'").fetchone()[0], "1")

    def test_authorizer_cannot_mutate_payload_after_digest(self):
        original = copy.deepcopy(self.proposal)
        def mutating_authorizer(review):
            self.proposal["rooms"][0]["name"] = "Tampered"
            return True
        receipt = _apply_synthetic_transaction(self.engine, self.proposal, owner_authorizer=mutating_authorizer)
        self.assertEqual(receipt["base_worldpack_sha256"], original["base_worldpack_sha256"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT name FROM main.rooms WHERE id='cave'").fetchone()[0], "Cave")
        self.assertEqual(self.proposal["rooms"][0]["name"], "Tampered")

    def test_internal_fixture_refuses_hosted_path_even_with_permissive_callback(self):
        from types import SimpleNamespace
        fake_host = SimpleNamespace(
            db_path=Path("D:/VERA/hosted-world.sqlite3"),
            _connect=self.engine._connect,
        )
        with self.assertRaises(ExpansionHeld):
            _apply_synthetic_transaction(fake_host, self.proposal, owner_authorizer=lambda _: True)

    def test_rejects_duplicate_and_source_claims_cannot_bypass_default_hold(self):
        extras = [
            {"rooms": self.proposal["rooms"] * 2},
            {"items": self.proposal["items"] * 2},
            {"exits": self.proposal["exits"] * 2},
            {"items": [{"id": "stone_1", "name": "Overwrite", "description": "No", "room": "cave"}]},
            {"items": [{"id": "alien", "name": "Lost", "description": "No", "room": "nowhere"}]},
            {"provenance": {"source_id": "synthetic-only", "source_sha256": "z" * 64}},
        ]
        for change in extras:
            p = copy.deepcopy(self.proposal)
            p.update(change)
            with self.subTest(change=change), self.assertRaises(ExpansionError):
                inspect_expansion(self.engine, p)
        before = snapshot(self.db)
        with self.assertRaises(ExpansionHeld):
            apply_expansion(self.engine, self.proposal, owner_authorizer=lambda review: False)
        with self.assertRaises(ExpansionHeld):
            apply_expansion(self.engine, self.proposal, owner_authorizer=lambda review: True)
        self.assertEqual(snapshot(self.db), before)
        self.assertEqual(inspect_expansion(self.engine, self.proposal)["revision"], 1)


if __name__ == "__main__":
    unittest.main()
