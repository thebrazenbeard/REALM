"""Full REALM political contest via two independent OS HTTP clients."""
import importlib.util
from pathlib import Path
import unittest


class PoliticalRivalryProcessTests(unittest.TestCase):
    def test_two_client_contested_resource_control_persists_across_restart(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "synthetic_political_rivalry.py"
        spec = importlib.util.spec_from_file_location("synthetic_political_rivalry", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run_demo()
        self.assertEqual(["Alice", "Bob"], result["clients"])
        self.assertEqual(3, len({result["host_process_id"], *result["client_process_ids"]}))
        self.assertEqual(["Northern League", "Southern Compact"], result["control_sequence"])
        self.assertEqual(2, result["distinct_item_offerings"])
        self.assertEqual("Southern Compact", result["control_after_restart"])
        self.assertEqual(403, result["cross_actor_denial"])
        self.assertEqual([0, 0, 0], result["client_exit_codes"])
        self.assertEqual("verified", result["temporary_world_cleanup"])
        self.assertEqual(2, result["faction_revisions"])


if __name__ == "__main__":
    unittest.main()
