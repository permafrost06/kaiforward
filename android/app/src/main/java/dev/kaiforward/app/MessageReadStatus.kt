package dev.kaiforward.app

/** Server arrival times avoid phone clock skew and newly uploaded SMS backlog. */
data class MessageReadStatus(
    val firstSyncAt: Long? = null,
    val openedThrough: Map<String, Long> = emptyMap(),
) {
    fun initialize(syncedAt: Long) = if (firstSyncAt == null) copy(firstSyncAt = syncedAt) else this

    fun opened(conversation: String, syncedAt: Long): MessageReadStatus {
        val initialized = initialize(syncedAt)
        val cutoff = maxOf(initialized.firstSyncAt!!, initialized.openedThrough[conversation] ?: Long.MIN_VALUE)
        return if (syncedAt <= cutoff) initialized
        else initialized.copy(openedThrough = initialized.openedThrough + (conversation to syncedAt))
    }

    private fun unread(conversation: String, receivedAt: Long?): Boolean {
        val baseline = firstSyncAt ?: return false
        return receivedAt != null && receivedAt > maxOf(baseline, openedThrough[conversation] ?: Long.MIN_VALUE)
    }

    fun isUnread(conversation: Conversation) = unread(conversation.key, conversation.lastIncomingAt)
    fun isUnread(message: Sms) = message.direction == "received" && unread(conversationKey(message.peer), message.receivedAt)
}
