"""Two independent OS clients against one disposable authenticated REALM."""
from __future__ import annotations

import importlib.util
import pathlib
import unittest


class SyntheticMultiprocessPlayTests(unittest.TestCase):
    def test_independent_processes_scope_replay_and_cleanup(self):
        script = pathlib.Path(__file__).resolve().parents[1] / "examples" / "synthetic_multiprocess_play.py"
        spec = importlib.util.spec_from_file_location("synthetic_multiprocess_play", script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        result = module.run_demo()
        self.assertEqual(["Captain", "Navigator"], result["clients"])
        self.assertEqual(3, len(set(result["client_process_ids"] + [result["host_process_id"]])))
        self.assertEqual(64, len(result["worldpack_sha256"]))
        self.assertTrue(result["ship_departed_by_captain"])
        self.assertEqual("destination", result["ship_arrived_host_utc"])
        self.assertEqual(2, result["committed_utc_clock_receipts"])
        self.assertGreater(result["shared_npc_event_id"], 0)
        self.assertEqual({"depart": 1, "arrive": 1}, result["ship_events"])
        self.assertEqual({"npc": 1, "ship_arrival": 1}, result["receipts"])
        self.assertEqual({"wrong_player": 403, "navigator_ship_depart": 401},
                         result["scope_checks"])
        self.assertFalse(result["native_ship_feed_visible"])
        self.assertEqual([0, 0], result["client_exit_codes"])
        self.assertEqual("verified", result["temporary_world_cleanup"])


if __name__ == "__main__":
    unittest.main()
