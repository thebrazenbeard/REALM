package dev.realm.terminal

import org.junit.Assert.*
import org.junit.Test

class TokenPromptDetectorTest {
    @Test fun fragmentsAndCaseVariationsNeverBypassMaskDetection() {
        val detector = TokenPromptDetector()
        assertEquals(TokenPromptDetector.Prompt.NONE, detector.scan("REALM\r\nTok"))
        assertEquals(TokenPromptDetector.Prompt.TOKEN, detector.scan("en: "))
        detector.reset()
        assertEquals(TokenPromptDetector.Prompt.NONE, detector.scan("token"))
        assertEquals(TokenPromptDetector.Prompt.TOKEN, detector.scan(":"))
        detector.reset()
        assertEquals(TokenPromptDetector.Prompt.TOKEN, detector.scan("TOKEN : "))
        assertEquals(TokenPromptDetector.Prompt.TOKEN, detector.scan("Na"))
        assertEquals(TokenPromptDetector.Prompt.NAME, detector.scan("me: "))
        detector.reset()
        assertEquals(TokenPromptDetector.Prompt.NONE, detector.scan("nA"))
        assertEquals(TokenPromptDetector.Prompt.NAME, detector.scan("mE :"))
    }
}
