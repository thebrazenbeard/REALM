"""Source-only portable plugin contract checks."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginManifestTests(unittest.TestCase):
    def test_portable_root_manifest_and_nonempty_skill(self):
        doc = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", doc["$schema"])
        self.assertEqual("realm-play", doc["name"])
        self.assertRegex(doc["version"], r"^\d+\.\d+\.\d+$")
        interface = doc["extensions"]["com.openai"]["interface"]
        self.assertTrue(interface["displayName"])
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        self.assertIsInstance(interface["capabilities"], list)
        self.assertNotIn("mcpServers", doc)

        skill = (ROOT / "skills" / "play-realm" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\n"))
        frontmatter = skill.split("---\n", 2)[1]
        self.assertRegex(frontmatter, r"(?m)^name: play-realm$")
        self.assertRegex(frontmatter, r"(?m)^description: .+$")

    def test_real_local_server_command_without_remote_url_or_credentials(self):
        doc = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual("https://agent-plugins.org/schemas/1.0.0/mcp.schema.json", doc["$schema"])
        self.assertEqual({"realm-local"}, set(doc["mcpServers"]))
        server = doc["mcpServers"]["realm-local"]
        self.assertEqual("stdio", server["type"])
        self.assertEqual("python", server["command"])
        self.assertEqual(["${PLUGIN_ROOT}/server/realm_mcp.py"], server["args"])
        self.assertEqual("${PLUGIN_ROOT}", server["cwd"])
        self.assertTrue((ROOT / "server" / "realm_mcp.py").is_file())
        self.assertFalse(any(key in server for key in ("url", "auth", "token", "apiKey")))

    def test_selected_backend_contract_and_truthful_release_boundary(self):
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("0.2.0", manifest["version"])
        documentation = (ROOT / "README.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "play-realm" / "SKILL.md").read_text(encoding="utf-8")
        launcher = (ROOT / "server" / "realm_mcp.py").read_text(encoding="utf-8")

        for name in ("realm_status", "realm_join", "realm_look", "realm_inventory",
                     "realm_move", "realm_command", "realm_say", "realm_feed", "realm_map"):
            self.assertIn(name, documentation)
            self.assertIn(name, skill)
        for setting in ("REALM_ROOT", "REALM_DB_PATH", "REALM_WORLD_ID",
                        "REALM_PLAYER_TOKEN"):
            self.assertIn(setting, documentation)
        self.assertIn("realm.mcp_server", launcher)
        self.assertNotIn("ResidentController", launcher)
        self.assertNotIn("REALM_ACTOR", launcher)

        self.assertIn("action_id", skill)
        self.assertIn("280 characters", skill)
        self.assertIn("Temporal privacy not qualified", skill)
        self.assertIn("Fog-of-war not qualified", skill)
        self.assertIn("not installed", documentation)
        self.assertIn("Lappy installation", documentation)


if __name__ == "__main__":
    unittest.main()
