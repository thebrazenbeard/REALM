"""Static, offline source-boundary assertions; these are NOT APK build tests."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "clients" / "android"
SRC = ANDROID / "app" / "src" / "main" / "java" / "dev" / "realm" / "terminal"


class AndroidSourceBoundaryTests(unittest.TestCase):
    def test_scaffold_is_separate_and_contains_no_embedded_world_or_weights(self):
        for name in ("MainActivity.kt", "Connections.kt", "Profile.kt", "ProfileStore.kt", "TelnetCodec.kt", "SafeAnsi.kt"):
            self.assertTrue((SRC / name).is_file(), name)
        for path in ANDROID.rglob("*"):
            if path.is_file():
                self.assertNotIn(path.suffix.lower(), {".gguf", ".safetensors", ".sqlite3", ".jks"})

    def test_manifest_blocks_backup_and_cleartext(self):
        manifest = (ANDROID / "app" / "src" / "main" / "AndroidManifest.xml").read_text()
        self.assertIn('android:allowBackup="false"', manifest)
        self.assertIn('android:usesCleartextTraffic="false"', manifest)
        self.assertIn('android:dataExtractionRules="@xml/data_extraction_rules"', manifest)
        self.assertIn('android.permission.INTERNET', manifest)
        rules = (ANDROID / "app" / "src" / "main" / "res" / "xml" / "data_extraction_rules.xml").read_text()
        self.assertIn("<cloud-backup>", rules)
        self.assertIn("<device-transfer>", rules)

    def test_remote_transport_is_https_only_and_no_redirects(self):
        source = (SRC / "Connections.kt").read_text()
        self.assertIn('URL("https://', source)
        self.assertIn("as HttpsURLConnection", source)
        self.assertIn("instanceFollowRedirects = false", source)
        self.assertNotIn("HostnameVerifier {", source)
        self.assertNotIn("TrustManager", source)

    def test_telnet_is_explicit_local_debug_only(self):
        profile = (SRC / "Profile.kt").read_text()
        self.assertIn('host == "127.0.0.1"', profile)
        self.assertIn('allowEmulatorBridge && host == "10.0.2.2"', profile)
        self.assertIn("require(localhost || emulator)", profile)
        ui = (SRC / "MainActivity.kt").read_text()
        self.assertIn("localConsent", ui)
        self.assertIn("BuildConfig.DEBUG", ui)
        self.assertIn("awaitingLocalToken = p.mode == TransportMode.LOCAL_TELNET_DEMO", ui)
        self.assertIn("activeMode == TransportMode.LOCAL_TELNET_DEMO", ui)
        self.assertIn("tokenPromptDetector.scan(text)", ui)
        self.assertIn("localHandshakeComplete = true", ui)

    def test_profiles_store_no_secrets_and_ui_token_not_saveable(self):
        stored = (SRC / "ProfileStore.kt").read_text()
        self.assertNotIn('put("token"', stored)
        self.assertNotIn('put("password"', stored)
        ui = (SRC / "MainActivity.kt").read_text()
        self.assertIn('var token by remember { mutableStateOf("") }', ui)
        self.assertNotIn('var token by rememberSaveable', ui)
        self.assertIn("PasswordVisualTransformation()", ui)

    def test_ansi_defuses_osc_dcs_and_telnet_subnegotiation(self):
        ansi = (SRC / "SafeAnsi.kt").read_text()
        self.assertIn("Mode.OSC", ansi)
        self.assertIn("Mode.DCS", ansi)
        self.assertIn("if (ch == 'm') applySgr", ansi)
        telnet = (SRC / "TelnetCodec.kt").read_text()
        self.assertIn("State.SUBNEGOTIATION", telnet)
        self.assertIn("State.OPTION", telnet)


if __name__ == "__main__":
    unittest.main()
