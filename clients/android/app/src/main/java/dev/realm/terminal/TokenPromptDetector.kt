package dev.realm.terminal

/** Bounded rolling detector; Telnet TCP packets may split login prompts anywhere. */
class TokenPromptDetector {
    private var tail = ""
    enum class Prompt { NONE, TOKEN, NAME }
    private val marker = Regex("token\\s*:|name\\s*:", RegexOption.IGNORE_CASE)

    fun scan(fragment: String): Prompt {
        tail = (tail + fragment).takeLast(32)
        val last = marker.findAll(tail).lastOrNull()?.value ?: return Prompt.NONE
        return if (last.startsWith("token", ignoreCase = true)) Prompt.TOKEN else Prompt.NAME
    }

    fun reset() { tail = "" }
}
