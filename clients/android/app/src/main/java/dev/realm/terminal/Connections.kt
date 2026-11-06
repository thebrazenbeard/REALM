package dev.realm.terminal

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import javax.net.ssl.HttpsURLConnection

/** Transport callbacks are delivered on a worker thread; the UI posts to main. */
interface RealmConnection : Closeable {
    fun start()
    fun sendLine(line: String)
}

/** Explicitly LOCAL plaintext Telnet compatibility test, not remote hosting. */
class LocalTelnetConnection(
    private val profile: HostProfile,
    private val allowEmulatorBridge: Boolean,
    private val onText: (String) -> Unit,
    private val onStatus: (String) -> Unit,
) : RealmConnection {
    private val open = AtomicBoolean(false)
    private var socket: Socket? = null
    private val writeLock = Any()
    private val outgoing = Executors.newSingleThreadExecutor()

    override fun start() {
        require(profile.mode == TransportMode.LOCAL_TELNET_DEMO)
        profile.validate(allowEmulatorBridge)
        check(open.compareAndSet(false, true)) { "Already connected" }
        Thread({
            try {
                onStatus("Connecting to local plaintext test only...")
                val target = if (profile.host.equals("localhost", true)) "127.0.0.1" else profile.host
                val client = Socket()
                socket = client
                client.connect(InetSocketAddress(target, profile.port), 5000)
                onStatus("LOCAL TELNET — PLAINTEXT. Never enter a reusable password.")
                val codec = TelnetCodec()
                val decoder = Utf8Chunks()
                val input = client.getInputStream()
                val output = client.getOutputStream()
                val chunk = ByteArray(4096)
                while (open.get()) {
                    val count = input.read(chunk)
                    if (count < 0) break
                    val frame = codec.feed(chunk.copyOf(count))
                    if (frame.refusals.isNotEmpty()) {
                        synchronized(writeLock) { output.write(frame.refusals); output.flush() }
                    }
                    val text = decoder.feed(frame.body)
                    if (text.isNotEmpty()) onText(text)
                }
                onStatus("Disconnected")
            } catch (error: Exception) {
                if (open.get()) onStatus("Connection ended: ${error.javaClass.simpleName}")
            } finally {
                close()
            }
        }, "realm-telnet-reader").start()
    }

    override fun sendLine(line: String) {
        require(line.all { it.code >= 32 && it.code != 127 })
        val bytes = line.toByteArray(StandardCharsets.UTF_8)
        require(bytes.size in 1..512) { "Command must be 1–512 UTF-8 bytes" }
        outgoing.execute {
            try {
                synchronized(writeLock) {
                    val client = socket ?: throw IllegalStateException("Not connected")
                    client.getOutputStream().apply { write(bytes); write(byteArrayOf(13, 10)); flush() }
                }
            } catch (error: Exception) {
                if (open.get()) onStatus("Local send failed: ${error.javaClass.simpleName}")
            }
        }
    }

    override fun close() {
        open.set(false)
        try { socket?.close() } catch (_: Exception) { }
        socket = null
        outgoing.shutdownNow()
    }
}

/** HTTPS-only existing /api/v1 transport; bearer token stays in process memory. */
class HttpsGatewayConnection(
    private val profile: HostProfile,
    private val player: String,
    private val bearerToken: String,
    private val onText: (String) -> Unit,
    private val onStatus: (String) -> Unit,
) : RealmConnection {
    private val executor = Executors.newSingleThreadExecutor()
    private val closed = AtomicBoolean(false)
    private val started = AtomicBoolean(false)

    init {
        require(profile.mode == TransportMode.HTTPS_GATEWAY)
        profile.validate()
        require(player.matches(Regex("[A-Za-z][A-Za-z0-9_-]{0,23}"))) { "Invalid player name" }
        require(bearerToken.matches(Regex("[A-Za-z0-9_-]{16,256}"))) {
            "A host-issued base64url player token is required"
        }
    }

    override fun start() {
        check(started.compareAndSet(false, true)) { "Already connected" }
        executor.execute {
            try {
                onStatus("Authenticating HTTPS world ${profile.worldId}...")
                val health = call("GET", "/api/v1/health", null, false)
                require(health.optBoolean("ok") && health.optString("world_id") == profile.worldId) {
                    "Server world identity did not match the selected profile"
                }
                val joined = call("POST", "/api/v1/join", JSONObject().put("player", player), true)
                require(joined.optBoolean("ok")) { "Host denied the player join" }
                if (!closed.get()) {
                    onText(joined.optString("text", "Joined."))
                    onStatus("HTTPS connected — ${profile.worldId} / $player")
                }
            } catch (error: Exception) {
                if (!closed.get()) onStatus("HTTPS connection failed: ${error.javaClass.simpleName}")
            }
        }
    }

    override fun sendLine(line: String) {
        require(!closed.get())
        require(line.isNotBlank() && line.length <= 280 &&
            line.toByteArray(StandardCharsets.UTF_8).size <= 512 &&
            line.all { it.code >= 32 && it.code != 127 })
        executor.execute {
            try {
                val response = call("POST", "/api/v1/command", JSONObject()
                    .put("player", player).put("command", line)
                    .put("action_id", UUID.randomUUID().toString()), true)
                require(response.optBoolean("ok")) { "Host rejected command" }
                if (!closed.get()) onText(response.optString("text", ""))
            } catch (error: Exception) {
                if (!closed.get()) onStatus("Command status uncertain: ${error.javaClass.simpleName}; do not blindly retry")
            }
        }
    }

    private fun call(method: String, path: String, payload: JSONObject?, authenticated: Boolean): JSONObject {
        check(!closed.get())
        val connection = URL("https://${profile.host}:${profile.port}$path")
            .openConnection() as HttpsURLConnection
        try {
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 5000
            connection.readTimeout = 10000
            connection.requestMethod = method
            connection.setRequestProperty("Accept", "application/json")
            if (authenticated) connection.setRequestProperty("Authorization", "Bearer $bearerToken")
            if (payload != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(payload.toString().toByteArray(StandardCharsets.UTF_8)) }
            }
            val code = connection.responseCode
            require(code in 200..299) { "HTTPS status $code; redirects and insecure fallbacks refused" }
            val bytes = connection.inputStream.use { input ->
                val out = ByteArrayOutputStream()
                val block = ByteArray(4096)
                while (out.size() <= 64 * 1024) {
                    val count = input.read(block)
                    if (count < 0) break
                    out.write(block, 0, count)
                }
                out.toByteArray()
            }
            require(bytes.size <= 64 * 1024) { "Host response exceeded 64 KiB" }
            return JSONObject(bytes.toString(StandardCharsets.UTF_8))
        } finally {
            connection.disconnect()
        }
    }

    override fun close() {
        if (closed.compareAndSet(false, true)) executor.shutdownNow()
    }
}
