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


if __name__ == "__main__":
    unittest.main()
