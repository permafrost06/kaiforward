package dev.kaiforward.app

import org.junit.Assert.*
import org.junit.Test

class MessageReadStatusTest {
    private fun incoming(arrival: Long, timestamp: Long = arrival, peer: String = "Bank") =
        Sms("$peer:$arrival", timestamp, peer, "", "received", "SMS", null, receivedAt = arrival)

    @Test fun firstSyncReadsAllExistingHistoryIncludingLaterPagesAndEmptyInbox() {
        val status = MessageReadStatus().initialize(100)
        assertFalse(status.isUnread(incoming(100)))
        assertFalse(status.isUnread(incoming(1)))
        assertTrue(status.isUnread(incoming(101)))
        assertEquals(status, status.initialize(200))
        assertTrue(MessageReadStatus().initialize(0).isUnread(incoming(1)))
    }

    @Test fun openingPersistsPerConversationAndNewArrivalsRemainUnread() {
        val status = MessageReadStatus().initialize(100).opened("bank", 200)
        val restored = MessageReadStatus(status.firstSyncAt, status.openedThrough.toMap())
        assertFalse(restored.isUnread(incoming(150)))
        assertTrue(restored.isUnread(incoming(201)))
        assertTrue(restored.isUnread(incoming(150, peer = "Other")))
        assertEquals(restored, restored.opened("bank", 125))
    }

    @Test fun arrivalTimeHandlesOldBacklogAndFutureSmsDatesAndSentMessagesAreRead() {
        val status = MessageReadStatus().initialize(100)
        assertTrue(status.isUnread(incoming(101, timestamp = 1)))
        assertFalse(status.isUnread(incoming(1, timestamp = 999999)))
        assertFalse(status.isUnread(incoming(101).copy(direction = "sent", recipient = "Bank")))
    }

    @Test fun conversationRemainsUnreadWhenLatestPreviewIsSentOrBacklogIsOlder() {
        val status = MessageReadStatus().initialize(100)
        val sent = incoming(120).copy(direction = "sent", recipient = "Bank")
        val conversation = Conversation("bank", "Bank", listOf(sent), lastIncomingAt = 110)
        assertTrue(status.isUnread(conversation))
        assertFalse(status.opened("bank", 120).isUnread(conversation))
        assertFalse(status.isUnread(conversation.copy(lastIncomingAt = null)))
    }
}
