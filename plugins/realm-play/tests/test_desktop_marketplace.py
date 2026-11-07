"""Validate the source-only, repo-scoped ChatGPT Desktop catalogue."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins" / "realm-play"


class DesktopMarketplaceTests(unittest.TestCase):
    def test_local_marketplace_points_to_existing_single_plugin(self):
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual("realm-local-development", marketplace["name"])
        entries = marketplace["plugins"]
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("realm-play", entry["name"])
        self.assertEqual({"source": "local", "path": "./plugins/realm-play"}, entry["source"])
        self.assertEqual("AVAILABLE", entry["policy"]["installation"])
        self.assertEqual("ON_INSTALL", entry["policy"]["authentication"])
        self.assertEqual(PLUGIN, ROOT / entry["source"]["path"])
        self.assertTrue((PLUGIN / "plugin.json").is_file())
        self.assertTrue((PLUGIN / "mcp.json").is_file())

    def test_source_only_desktop_package_does_not_claim_acl_or_host_access(self):
        manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
        mcp = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
        interface = manifest["extensions"]["com.openai"]["interface"]
        self.assertIn("Desktop", manifest["description"])
        self.assertIn("realm_status and realm_look", interface["longDescription"])
        self.assertIn("allowlist", interface["longDescription"])
        self.assertIn("older engines", interface["longDescription"])
        self.assertLessEqual(len(interface["shortDescription"]), 30)
        self.assertNotIn("apps", manifest["extensions"]["com.openai"])
        self.assertEqual({"realm-local"}, set(mcp["mcpServers"]))
        server = mcp["mcpServers"]["realm-local"]
        self.assertEqual("stdio", server["type"])
        self.assertNotIn("url", server)
        self.assertNotIn("tunnelId", server)
        self.assertNotIn("env", server)


if __name__ == "__main__":
    unittest.main()
