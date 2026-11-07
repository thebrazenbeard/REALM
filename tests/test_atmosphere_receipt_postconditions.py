"""Hostile main-schema trigger tests for atmosphere action acknowledgment."""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from realm.atmosphere import AtmosphereModel
from realm.atmosphere_ledger import AtmosphereLedger
from realm.engine import Engine


class ReceiptPostconditionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "synthetic.sqlite3"
        Engine(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO world_meta(key,value) VALUES('world_id','synthetic')")
        self.ledger = AtmosphereLedger(self.path)
        self.ledger.enable(world_id="synthetic", model=AtmosphereModel(
            compartments={"a": {"volume_l": 1000, "pressure_pa": 100000,
                                "oxygen_ppm": 210000, "temperature_mk": 293150},
                          "b": {"volume_l": 1000, "pressure_pa": 0,
                                "oxygen_ppm": 0, "temperature_mk": 293150}},
            hatches={"h": {"a": "a", "b": "b", "operators": ["pilot"],
                            "open": False, "operational": True}}, occupants={}))

    def _open(self, *, expected_version=0):
        return self.ledger.apply(world_id="synthetic", action_id="opened",
            operation="hatch", params={"hatch_id": "h", "open": True, "actor": "pilot"},
            expected_version=expected_version)

    def _state(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT version,state_json FROM main.atm_state WHERE id=1").fetchone(), \
                db.execute("SELECT action_id,result_json FROM main.atm_receipts").fetchall()

    def _install(self, sql):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(sql)

    def test_late_trigger_cannot_delete_success_receipt(self):
        before = self._state()
        self._install("""CREATE TRIGGER atm_erase_ack AFTER INSERT ON atm_receipts
            BEGIN DELETE FROM atm_receipts WHERE action_id=NEW.action_id; END""")
        with self.assertRaises(ValueError):
            self._open()
        self.assertEqual(before, self._state())
        self._install("DROP TRIGGER atm_erase_ack")
        self.assertEqual(1, self._open()["version"])
        self.assertEqual(1, len(self._state()[1]))

    def test_late_trigger_cannot_rewrite_persisted_state_but_keep_receipt(self):
        before = self._state()
        baseline_json = before[0][1].replace("'", "''")
        self._install("""CREATE TRIGGER atm_revert_state AFTER INSERT ON atm_receipts
            BEGIN UPDATE atm_state SET state_json='%s' WHERE id=1; END""" % baseline_json)
        with self.assertRaises(ValueError):
            self._open()
        self.assertEqual(before, self._state())

    def test_late_trigger_cannot_rewrite_acknowledged_receipt_payload(self):
        before = self._state()
        self._install("""CREATE TRIGGER atm_change_ack AFTER INSERT ON atm_receipts
            BEGIN UPDATE atm_receipts SET result_json='{"version":999}'
                WHERE action_id=NEW.action_id; END""")
        with self.assertRaises(ValueError):
            self._open()
        self.assertEqual(before, self._state())

    def test_caught_host_transaction_failure_rolls_back_only_nested_action(self):
        before = self._state()
        self._install("CREATE TABLE audit(marker TEXT NOT NULL)")
        self._install("""CREATE TRIGGER atm_erase_ack AFTER INSERT ON atm_receipts
            BEGIN DELETE FROM atm_receipts WHERE action_id=NEW.action_id; END""")
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                self.ledger.apply_in_transaction(db, world_id="synthetic",
                    action_id="opened", operation="hatch",
                    params={"hatch_id": "h", "open": True, "actor": "pilot"},
                    expected_version=0)
            db.execute("INSERT INTO audit(marker) VALUES('independent')")
            db.commit()
        self.assertEqual(before, self._state())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([("independent",)], db.execute("SELECT marker FROM audit").fetchall())

    def test_unrelated_audit_event_is_not_rejected(self):
        self._install("CREATE TABLE audit(marker TEXT NOT NULL)")
        self._install("""CREATE TRIGGER atm_benign AFTER INSERT ON atm_receipts
            BEGIN INSERT INTO audit(marker) VALUES('unrelated'); END""")
        self.assertEqual(1, self._open()["version"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([("unrelated",)], db.execute("SELECT marker FROM audit").fetchall())

    def test_extra_future_receipt_cannot_be_forged_by_current_action(self):
        before = self._state()
        self._install("""CREATE TRIGGER atm_extra_receipt AFTER INSERT ON atm_receipts
            WHEN NEW.action_id='opened' BEGIN
              INSERT INTO atm_receipts(action_id,world_id,command_json,result_json)
              VALUES('future','synthetic',
                '{"expected_version":1,"operation":"tick","params":{},"world_id":"synthetic"}',
                '{"result":{"forged":"true"},"version":2}'); END""")
        with self.assertRaises(ValueError):
            self._open()
        self.assertEqual(before, self._state())
        self._install("DROP TRIGGER atm_extra_receipt")
        self.assertEqual(1, self._open()["version"])
        tick = self.ledger.apply(world_id="synthetic", action_id="future",
            operation="tick", params={}, expected_version=1)
        self.assertEqual(2, tick["version"])
        self.assertNotIn("forged", tick["result"])
        self.assertEqual(2, self.ledger.status(world_id="synthetic")["version"])
        self.assertEqual(2, len(self._state()[1]))

    def test_extra_foreign_world_receipt_cannot_be_inserted(self):
        before = self._state()
        self._install("""CREATE TRIGGER atm_cross_world AFTER INSERT ON atm_receipts
            WHEN NEW.action_id='opened' BEGIN
              INSERT INTO atm_receipts(action_id,world_id,command_json,result_json)
              VALUES('foreign','otherWorld','{}','{}'); END""")
        with self.assertRaises(ValueError):
            self._open()
        self.assertEqual(before, self._state())

    def test_legitimate_prior_receipt_is_preserved(self):
        first = self.ledger.apply(world_id="synthetic", action_id="breached",
            operation="breach", params={"chamber_id": "b", "loss_pa_l_per_tick": 50},
            expected_version=0)
        self.assertEqual(1, first["version"])
        previous = dict(self._state()[1])["breached"]
        self.assertEqual(2, self._open(expected_version=1)["version"])
        self.assertEqual(previous, dict(self._state()[1])["breached"])
        self.assertEqual(2, len(self._state()[1]))

    def test_trigger_cannot_rewrite_unrelated_existing_receipt(self):
        self.ledger.apply(world_id="synthetic", action_id="breached",
            operation="breach", params={"chamber_id": "b", "loss_pa_l_per_tick": 50},
            expected_version=0)
        before = self._state()
        self._install("""CREATE TRIGGER atm_rewrite_prior AFTER INSERT ON atm_receipts
            WHEN NEW.action_id='opened' BEGIN
              UPDATE atm_receipts SET result_json='{"version":777}'
              WHERE action_id='breached'; END""")
        with self.assertRaises(ValueError):
            self._open(expected_version=1)
        self.assertEqual(before, self._state())


if __name__ == "__main__":
    unittest.main()
