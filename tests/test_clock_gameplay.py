"""Clock-driven real Engine and Crafting actions share one SQLite transaction."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway, GatewayServer
from realm.engine import Engine
from realm.crafting import Crafting
from realm.clock_dispatch import schedule_in_transaction
from realm.world_clock import WorldClock

BASE = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)


class ClockGameplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "first.sqlite3"
        Gateway(self.path, world_id="world_first")
        self.engine = Engine(self.path)
        self.engine.join("BotA")
        self.crafting = Crafting(self.path)
        self.crafting.place_node("atrium", "iron", stock=6, batch_yield=3)
        self.crafting.register_recipe("blade", kind="craft", ingredients={"iron": 3})
        self.clock = WorldClock(self.path)
        self.clock.enable()

    def _connection(self, path=None):
        db = sqlite3.connect(str(path or self.path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _apply(self, db, actor, command, action_id):
        try:
            from realm.clock_gameplay import perform_in_transaction
        except ModuleNotFoundError:
            self.fail("Clock gameplay lacks the shared-transaction action seam")
        return perform_in_transaction(
            db, engine=self.engine, crafting=self.crafting,
            actor=actor, command=command, action_id=action_id,
        )

    def test_gather_and_craft_from_real_clock_tick_with_atomic_followup(self):
        self.clock.schedule("gather_1", due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "gather iron"})
        responses = []

        def handler(db, event):
            responses.append(self._apply(db, event.payload["actor"],
                                         event.payload["command"], event.event_id))
            if event.event_id == "gather_1":
                schedule_in_transaction(self.clock, db, "craft_1",
                                        due_at=BASE + timedelta(seconds=1),
                                        kind="host_action",
                                        payload={"actor": "BotA", "command": "craft blade"})

        self.assertEqual(["gather_1"],
                         [x.event_id for x in self.clock.tick(BASE, handler)])
        self.assertEqual("You gather iron x3.", responses[0])
        self.assertEqual(["craft_1"], [x.event_id for x in
                         self.clock.tick(BASE + timedelta(seconds=1), handler)])
        self.assertEqual("You craft blade x1.", responses[1])
        self.assertEqual([], self.clock.tick(BASE + timedelta(seconds=2), handler))
        with closing(self._connection()) as db:
            remaining = db.execute(
                "SELECT remaining FROM gathering_nodes WHERE room_id='atrium' AND material='iron'"
            ).fetchone()[0]
            pouch = db.execute(
                "SELECT quantity FROM material_pouches WHERE actor='BotA' AND material='iron'"
            ).fetchone()[0]
            good = db.execute(
                "SELECT quantity FROM crafted_goods WHERE actor='BotA' AND good='blade'"
            ).fetchone()[0]
            receipts = db.execute(
                "SELECT action_id FROM action_receipts WHERE actor='@clock:BotA' "
                "ORDER BY action_id"
            ).fetchall()
        self.assertEqual((3, 0, 1), (remaining, pouch, good))
        self.assertEqual(["craft_1", "gather_1"], [x[0] for x in receipts])

    def test_engine_movement_and_forced_failure_roll_back_both_ledgers(self):
        self.clock.schedule("move_1", due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "north"})

        def fails(db, event):
            self.assertIn("The Corridor", self._apply(
                db, "BotA", "north", event.event_id))
            self.assertEqual("corridor", db.execute(
                "SELECT room_id FROM players WHERE id='BotA'").fetchone()[0])
            raise RuntimeError("synthetic abort")

        with self.assertRaisesRegex(RuntimeError, "synthetic abort"):
            self.clock.tick(BASE, fails)
        with closing(self._connection()) as db:
            self.assertEqual("atrium", db.execute(
                "SELECT room_id FROM players WHERE id='BotA'").fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM action_receipts WHERE actor='BotA'").fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM events WHERE kind='move'").fetchone()[0])
        self.assertIsNone(self.clock.lookup("move_1").processed_at)
        self.assertEqual(["move_1"], [x.event_id for x in self.clock.tick(
            BASE, lambda db, event: self._apply(
                db, "BotA", "north", event.event_id))])
        with closing(self._connection()) as db:
            self.assertEqual("corridor", db.execute(
                "SELECT room_id FROM players WHERE id='BotA'").fetchone()[0])

    def test_existing_action_receipt_replay_and_conflicting_text(self):
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            a = self._apply(db, "BotA", "gather iron", "same")
            self.assertEqual(a, self._apply(db, "BotA", "gather iron", "same"))
            with self.assertRaisesRegex(ValueError, "different command"):
                self._apply(db, "BotA", "gather iron 2", "same")
            db.commit()
        with closing(self._connection()) as db:
            self.assertEqual(3, db.execute(
                "SELECT remaining FROM gathering_nodes WHERE material='iron'"
            ).fetchone()[0])
            self.assertEqual(1, db.execute(
                "SELECT count(*) FROM events WHERE kind='gather'"
            ).fetchone()[0])

    def test_player_preclaimed_receipt_cannot_acknowledge_unprocessed_clock_action(self):
        """A player-controlled action ID must not counterfeit clock execution."""
        event_id = "preclaimed_scheduled_event"
        command = "say scheduled " + event_id
        self.clock.schedule(event_id, due_at=BASE, kind="trusted_game_action",
                            payload={"actor": "BotA", "command": command})
        self.assertEqual(
            "You say: scheduled " + event_id,
            self.engine.act("BotA", command, action_id=event_id),
        )
        self.assertEqual([event_id], [event.event_id for event in self.clock.tick(
            BASE, lambda db, event: self._apply(
                db, event.payload["actor"], event.payload["command"], event.event_id,
            ))])
        self.assertIsNotNone(self.clock.lookup(event_id).processed_at)
        with closing(self._connection()) as db:
            self.assertEqual(2, db.execute(
                "SELECT COUNT(*) FROM events WHERE actor='BotA' AND kind='say' "
                "AND payload_json LIKE ?", ('%' + event_id + '%',),
            ).fetchone()[0])
            self.assertEqual([("@clock:BotA", event_id), ("BotA", event_id)],
                             [tuple(row) for row in db.execute(
                                 "SELECT actor,action_id FROM action_receipts ORDER BY actor")])

    def test_scheduling_after_player_receipt_cannot_preclaim_clock_action(self):
        """An event created after a user receipt is just as vulnerable."""
        event_id = "preclaimed_later_event"
        command = "say scheduled " + event_id
        self.assertEqual(
            "You say: scheduled " + event_id,
            self.engine.act("BotA", command, action_id=event_id),
        )
        self.clock.schedule(event_id, due_at=BASE, kind="trusted_game_action",
                            payload={"actor": "BotA", "command": command})
        self.assertEqual([event_id], [event.event_id for event in self.clock.tick(
            BASE, lambda db, event: self._apply(
                db, event.payload["actor"], event.payload["command"], event.event_id,
            ))])
        self.assertIsNotNone(self.clock.lookup(event_id).processed_at)
        with closing(self._connection()) as db:
            self.assertEqual(2, db.execute(
                "SELECT COUNT(*) FROM events WHERE actor='BotA' AND kind='say' "
                "AND payload_json LIKE ?", ('%' + event_id + '%',),
            ).fetchone()[0])

    def test_authenticated_http_player_can_preclaim_an_action_id_but_not_clock_effect(self):
        """The hostile actor needs only a normal bearer and command POST."""
        event_id = "authenticated_preclaim"
        command = "say clock-bound text"
        self.clock.schedule(event_id, due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": command})
        gateway = Gateway(self.path, world_id="world_first")
        token = gateway.credentials.provision("BotA")
        server = GatewayServer(gateway, ("http://127.0.0.1:8000",), port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            body = json.dumps({"player": "BotA", "command": command,
                               "action_id": event_id}).encode()
            req = request.Request(
                f"http://127.0.0.1:{server.server_port}/api/v1/command",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + token},
                method="POST",
            )
            with request.urlopen(req, timeout=3) as response:
                self.assertEqual(200, response.status)
                self.assertEqual("You say: clock-bound text",
                                 json.load(response)["text"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual([event_id], [event.event_id for event in self.clock.tick(
            BASE, lambda db, event: self._apply(
                db, event.payload["actor"], event.payload["command"], event.event_id,
            ))])
        self.assertIsNotNone(self.clock.lookup(event_id).processed_at)
        with closing(self._connection()) as db:
            self.assertEqual(2, db.execute(
                "SELECT COUNT(*) FROM events WHERE actor='BotA' AND kind='say'"
            ).fetchone()[0])

    def test_preclaimed_gather_cannot_skip_a_second_scheduled_economic_effect(self):
        """Receipt preclaim must not ACK a gather without due-time resource debit."""
        event_id = "economic_preclaim"
        command = "gather iron"
        self.clock.schedule(event_id, due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": command})
        self.assertEqual("You gather iron x3.", self.crafting.perform(
            "BotA", command, action_id=event_id))
        self.assertEqual([event_id], [event.event_id for event in self.clock.tick(
            BASE, lambda db, event: self._apply(
                db, event.payload["actor"], event.payload["command"], event.event_id,
            ))])
        self.assertIsNotNone(self.clock.lookup(event_id).processed_at)
        with closing(self._connection()) as db:
            self.assertEqual(0, db.execute(
                "SELECT remaining FROM gathering_nodes WHERE room_id='atrium' "
                "AND material='iron'"
            ).fetchone()[0])
            self.assertEqual(6, db.execute(
                "SELECT quantity FROM material_pouches WHERE actor='BotA' "
                "AND material='iron'"
            ).fetchone()[0])
            self.assertEqual(2, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='gather'"
            ).fetchone()[0])

    def test_colliding_player_receipt_does_not_block_other_due_clock_events(self):
        """One user-chosen idempotency key must not stop an unrelated UTC tick."""
        self.clock.schedule("a_preclaim", due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "say first"})
        self.clock.schedule("z_healthy", due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "say second"})
        self.assertEqual("You say: first", self.engine.act(
            "BotA", "say first", action_id="a_preclaim"))
        self.assertEqual(["a_preclaim", "z_healthy"], [event.event_id
            for event in self.clock.tick(BASE, lambda db, event: self._apply(
                db, event.payload["actor"], event.payload["command"], event.event_id))])
        self.assertIsNotNone(self.clock.lookup("a_preclaim").processed_at)
        self.assertIsNotNone(self.clock.lookup("z_healthy").processed_at)
        with closing(self._connection()) as db:
            self.assertEqual(3, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'"
            ).fetchone()[0])

    def test_full_length_event_id_uses_private_actor_namespace_and_host_replays_safely(self):
        """Host receipt identity does not grow past the 128-character event ID limit."""
        event_id = "L" + "q" * 127
        self.clock.schedule(event_id, due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "say long id"})
        self.assertEqual([event_id], [event.event_id for event in self.clock.tick(
            BASE, lambda db, event: self._apply(
                db, event.payload["actor"], event.payload["command"], event.event_id))])
        self.assertIsNotNone(self.clock.lookup(event_id).processed_at)
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual("You say: long id",
                             self._apply(db, "BotA", "say long id", event_id))
            db.commit()
            self.assertEqual(("@clock:BotA", event_id), tuple(db.execute(
                "SELECT actor,action_id FROM action_receipts WHERE action_id=?",
                (event_id,),
            ).fetchone()))
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'"
            ).fetchone()[0])

    def test_player_names_cannot_enter_the_clock_receipt_actor_namespace(self):
        """No authenticated player name can select the host's receipt keyspace."""
        for forbidden in ("@clock:BotA", "@roster:BotA"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValueError):
                    self.engine.join(forbidden)
                with self.assertRaises(ValueError):
                    self.engine.act(forbidden, "say forged", action_id="forge")
                with self.assertRaises(ValueError):
                    self.crafting.perform(forbidden, "gather iron", action_id="forge")
        with closing(self._connection()) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM action_receipts WHERE actor LIKE '@%'"
            ).fetchone()[0])

    def test_legacy_processed_event_without_host_receipt_cannot_repeat_effect(self):
        """A post-upgrade direct retry must not duplicate a legacy ACKed action."""
        event_id = "legacy_clock_receipt"
        self.assertEqual("You say: legacy", self.engine.act(
            "BotA", "say legacy", action_id=event_id))
        self.clock.schedule(event_id, due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "say legacy"})
        # Model a pre-upgrade durable ACK plus the historical player-keyed
        # receipt, without running or blessing an actual legacy callback.
        with closing(self._connection()) as db, db:
            db.execute("UPDATE clock_events SET processed_utc=? WHERE event_id=?",
                       (BASE.isoformat(timespec="microseconds"), event_id))
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "Processed clock event"):
                self._apply(db, "BotA", "say legacy", event_id)
            db.rollback()
        with closing(self._connection()) as db:
            self.assertEqual(1, db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='say'"
            ).fetchone()[0])

    def test_fail_closed_for_foreign_world_or_missing_transaction(self):
        other = Path(self.path.parent) / "other.sqlite3"
        Gateway(other, world_id="world_second")
        with closing(self._connection()) as db:
            with self.assertRaisesRegex(RuntimeError, "transaction"):
                self._apply(db, "BotA", "north", "without_tx")
        with closing(self._connection(other)) as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "different world"):
                self._apply(db, "BotA", "north", "wrong_world")
            db.rollback()

    def test_fail_closed_for_unsupported_actions_and_connection_configuration(self):
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            for command in ("quit", "resources", "help", "erase world", "gather"):
                if command == "gather":
                    continue  # gather without argument is a valid read-only query.
                with self.subTest(command=command):
                    with self.assertRaisesRegex(ValueError, "Unsupported"):
                        self._apply(db, "BotA", command, command.replace(" ", "_"))
            with self.assertRaisesRegex(ValueError, "action_id"):
                self._apply(db, "BotA", "north", "")
            db.rollback()
        with closing(self._connection()) as db:
            db.row_factory = None
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "row_factory"):
                self._apply(db, "BotA", "north", "bad_row")
            db.rollback()
        with closing(sqlite3.connect(str(self.path))) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "foreign keys"):
                self._apply(db, "BotA", "north", "bad_fk")
            db.rollback()

    def test_failed_receipt_trigger_is_rolled_back_even_if_caught(self):
        self.clock.schedule("trigger", due_at=BASE, kind="host_action", payload={})
        with closing(self._connection()) as db, db:
            db.execute("""CREATE TRIGGER fail_action_receipt BEFORE INSERT ON action_receipts
                BEGIN
                    UPDATE gathering_nodes SET remaining=0 WHERE material='iron';
                    SELECT RAISE(FAIL, 'blocked receipt');
                END""")

        def handler(db, event):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "blocked receipt"):
                self._apply(db, "BotA", "gather iron", event.event_id)

        self.assertEqual(["trigger"], [x.event_id for x in
                         self.clock.tick(BASE, handler)])
        with closing(self._connection()) as db:
            self.assertEqual(6, db.execute(
                "SELECT remaining FROM gathering_nodes WHERE material='iron'"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM action_receipts WHERE actor='BotA'"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM events WHERE kind='gather'"
            ).fetchone()[0])

    def test_temp_receipt_shadow_cannot_forge_processed_clock_action(self):
        self.clock.schedule("npc_action", due_at=BASE, kind="host_action",
                            payload={"actor": "BotA", "command": "north"})

        def poisoned(db, event):
            db.execute("CREATE TEMP TABLE action_receipts(actor TEXT, action_id TEXT, command TEXT, response TEXT)")
            db.execute("INSERT INTO temp.action_receipts VALUES ('BotA','npc_action','north','FAKE_REPLAY')")
            self._apply(db, "BotA", "north", event.event_id)

        with self.assertRaisesRegex(RuntimeError, "TEMP"):
            self.clock.tick(BASE, poisoned)
        self.assertIsNone(self.clock.lookup("npc_action").processed_at)
        with closing(self._connection()) as db:
            self.assertEqual("atrium", db.execute(
                "SELECT room_id FROM main.players WHERE id='BotA'").fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM main.action_receipts WHERE actor='BotA'"
            ).fetchone()[0])

    def test_temp_actor_shadow_cannot_inject_ghost_player_and_receipt(self):
        self.clock.schedule("ghost_action", due_at=BASE, kind="host_action", payload={})

        def poisoned(db, event):
            db.execute("CREATE TEMP TABLE players(id TEXT, room_id TEXT)")
            db.execute("INSERT INTO temp.players VALUES ('Ghost','atrium')")
            self._apply(db, "Ghost", "north", event.event_id)

        with self.assertRaisesRegex(RuntimeError, "TEMP"):
            self.clock.tick(BASE, poisoned)
        self.assertIsNone(self.clock.lookup("ghost_action").processed_at)
        with closing(self._connection()) as db:
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM main.action_receipts WHERE actor='Ghost'"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM main.events WHERE actor='Ghost'"
            ).fetchone()[0])

    def test_temp_stock_shadow_cannot_grant_resources_without_decrement(self):
        self.clock.schedule("stock_action", due_at=BASE, kind="host_action", payload={})

        def poisoned(db, event):
            db.execute("CREATE TEMP TABLE gathering_nodes(room_id TEXT, material TEXT, remaining INT, batch_yield INT)")
            db.execute("INSERT INTO temp.gathering_nodes VALUES ('atrium','iron',9999,100)")
            self._apply(db, "BotA", "gather iron", event.event_id)

        with self.assertRaisesRegex(RuntimeError, "TEMP"):
            self.clock.tick(BASE, poisoned)
        self.assertIsNone(self.clock.lookup("stock_action").processed_at)
        with closing(self._connection()) as db:
            self.assertEqual(6, db.execute(
                "SELECT remaining FROM main.gathering_nodes WHERE material='iron'"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM main.material_pouches"
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM main.action_receipts"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
