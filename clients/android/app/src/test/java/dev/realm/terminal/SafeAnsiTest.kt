package dev.realm.terminal

import org.junit.Assert.*
import org.junit.Test

class SafeAnsiTest {
    @Test fun allowsOnlyVisualSGRAndStripsCursorCommands() {
        val parser = SafeAnsi()
        val first = parser.feed("A\u001b[31")
        val second = parser.feed("mRED\u001b[2Jhidden\u001b[0mZ")
        assertEquals("A", first.joinToString("") { it.text })
        assertEquals("REDhiddenZ", second.joinToString("") { it.text })
        assertTrue(second.first { it.text == "RED" }.style.foreground == 31)
        assertNull(second.last { it.text == "Z" }.style.foreground)
    }

    @Test fun dropsSplitOsc52ClipboardAndDcsPayloads() {
        val parser = SafeAnsi()
        val parts = listOf("hello\u001b]52;c;SECRET", "\u001b\\world\u001bPpayload", "\u001b\\done")
        val output = parts.flatMap { parser.feed(it) }.joinToString("") { it.text }
        assertEquals("helloworlddone", output)
        assertFalse(output.contains("SECRET"))
        assertFalse(output.contains("payload"))
    }

    @Test fun normalizesCrLfEvenWhenSplit() {
        val parser = SafeAnsi()
        val output = parser.feed("A\r").plus(parser.feed("\nB"))
            .joinToString("") { it.text }
        assertEquals("A\nB", output)
    }

    @Test fun oversizedAnsiControlIsDiscardedUntilItsEnd() {
        val parser = SafeAnsi()
        val output = parser.feed("before\u001b[" + "1".repeat(90) + "mAfter")
            .joinToString("") { it.text }
        assertEquals("beforeAfter", output)
    }
}
