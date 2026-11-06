package dev.realm.terminal

/** No tokens, passwords or world canon belong in a saved client profile. */
enum class TransportMode { HTTPS_GATEWAY, LOCAL_TELNET_DEMO }

data class HostProfile(
    val label: String,
    val host: String,
    val port: Int,
    val mode: TransportMode = TransportMode.HTTPS_GATEWAY,
    val worldId: String = "",
) {
    fun validate(allowEmulatorBridge: Boolean = false) {
        require(label.isNotBlank() && label.length <= 50) { "Enter a short profile name" }
        require(port in 1..65535) { "Port must be 1–65535" }
        require(host.isNotBlank() && host.length <= 253) { "Host is required" }
        // The profile is a hostname, NOT a URL, path or string that can embed
        // credentials or redirect an app to a second scheme/endpoint.
        require(host.none { it == '/' || it == '\\' || it == '@' || it.isWhitespace() }) {
            "Enter a hostname without a URL, path or credentials"
        }
        if (mode == TransportMode.HTTPS_GATEWAY) {
            require(host.matches(Regex("[A-Za-z0-9.-]+"))) { "HTTPS needs a DNS name or IPv4 host" }
            require(worldId.matches(Regex("[a-z][a-z0-9_-]{0,63}"))) {
                "Enter the exact server world ID"
            }
        }
        if (mode == TransportMode.LOCAL_TELNET_DEMO) {
            val localhost = host.equals("localhost", true) || host == "127.0.0.1" || host == "::1"
            val emulator = allowEmulatorBridge && host == "10.0.2.2"
            require(localhost || emulator) {
                "Raw Telnet is permitted only on this device's loopback (or debug emulator bridge)"
            }
        }
    }
}
