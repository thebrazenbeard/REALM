"""End-to-end disposable two-client play smoke; no installed service or lore."""
from __future__ import annotations

import importlib.util
import pathlib
import unittest


class SyntheticForegroundPlayTests(unittest.TestCase):
    def test_two_clients_npc_ship_arrival_and_replay(self):
        script = pathlib.Path(__file__).resolve().parents[1] / "examples" / "synthetic_foreground_play.py"
        spec = importlib.util.spec_from_file_location("synthetic_foreground_play", script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        result = module.run_demo()
        self.assertEqual(64, len(result["validated_worldpack_sha256"]))
        self.assertEqual(["Captain", "Navigator"], result["clients"])
        self.assertEqual({"Captain": 1, "Navigator": 1}, result["npc_feed_events_each"])
        self.assertTrue(result["ship_departed"])
        self.assertEqual("demo_moon", result["ship_arrived_at"])
        self.assertEqual({"npc": 1, "arrival": 1}, result["receipts"])


if __name__ == "__main__":
    unittest.main()
