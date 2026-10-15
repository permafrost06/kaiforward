package dev.kaiforward.app

import org.junit.Assert.*
import org.junit.Test

class MessagesTest {
    @Test fun shortSearchResultsDoNotFetchTheEntireHistoryWhileIdle() {
        val trigger = ScrollPageTrigger()
        repeat(10) { assertFalse(trigger.shouldLoad(scrolling = false, endVisible = true, canLoad = true)) }
    }

    @Test fun completingPagesCannotRetriggerTheSameScroll() {
        val trigger = ScrollPageTrigger()
        assertFalse(trigger.shouldLoad(scrolling = true, endVisible = false, canLoad = true))
        assertTrue(trigger.shouldLoad(scrolling = true, endVisible = true, canLoad = true))
        assertFalse(trigger.shouldLoad(scrolling = true, endVisible = true, canLoad = false))
        assertFalse(trigger.shouldLoad(scrolling = true, endVisible = true, canLoad = true))
        assertFalse(trigger.shouldLoad(scrolling = false, endVisible = true, canLoad = true))
        assertTrue(trigger.shouldLoad(scrolling = true, endVisible = true, canLoad = true))
    }

    @Test fun exhaustedOrFailedHistoryDoesNotLoadOnScroll() {
        val trigger = ScrollPageTrigger()
        assertFalse(trigger.shouldLoad(scrolling = true, endVisible = true, canLoad = false))
        assertFalse(trigger.shouldLoad(scrolling = false, endVisible = true, canLoad = false))
        assertFalse(trigger.shouldLoad(scrolling = true, endVisible = true, canLoad = false))
    }

    private fun sms(key: String, timestamp: Long, sender: String = "+880 1712-345678", recipient: String = "", direction: String = "received") =
        Sms(key, timestamp, sender, recipient, direction, "Hello", 1)

    @Test fun sentAndReceivedMessagesShareThePeerConversation() {
        val incoming = sms("1", 100)
        val outgoing = sms("2", 200, "my SIM", "+8801712345678", "sent")
        val threads = conversations(listOf(outgoing, incoming, incoming))
        assertEquals(1, threads.size)
        assertEquals(listOf(incoming, outgoing), threads.single().messages)
        assertEquals(outgoing, threads.single().latest)
    }

    @Test fun newestConversationFirstAndCountriesStaySeparate() {
        val threads = conversations(listOf(sms("1", 100, "+1 555 123"), sms("2", 300, "+44 555 123")))
        assertEquals(listOf("+44555123", "+1555123"), threads.map { it.key })
    }

    @Test fun senderIdsAndEmptyAddressesAreStable() {
        assertEquals("bkash", conversationKey(" bKash "))
        assertEquals("", conversationKey(""))
        assertNotEquals(conversationKey("Bank1"), conversationKey("1"))
    }

    @Test fun conversationPagesReplaceUpdatedPreviewsAndKeepDistinctRowsInOrder() {
        val first = Conversation("bank", "Bank", listOf(sms("1", 100, "Bank")))
        val other = Conversation("other", "Other", listOf(sms("2", 200, "Other")))
        val updated = first.copy(messages = listOf(sms("3", 300, "Bank")))
        val merged = mergeConversations(listOf(first, other), listOf(updated, other))
        assertEquals(listOf("bank", "other"), merged.map { it.key })
        assertEquals("3", merged.first().latest.key)
        assertEquals(2, merged.size)
    }

    @Test fun onlyHttpsOriginsAndHeaderSafeTokensAreAccepted() {
        assertEquals(Connection("https://example.convex.site", "secret"), validateConnection(" https://example.convex.site/ ", "secret"))
        for (endpoint in listOf("http://example.com", "https://user@example.com", "https://example.com/v1/messages", "https://example.com?q=token", "https://example.com/#x", "https:///")) {
            assertThrows(Exception::class.java) { validateConnection(endpoint, "secret") }
        }
        for (token in listOf("", "secret\ninjected", "two words")) {
            assertThrows(Exception::class.java) { validateConnection("https://example.com", token) }
        }
    }
}
