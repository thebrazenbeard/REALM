package dev.realm.terminal

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.sp
import androidx.compose.ui.unit.dp
import java.util.concurrent.atomic.AtomicInteger


class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { MaterialTheme(colorScheme = darkColorScheme()) { RealmTerminalScreen() } }
    }
}

@Composable
private fun RealmTerminalScreen() {
    val context = LocalContext.current
    val mainThread = remember { Handler(Looper.getMainLooper()) }
    val generation = remember { AtomicInteger(0) }
    var profiles by remember { mutableStateOf(ProfileStore.load(context)) }
    var profileName by rememberSaveable { mutableStateOf("My REALM") }
    var host by rememberSaveable { mutableStateOf("") }
    var port by rememberSaveable { mutableStateOf("443") }
    var worldId by rememberSaveable { mutableStateOf("siltglass-reach") }
    var player by rememberSaveable { mutableStateOf("") }
    var token by remember { mutableStateOf("") } // Deliberately not saveable.
    var mode by remember { mutableStateOf(TransportMode.HTTPS_GATEWAY) }
    var localConsent by remember { mutableStateOf(false) }
    var activeMode by remember { mutableStateOf<TransportMode?>(null) }
    var localHandshakeComplete by remember { mutableStateOf(false) }
    var showSettings by rememberSaveable { mutableStateOf(true) }
    var status by remember { mutableStateOf("Disconnected") }
    var transcript by remember { mutableStateOf(listOf<StyledText>()) }
    var filter by remember { mutableStateOf(SafeAnsi()) }
    var command by remember { mutableStateOf("") }
    var awaitingLocalToken by remember { mutableStateOf(false) }
    val tokenPromptDetector = remember { TokenPromptDetector() }
    var fontSize by rememberSaveable { mutableStateOf(16f) }
    var connection by remember { mutableStateOf<RealmConnection?>(null) }
    val listState = rememberLazyListState()

    fun appendText(text: String) {
        val clean = filter.feed(text)
        if (activeMode == TransportMode.LOCAL_TELNET_DEMO && !localHandshakeComplete) {
            when (tokenPromptDetector.scan(text)) {
                TokenPromptDetector.Prompt.TOKEN -> awaitingLocalToken = true
                TokenPromptDetector.Prompt.NAME -> {
                    // REALM's local server prints Name: only AFTER accepting
                    // the one-time token. Until then, keep input masked.
                    awaitingLocalToken = false
                    localHandshakeComplete = true
                }
                TokenPromptDetector.Prompt.NONE -> Unit
            }
        }
        if (clean.isNotEmpty()) transcript = (transcript + clean).takeLast(1000)
    }

    fun disconnect() {
        generation.incrementAndGet()
        connection?.close()
        connection = null
        token = ""
        localConsent = false
        activeMode = null
        localHandshakeComplete = false
        awaitingLocalToken = false
        tokenPromptDetector.reset()
        status = "Disconnected"
    }

    fun send() {
        val sending = command
        command = "" // Never mirror typed commands; a local token could be one.
        if (sending.isBlank()) return
        try {
            connection?.sendLine(sending)
            // Queuing a socket write is not evidence the local token was
            // accepted. Keep input masked until verified Name: server prompt.
        } catch (error: Exception) {
            status = "Send rejected: ${error.javaClass.simpleName}"
        }
    }

    DisposableEffect(Unit) { onDispose { generation.incrementAndGet(); connection?.close() } }
    LaunchedEffect(transcript.size) {
        if (transcript.isNotEmpty()) listState.animateScrollToItem(transcript.lastIndex)
    }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("REALM Terminal", style = MaterialTheme.typography.headlineSmall)
        Text("Text-only worlds. Host owns state and AI characters; this phone is a client.")
        Button(onClick = { showSettings = !showSettings }) {
            Text(if (showSettings) "Hide profiles" else "Configure server profile")
        }
        if (showSettings) {
        Column(Modifier.fillMaxWidth().heightIn(max = 340.dp)
            .verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { mode = TransportMode.HTTPS_GATEWAY; port = "443" }) { Text("HTTPS (default)") }
            Button(onClick = { mode = TransportMode.LOCAL_TELNET_DEMO; port = "7777" }) { Text("Local Telnet test") }
        }
        if (mode == TransportMode.LOCAL_TELNET_DEMO) {
            Text("PLAINTEXT: loopback only (or debug emulator bridge). Cannot connect to Lappy from your phone.", color = Color(0xFFFFC278))
            Row {
                Checkbox(checked = localConsent, onCheckedChange = { localConsent = it })
                Text("I understand this sends local test commands without TLS")
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(profileName, { profileName = it }, label = { Text("Profile") }, modifier = Modifier.weight(1f))
            OutlinedTextField(port, { port = it.filter(Char::isDigit) }, label = { Text("Port") }, modifier = Modifier.weight(0.45f))
        }
        OutlinedTextField(host, { host = it }, label = { Text("Host name (not URL)") }, modifier = Modifier.fillMaxWidth())
        if (mode == TransportMode.HTTPS_GATEWAY) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(worldId, { worldId = it }, label = { Text("Expected world ID") }, modifier = Modifier.weight(1f))
                OutlinedTextField(player, { player = it }, label = { Text("Player") }, modifier = Modifier.weight(0.75f))
            }
            OutlinedTextField(token, { token = it }, label = { Text("Host-issued player token (never saved)") },
                visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                try {
                    val p = HostProfile(profileName, host, port.toIntOrNull() ?: 0, mode, worldId)
                    p.validate(BuildConfig.DEBUG)
                    if (p.mode == TransportMode.LOCAL_TELNET_DEMO && !localConsent) error("Explicit local plaintext acknowledgment required")
                    val credential = token // Grab before session teardown clears UI state.
                    disconnect()
                    transcript = emptyList(); filter = SafeAnsi()
                    activeMode = p.mode // Immutable session binding, NOT editable profile mode.
                    localHandshakeComplete = false
                    // Mask the very first local input even if the server's
                    // `Token:` prompt arrives in arbitrarily split packets.
                    awaitingLocalToken = p.mode == TransportMode.LOCAL_TELNET_DEMO
                    val current = generation.incrementAndGet()
                    val onText: (String) -> Unit = { text -> mainThread.post {
                        if (generation.get() == current) appendText(text)
                    } }
                    val onStatus: (String) -> Unit = { newStatus -> mainThread.post {
                        if (generation.get() == current) status = newStatus
                    } }
                    connection = if (p.mode == TransportMode.HTTPS_GATEWAY) {
                        HttpsGatewayConnection(p, player, credential, onText, onStatus)
                    } else {
                        LocalTelnetConnection(p, BuildConfig.DEBUG, onText, onStatus)
                    }
                    token = "" // UI no longer retains the credential.
                    connection?.start()
                    showSettings = false
                } catch (error: Exception) { status = "Connect rejected: ${error.message ?: error.javaClass.simpleName}" }
            }) { Text("Connect") }
            Button(onClick = { disconnect() }) { Text("Disconnect") }
            Button(onClick = {
                try {
                    val p = HostProfile(profileName, host, port.toIntOrNull() ?: 0, mode, worldId)
                    p.validate(false) // No debug emulator bridge profiles persisted.
                    profiles = (profiles.filterNot { it.label == p.label } + p).takeLast(20)
                    ProfileStore.save(context, profiles)
                } catch (error: Exception) { status = "Profile rejected: ${error.message}" }
            }) { Text("Save profile") }
        }
        if (profiles.isNotEmpty()) {
            Row(Modifier.horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                profiles.forEach { p ->
                    Button(onClick = {
                        profileName = p.label; host = p.host; port = p.port.toString()
                        worldId = p.worldId; mode = p.mode; localConsent = false; token = ""
                    }) { Text(p.label.take(12)) }
                }
            }
        }
        }
        }
        Text("Status: $status", modifier = Modifier.semantics { contentDescription = "Connection status: $status" })
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Text size: ${fontSize.toInt()}sp")
            Slider(value = fontSize, onValueChange = { fontSize = it }, valueRange = 12f..28f,
                modifier = Modifier.weight(1f))
        }
        Text("World transcript (read-only)")
        LazyColumn(
            state = listState,
            modifier = Modifier.weight(1f).fillMaxWidth().heightIn(min = 80.dp),
        ) {
            items(transcript) { run ->
                val fg = ansiColor(run.style.foreground)
                Text(run.text, color = fg, fontSize = fontSize.sp, fontFamily = FontFamily.Monospace,
                    fontWeight = if (run.style.bold) FontWeight.Bold else FontWeight.Normal)
            }
        }
        OutlinedTextField(
            value = command, onValueChange = { command = it.take(512) },
            label = { Text(if (awaitingLocalToken) "Local token (not saved)" else "Command") },
            visualTransformation = if (awaitingLocalToken) PasswordVisualTransformation()
                else androidx.compose.ui.text.input.VisualTransformation.None,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { send() }),
            modifier = Modifier.fillMaxWidth(),
        )
        Button(onClick = { send() }, modifier = Modifier.fillMaxWidth()) { Text("Send") }
    }
}

private fun ansiColor(code: Int?): Color = when (code) {
    30, 90 -> Color.LightGray // Never render black-on-black in the dark theme.
    31, 91 -> Color(0xFFFF7777)
    32, 92 -> Color(0xFF8FDD8F)
    33, 93 -> Color(0xFFFFD080)
    34, 94 -> Color(0xFF88AAFF)
    35, 95 -> Color(0xFFD6A5FF)
    36, 96 -> Color(0xFF80E0E0)
    else -> Color.White
}
