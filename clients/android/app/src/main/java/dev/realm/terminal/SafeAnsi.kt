package dev.realm.terminal

/** Visual SGR only. No cursor, link, clipboard, OSC, DCS or terminal commands. */
data class SafeStyle(val foreground: Int? = null, val bold: Boolean = false)
data class StyledText(val text: String, val style: SafeStyle)

class SafeAnsi {
    private enum class Mode { TEXT, ESC, CSI, CSI_DISCARD, OSC, OSC_ESC, DCS, DCS_ESC }
    private var mode = Mode.TEXT
    private val params = StringBuilder()
    private var current = SafeStyle()
    private var previousCR = false

    fun feed(fragment: String): List<StyledText> {
        val result = mutableListOf<StyledText>()
        val text = StringBuilder()
        fun flush() {
            if (text.isNotEmpty()) {
                result += StyledText(text.toString(), current)
                text.clear()
            }
        }

        for (ch in fragment) {
            when (mode) {
                Mode.TEXT -> when {
                    ch == '\u001b' -> { flush(); mode = Mode.ESC }
                    ch == '\r' -> { text.append('\n'); previousCR = true }
                    ch == '\n' -> { if (!previousCR) text.append('\n'); previousCR = false }
                    ch == '\t' -> { text.append('\t'); previousCR = false }
                    ch.code >= 32 && ch.code != 127 -> {
                        text.append(ch); previousCR = false
                    }
                    else -> previousCR = false
                }
                Mode.ESC -> {
                    mode = when (ch) {
                        '[' -> Mode.CSI
                        ']' -> Mode.OSC
                        'P' -> Mode.DCS
                        else -> Mode.TEXT
                    }
                    params.clear()
                }
                Mode.CSI -> {
                    if (ch.code in 0x40..0x7e) {
                        if (ch == 'm') applySgr(params.toString())
                        mode = Mode.TEXT
                        params.clear()
                    } else if (ch.code in 0x20..0x3f && params.length < 48) {
                        params.append(ch)
                    } else {
                        mode = Mode.CSI_DISCARD
                        params.clear()
                    }
                }
                Mode.CSI_DISCARD -> if (ch.code in 0x40..0x7e) mode = Mode.TEXT
                Mode.OSC -> when (ch) {
                    '\u0007' -> mode = Mode.TEXT
                    '\u001b' -> mode = Mode.OSC_ESC
                }
                Mode.OSC_ESC -> mode = if (ch == '\\') Mode.TEXT else Mode.OSC
                Mode.DCS -> if (ch == '\u001b') mode = Mode.DCS_ESC
                Mode.DCS_ESC -> mode = if (ch == '\\') Mode.TEXT else Mode.DCS
            }
        }
        flush()
        return result
    }

    private fun applySgr(raw: String) {
        if (!raw.matches(Regex("[0-9;]{0,48}"))) return
        val values = if (raw.isEmpty()) listOf(0) else raw.split(';').mapNotNull { it.toIntOrNull() }
        for (code in values) {
            current = when (code) {
                0 -> SafeStyle()
                1 -> current.copy(bold = true)
                22 -> current.copy(bold = false)
                39 -> current.copy(foreground = null)
                in 30..37, in 90..97 -> current.copy(foreground = code)
                else -> current // Ignore extended colors and unsupported effects.
            }
        }
    }
}
