package com.friend.ios.batch

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OmiBackgroundAudioStreamerTest {
    @Test
    fun selfHostedNativeStreamRequiresExplicitOperatorOrigin() {
        assertFalse(OmiBackgroundAudioStreamer.isAllowedSelfHostedApiBase(""))
        assertFalse(OmiBackgroundAudioStreamer.isAllowedSelfHostedApiBase("https://api.omiapi.com/"))
        assertFalse(OmiBackgroundAudioStreamer.isAllowedSelfHostedApiBase("https://api.omi.me/"))
        assertFalse(OmiBackgroundAudioStreamer.isAllowedSelfHostedApiBase("http://operator.example/"))
        assertTrue(OmiBackgroundAudioStreamer.isAllowedSelfHostedApiBase("https://operator.example/"))
    }
}
