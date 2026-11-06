package dev.realm.terminal

import org.junit.Assert.*
import org.junit.Test

class TelnetCodecTest {
    @Test fun refusesFragmentedOptionsAndKeepsText() {
        val codec = TelnetCodec()
        val first = codec.feed(byteArrayOf('H'.code.toByte(), 'i'.code.toByte(), 255.toByte(), 251.toByte()))
        assertEquals("Hi", first.body.toString(Charsets.UTF_8))
        assertEquals(0, first.refusals.size)
        val second = codec.feed(byteArrayOf(1, '!'.code.toByte()))
        assertArrayEquals(byteArrayOf(255.toByte(), 254.toByte(), 1), second.refusals)
        assertEquals("!", second.body.toString(Charsets.UTF_8))
    }

    @Test fun ignoresSubnegotiationOsc52LikeDataAndNegotiatesDont() {
        val codec = TelnetCodec()
        assertTrue(codec.feed(byteArrayOf(255.toByte(), 250.toByte(), 201.toByte(), 'S'.code.toByte())).body.isEmpty())
        val frame = codec.feed(byteArrayOf(255.toByte(), 240.toByte(), 'Y'.code.toByte(), 255.toByte(), 253.toByte(), 31))
        assertEquals("Y", frame.body.toString(Charsets.UTF_8))
        assertArrayEquals(byteArrayOf(255.toByte(), 252.toByte(), 31), frame.refusals)
    }

    @Test fun utf8CodepointSurvivesSplitNetworkChunks() {
        val input = "hé 🐉".toByteArray(Charsets.UTF_8)
        val codec = Utf8Chunks()
        val builder = StringBuilder()
        for (byte in input) builder.append(codec.feed(byteArrayOf(byte)))
        assertEquals("hé 🐉", builder.toString())
    }
}
