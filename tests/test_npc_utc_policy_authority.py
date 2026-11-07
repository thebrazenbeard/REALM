"""Host grant cannot originate from a queued event or model proposal."""
from __future__ import annotations
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from realm.gateway import Gateway
from realm.engine import Engine
from realm.crafting import Crafting
from realm.world_clock import WorldClock
from realm.npc_game_theory import ResourceSite
from realm.npc_utc_policy import HostNpcGatherPolicy, perform_host_npc_gather_in_transaction

BASE = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
KEY = b"synthetic-host-key-not-a-player-credential-0001"
DOMAIN = b"REALM/NPC-HOST-PERMIT/v1\x00"
SITES = (ResourceSite("atrium", "iron"), ResourceSite("atrium", "copper"))


class HostNpcAuthorityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "host.sqlite3"
        Gateway(self.db, world_id="npc_world")
        self.engine = Engine(self.db)
        for actor in ("Ada", "Ben"):
            self.engine.join(actor)
        self.crafting = Crafting(self.db)
        for material in ("iron", "copper"):
            self.crafting.place_node("atrium", material, stock=6)
        self.clock = WorldClock(self.db)
        self.clock.enable()

    def policy(self):
        try:
            return HostNpcGatherPolicy(
                policy_id="host_policy", world_id="npc_world",
                actor="Ada", opponent="Ben", sites=SITES, seed=7,
                event_id="npc_ada_1", not_before_utc=BASE,
                expires_at_utc=BASE + timedelta(minutes=30))
        except TypeError:
            self.fail("Host grant lacks event-ID and UTC-window binding")

    def signed(self, policy):
        return hmac.new(KEY, DOMAIN + policy.signing_bytes(), hashlib.sha256).hexdigest()

    def test_host_key_and_exact_actor_are_required(self):
        policy = self.policy()
        sig = self.signed(policy)
        self.clock.schedule("npc_ada_1", due_at=BASE, kind="npc_economic_gather",
                            payload={"policy_id":"host_policy","world_id":"npc_world"})
        def unauthorized(db, event):
            changed = replace(policy, actor="Ben", opponent="Ada")
            perform_host_npc_gather_in_transaction(
                db, event, engine=self.engine, crafting=self.crafting,
                policy=changed, host_key=KEY, grant_mac_hex=sig, now_utc=BASE)
        with self.assertRaisesRegex(ValueError, "grant"):
            self.clock.tick(BASE, unauthorized)
        self.assertIsNone(self.clock.lookup("npc_ada_1").processed_at)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM main.action_receipts WHERE actor='Ben'").fetchone()[0])

if __name__ == "__main__":
    unittest.main()
