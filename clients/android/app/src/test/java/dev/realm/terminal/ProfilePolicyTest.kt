package dev.realm.terminal

import org.junit.Assert.*
import org.junit.Test

class ProfilePolicyTest {
    @Test fun httpsIsDefaultAndRequiresExpectedWorld() {
        HostProfile("Safe", "realm.example", 443, worldId = "siltglass-reach").validate()
        assertThrows(IllegalArgumentException::class.java) {
            HostProfile("Bad", "realm.example", 443).validate()
        }
        assertThrows(IllegalArgumentException::class.java) {
            HostProfile("Injected", "realm.example/path", 443, worldId = "demo").validate()
        }
    }

    @Test fun rawTelnetCannotConnectToRemoteHost() {
        assertThrows(IllegalArgumentException::class.java) {
            HostProfile("No", "realm.example", 23, TransportMode.LOCAL_TELNET_DEMO).validate()
        }
        HostProfile("Local", "127.0.0.1", 7777, TransportMode.LOCAL_TELNET_DEMO).validate()
        assertThrows(IllegalArgumentException::class.java) {
            HostProfile("Emulator", "10.0.2.2", 7777, TransportMode.LOCAL_TELNET_DEMO).validate()
        }
        HostProfile("Emulator", "10.0.2.2", 7777, TransportMode.LOCAL_TELNET_DEMO)
            .validate(allowEmulatorBridge = true)
    }
}
