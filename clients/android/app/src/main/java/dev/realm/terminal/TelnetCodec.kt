package dev.realm.terminal

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.CharBuffer
import java.nio.charset.CodingErrorAction

/** Minimal RFC 854/855 filter. Never negotiate options or execute controls. */
class TelnetCodec {
    data class Frame(val body: ByteArray, val refusals: ByteArray)

    private enum class State { DATA, IAC, OPTION, SUBNEGOTIATION, SUBNEG_IAC }
    private var state = State.DATA
    private var optionCommand = 0

    fun feed(bytes: ByteArray): Frame {
        val body = ByteArrayOutputStream()
        val refused = ByteArrayOutputStream()
        for (raw in bytes) {
            val byte = raw.toInt() and 0xff
            when (state) {
                State.DATA -> when (byte) {
                    255 -> state = State.IAC
                    0 -> Unit // CR NUL, padding and embedded NUL never reach text.
                    else -> body.write(byte)
                }
                State.IAC -> when (byte) {
                    251, 252, 253, 254 -> {
                        optionCommand = byte
                        state = State.OPTION
                    }
                    250 -> state = State.SUBNEGOTIATION
                    else -> state = State.DATA // Includes IAC IAC; never emit 0xff.
                }
                State.OPTION -> {
                    // WILL/WONT -> DONT; DO/DONT -> WONT.
                    val refusal = if (optionCommand == 251 || optionCommand == 252) 254 else 252
                    refused.write(byteArrayOf(255.toByte(), refusal.toByte(), byte.toByte()))
                    state = State.DATA
                }
                State.SUBNEGOTIATION -> if (byte == 255) state = State.SUBNEG_IAC
                State.SUBNEG_IAC -> state = if (byte == 240) State.DATA else State.SUBNEGOTIATION
            }
        }
        return Frame(body.toByteArray(), refused.toByteArray())
    }
}

/** Incremental UTF-8 decoder; split multibyte codepoints are not garbled. */
class Utf8Chunks {
    private val decoder = Charsets.UTF_8.newDecoder()
        .onMalformedInput(CodingErrorAction.REPLACE)
        .onUnmappableCharacter(CodingErrorAction.REPLACE)
    private var carry = ByteArray(0)

    fun feed(bytes: ByteArray): String {
        if (bytes.isEmpty()) return ""
        val merged = carry + bytes
        val input = ByteBuffer.wrap(merged)
        val output = CharBuffer.allocate(merged.size + 1)
        decoder.decode(input, output, false)
        carry = ByteArray(input.remaining())
        input.get(carry)
        // A valid UTF-8 prefix leaves at most three trailing bytes.
        check(carry.size <= 3) { "Unexpected UTF-8 decoder state" }
        output.flip()
        return output.toString()
    }
}
