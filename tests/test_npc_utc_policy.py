"""Host-driven NPC resource actions use the real UTC and game ledgers."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.crafting import Crafting
from realm.engine import Engine
from realm.gateway import Gateway
from realm.npc_game_theory import ResourceSite
from realm.world_clock import WorldClock

BASE = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
SITES = (ResourceSite("atrium", "iron"), ResourceSite("atrium", "copper"))


class NpcUtcPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "npc_world.sqlite3"
        Gateway(self.db, world_id="npc_world")
        self.engine = Engine(self.db)
        self.engine.join("Ada")
        self.engine.join("Ben")
        self.crafting = Crafting(self.db)
        for material in ("iron", "copper"):
            self.assertTrue(self.crafting.place_node("atrium", material, stock=6, batch_yield=3))
        self.clock = WorldClock(self.db)
        self.clock.enable()

    def test_due_event_commits_real_strategic_gather_and_receipt(self):
        try:
            from realm.npc_utc_policy import HostNpcGatherPolicy, perform_host_npc_gather_in_transaction
        except ModuleNotFoundError:
            self.fail("NPC UTC host action policy has not been implemented")
        permit = HostNpcGatherPolicy(
            policy_id="gather_ada", world_id="npc_world",
            actor="Ada", opponent="Ben", sites=SITES, seed=7,
            event_id="npc_ada_1", not_before_utc=BASE,
            expires_at_utc=BASE + timedelta(minutes=30))
        key = b"synthetic-host-key-not-a-player-credential-0001"
        mac = hmac.new(key, b"REALM/NPC-HOST-PERMIT/v1\x00" + permit.signing_bytes(),
                       hashlib.sha256).hexdigest()
        self.clock.schedule("npc_ada_1", due_at=BASE, kind="npc_economic_gather",
                            payload={"policy_id": "gather_ada", "world_id": "npc_world"})
        # A normal player may preclaim even a signed event ID with a read-only
        # command. It grants no host receipt and must not starve the UTC grant.
        self.assertIn("Commands:", self.engine.act(
            "Ada", "help", action_id="npc_ada_1"))
        results = []
        self.clock.tick(
            BASE, lambda db, event: results.append(
                perform_host_npc_gather_in_transaction(
                    db, event, engine=self.engine, crafting=self.crafting, policy=permit,
                    host_key=key, grant_mac_hex=mac, now_utc=BASE)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].choice.status, "VERIFIED_FOR_HOST_ACTION")
        self.assertIn("gather", results[0].response.lower())
        with closing(sqlite3.connect(self.db)) as db:
            events = db.execute("SELECT COUNT(*) FROM main.events WHERE kind='gather'").fetchone()[0]
            receipts = db.execute(
                "SELECT COUNT(*) FROM main.action_receipts WHERE actor='@clock:Ada'"
            ).fetchone()[0]
            total = db.execute("SELECT SUM(remaining) FROM main.gathering_nodes").fetchone()[0]
        self.assertEqual((events, receipts, total), (1, 1, 9))
        self.assertIsNotNone(self.clock.lookup("npc_ada_1").processed_at)


if __name__ == "__main__":
    unittest.main()
