"""Public, source-only contested site control with authentic item custody."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway
from realm.political_oaths import WorldPolitics
from realm.political_leverage import WorldLeverage


class PoliticalLeverageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "world.sqlite3"
        self.game = Gateway(self.path, world_id="political_test")
        self.game.join("Alice")
        self.game.join("Bob")

    def command(self, actor: str, action: str, receipt=None):
        return self.game.command(actor, action, receipt)["text"]

    def rows(self, sql: str, params=()):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, params).fetchall()

    def initialize(self):
        politics = WorldPolitics(self.path, world_id="political_test")
        politics.install()
        politics.create_faction("north", "Northern League")
        politics.create_faction("south", "Southern Compact")
        leverage = WorldLeverage(self.path, world_id="political_test")
        leverage.install()
        leverage.define_site("council", "Council Chamber", room_id="atrium")
        leverage.authorize_resource("token", points=1)
        return leverage

    def create_item(self, item_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO items(id,name,description,room_id) VALUES(?,?,?,?)",
                (item_id, item_id.title(), "An original expendable demo token.", "atrium"),
            )

    def test_world_without_leverage_stays_unmodified(self):
        self.assertEqual("Political sites are not enabled.",
                         self.command("Alice", "sites"))
        self.assertEqual("Political sites are not enabled.",
                         self.command("Alice", "control council"))
        self.assertEqual("Political sites are not enabled.",
                         self.command("Alice", "offer token to council"))
        self.assertFalse(self.rows(
            "SELECT name FROM sqlite_master WHERE name='political_sites'"
        ))

    def test_rivals_contest_and_take_control_by_spending_real_inventory(self):
        self.initialize()
        self.create_item("seal")
        self.create_item("banner")
        self.initialize_more_resources()
        self.command("Alice", "pledge north")
        self.command("Bob", "pledge south")
        self.assertIn("Unclaimed", self.command("Alice", "control council"))
        self.assertIn("Council Chamber", self.command("Alice", "sites"))
        self.assertIn("You take Brass token", self.command("Alice", "take token"))
        first = self.command("Alice", "offer token to council", "offer-a")
        self.assertEqual("Alice offered Brass token to Northern League at Council Chamber (1 influence).", first)
        self.assertIn("Controlled by Northern League", self.command("Bob", "control council"))
        self.assertEqual([], self.rows("SELECT id FROM items WHERE id='token'"))
        self.assertIn("nothing", self.command("Alice", "inventory"))
        self.assertIn("You take Seal", self.command("Bob", "take seal"))
        self.assertIn("offered Seal to Southern Compact", self._make_tie())
        self.assertIn("Contested", self.command("Bob", "control council"))
        self.assertIn("offered Banner to Southern Compact", self._break_tie())
        self.assertIn("Controlled by Southern Compact", self.command("Alice", "control council"))
        self.assertEqual(3, self.rows("SELECT COUNT(*) FROM political_offerings")[0][0])
        other = Gateway(self.path, world_id="political_test")
        self.assertIn("Controlled by Southern Compact", other.command("Alice", "control council", None)["text"])
        self.assertEqual(3, len(self.rows("SELECT id FROM events WHERE kind='political_offer'")))

    def initialize_more_resources(self):
        self.host = WorldLeverage(self.path, world_id="political_test")
        self.host.authorize_resource("seal", points=1)
        self.host.authorize_resource("banner", points=1)

    def _make_tie(self):
        return self.command("Bob", "offer seal to council", "offer-b")

    def _break_tie(self):
        self.command("Bob", "take banner")
        return self.command("Bob", "offer banner to council", "offer-c")

    def test_no_unheld_unapproved_or_distant_contribution(self):
        self.initialize()
        self.create_item("seal")
        self.command("Alice", "pledge north")
        self.assertIn("not carrying", self.command("Alice", "offer token to council"))
        self.command("Alice", "take token")
        self.command("Alice", "take seal")
        self.assertIn("not designated", self.command("Alice", "offer seal to council"))
        self.command("Alice", "north")
        self.assertIn("must stand", self.command("Alice", "offer token to council"))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))
        self.assertEqual([("Alice",)], self.rows("SELECT held_by FROM items WHERE id='token'"))
        self.assertEqual([("Alice",)], self.rows("SELECT held_by FROM items WHERE id='seal'"))

    def test_unaffiliated_and_shared_token_cannot_claim_or_pledge(self):
        self.initialize()
        self.command("Alice", "take token")
        self.assertIn("must pledge", self.command("Alice", "offer token to council"))
        self.assertEqual("Political pledges require player-bound credentials.",
                         self.game.engine.act("Alice", "offer token to council",
                                              private_channels=False))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))
        self.assertIn("nothing", self.command("Alice", "allegiance").replace("unaffiliated", "nothing"))

    def test_duplicate_action_and_regenerated_item_id_cannot_spend_again(self):
        host = self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        first = self.command("Alice", "offer token to council", "once")
        self.assertEqual(first, self.command("Alice", "offer token to council", "once"))
        self.assertEqual(1, len(self.rows("SELECT id FROM political_offerings")))
        self.create_item("token")
        self.command("Alice", "take token")
        self.assertIn("already contributed", self.command("Alice", "offer token to council", "fresh"))
        self.assertEqual(1, len(self.rows("SELECT id FROM political_offerings")))
        self.assertEqual([("Alice",)], self.rows("SELECT held_by FROM items WHERE id='token'"))
        with self.assertRaises(ValueError):
            host.authorize_resource("token", points=1)

    def test_event_failure_rolls_back_retired_item_offer_and_receipt(self):
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TRIGGER abort_offer BEFORE INSERT ON events "
                "WHEN NEW.kind='political_offer' "
                "BEGIN SELECT RAISE(ABORT,'blocked political offer'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.command("Alice", "offer token to council", "txn")
        self.assertEqual([("Alice",)], self.rows("SELECT held_by FROM items WHERE id='token'"))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))
        self.assertEqual([], self.rows(
            "SELECT action_id FROM action_receipts WHERE action_id='txn'"
        ))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER abort_offer")
        self.assertIn("offered Brass token", self.command("Alice", "offer token to council", "txn"))
        self.assertEqual([], self.rows("SELECT id FROM items WHERE id='token'"))
        self.assertEqual(1, len(self.rows("SELECT id FROM political_offerings")))

    def test_competing_clients_cannot_double_offer_one_held_item(self):
        from concurrent.futures import ThreadPoolExecutor
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        rival_client = Gateway(self.path, world_id="political_test")
        def submit(index):
            return rival_client.command(
                "Alice", "offer token to council", f"parallel-{index}",
            )["text"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, (1, 2)))
        self.assertEqual(1, sum("offered Brass token" in x for x in responses))
        self.assertEqual(1, sum("not carrying" in x for x in responses))
        self.assertEqual(1, len(self.rows("SELECT item_id FROM political_offerings")))

    def test_world_mismatch_and_partial_schema_fail_closed(self):
        from realm.political_leverage import WorldLeverage
        foreign = WorldLeverage(self.path, world_id="other_world")
        with self.assertRaises(PermissionError):
            foreign.install()
        with self.assertRaises(PermissionError):
            foreign.define_site("false", "Other Place", room_id="atrium")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE political_sites(id TEXT PRIMARY KEY)")
        with self.assertRaises(RuntimeError):
            self.command("Alice", "control council")

    def test_host_cannot_reprice_or_relocate_contested_assets(self):
        host = self.initialize()
        host.define_site("council", "Council Chamber", room_id="atrium")
        host.authorize_resource("token", points=1)
        with self.assertRaises(ValueError):
            host.authorize_resource("token", points=5)
        with self.assertRaises(ValueError):
            host.define_site("council", "Royal Palace", room_id="atrium")
        with self.assertRaises(ValueError):
            host.define_site("council", "Council Chamber", room_id="corridor")
        with self.assertRaises(ValueError):
            host.define_site("bogus", "Bogus", room_id="not_a_room")
        with self.assertRaises(ValueError):
            host.authorize_resource("phantom", points=1)
        with self.assertRaises(ValueError):
            host.authorize_resource("wrench", points=True)
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))

    def test_offering_does_not_mint_group_role_or_privileged_crewchat(self):
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        self.command("Alice", "offer token to council")
        self.assertIn("No active group channel membership",
                      self.command("Alice", "crewchat Offer grants admiral rank"))
        self.assertEqual([], self.rows("SELECT kind FROM events WHERE kind='crewchat'"))

    def test_room_feed_and_history_reveal_only_locally_witnessed_offerings(self):
        self.initialize()
        cursor = self.game.join("Bob")["cursor"]
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        self.command("Alice", "offer token to council")
        feed = self.game.feed("Bob", cursor)["events"]
        expected = "Alice offered Brass token to Northern League at Council Chamber (1 influence)."
        self.assertTrue(any(x["kind"] == "political_offer" and x["text"] == expected
                            for x in feed), feed)
        self.assertIn(expected, self.command("Bob", "history 50"))
        self.command("Bob", "north")
        self.assertFalse(any(x["kind"] == "political_offer"
                             for x in self.game.feed("Bob", 0)["events"]))


    def test_host_cli_installs_sites_and_approved_resources_without_a_listener(self):
        from realm.gateway import main as gateway_main
        fresh = self.path.parent / "cli_leverage.sqlite3"
        self.assertEqual(0, gateway_main([
            "--db", str(fresh), "--world-id", "leverage_cli",
            "--install-politics", "--political-faction", "north=Northern League",
        ]))
        self.assertEqual(0, gateway_main([
            "--db", str(fresh), "--world-id", "leverage_cli",
            "--install-leverage",
            "--political-site", "council@atrium=Council Chamber",
            "--political-resource", "token=1",
        ]))
        started = Gateway(fresh, world_id="leverage_cli")
        started.join("Alice")
        started.command("Alice", "pledge north", None)
        started.command("Alice", "take token", None)
        self.assertIn("offered Brass token", started.command(
            "Alice", "offer token to council", None
        )["text"])
        self.assertIn("Controlled by Northern League", started.command(
            "Alice", "control council", None
        )["text"])

    def test_invalid_operator_specs_abort_before_creating_world(self):
        from realm.gateway import main as gateway_main
        missing = self.path.parent / "must_not_exist.sqlite3"
        scenarios = (
            ("--political-site", "council@atrium=Council Chamber"),
            ("--install-leverage", "--political-site", "bad;id@atrium=Forged"),
            ("--install-leverage", "--political-resource", "token=0"),
        )
        for parts in scenarios:
            with self.subTest(parts=parts):
                with self.assertRaises(SystemExit):
                    gateway_main([
                        "--db", str(missing), "--world-id", "cli_test", *parts
                    ])
                self.assertFalse(missing.exists())

    def test_host_cli_batch_rejects_missing_item_without_partial_site_registration(self):
        from realm.gateway import main as gateway_main
        politics = WorldPolitics(self.path, world_id="political_test")
        politics.install()
        politics.create_faction("north", "Northern League")
        with self.assertRaises(ValueError):
            gateway_main([
                "--db", str(self.path), "--world-id", "political_test",
                "--install-leverage",
                "--political-site", "council@atrium=Council Chamber",
                "--political-resource", "phantom=2",
            ])
        if self.rows("SELECT name FROM sqlite_master WHERE name='political_sites'"):
            self.assertEqual([], self.rows("SELECT id FROM political_sites"))

    def test_privileged_event_ignore_cannot_acknowledge_a_spent_item(self):
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TRIGGER suppress_offer BEFORE INSERT ON events "
                "WHEN NEW.kind='political_offer' "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        with self.assertRaises((ValueError, RuntimeError)):
            self.command("Alice", "offer token to council", "must-not-ack")
        self.assertEqual([("Alice",)], self.rows(
            "SELECT held_by FROM items WHERE id='token'"
        ))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))
        self.assertEqual([], self.rows(
            "SELECT action_id FROM action_receipts WHERE action_id='must-not-ack'"
        ))

    def test_post_insert_event_deletion_does_not_acknowledge_influence(self):
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TRIGGER hide_offer AFTER INSERT ON events "
                "WHEN NEW.kind='political_offer' "
                "BEGIN DELETE FROM events WHERE id=NEW.id; END"
            )
        with self.assertRaises((ValueError, RuntimeError)):
            self.command("Alice", "offer token to council", "false-success")
        self.assertEqual([("Alice",)], self.rows(
            "SELECT held_by FROM items WHERE id='token'"
        ))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))


    def test_receipt_after_insert_event_tamper_cannot_claim_success(self):
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TRIGGER suppress_after_receipt AFTER INSERT ON action_receipts "
                "WHEN NEW.command='offer token to council' "
                "BEGIN DELETE FROM events WHERE kind='political_offer' "
                "AND actor=NEW.actor; END"
            )
        with self.assertRaises(RuntimeError):
            self.command("Alice", "offer token to council", "tamper-event")
        self.assertEqual([("Alice",)], self.rows(
            "SELECT held_by FROM items WHERE id='token'"
        ))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))
        self.assertEqual([], self.rows(
            "SELECT action_id FROM action_receipts WHERE action_id='tamper-event'"
        ))

    def test_receipt_after_insert_item_resurrection_cannot_acknowledge_double_custody(self):
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TRIGGER resurrect_after_receipt AFTER INSERT ON action_receipts "
                "WHEN NEW.command='offer token to council' "
                "BEGIN INSERT INTO items(id,name,description,room_id) "
                "VALUES('token','Brass token','Forged extra copy','atrium'); END"
            )
        with self.assertRaises(RuntimeError):
            self.command("Alice", "offer token to council", "tamper-item")
        self.assertEqual([("Alice",)], self.rows(
            "SELECT held_by FROM items WHERE id='token'"
        ))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))

    def test_ignored_action_receipt_cannot_acknowledge_offered_item(self):
        self.initialize()
        self.command("Alice", "pledge north")
        self.command("Alice", "take token")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TRIGGER ignore_receipt BEFORE INSERT ON action_receipts "
                "WHEN NEW.command='offer token to council' "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        with self.assertRaises(RuntimeError):
            self.command("Alice", "offer token to council", "ignored-receipt")
        self.assertEqual([("Alice",)], self.rows(
            "SELECT held_by FROM items WHERE id='token'"
        ))
        self.assertEqual([], self.rows("SELECT item_id FROM political_offerings"))


if __name__ == "__main__":
    unittest.main()
