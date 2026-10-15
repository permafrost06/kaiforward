package dev.kaiforward.app

data class Sms(
    val key: String,
    val timestamp: Long,
    val sender: String,
    val recipient: String,
    val direction: String,
    val body: String,
    val simSlot: Int?,
) {
    val peer: String get() = if (direction == "sent") recipient else sender
}

// Keep country prefixes: dropping them can merge unrelated contacts. Android's
// PhoneLookup handles local/international matching when resolving display names.
fun conversationKey(number: String): String {
    val trimmed = number.trim()
    if (trimmed.any { it.isLetter() }) return trimmed.lowercase(java.util.Locale.ROOT)
    return trimmed.filter { it.isDigit() || it == '+' }.ifEmpty { trimmed }
}

data class Conversation(val key: String, val number: String, val messages: List<Sms>) {
    val latest: Sms get() = messages.last()
}

fun conversations(messages: List<Sms>): List<Conversation> = messages
    .distinctBy { it.key }
    .groupBy { conversationKey(it.peer) }
    .map { (key, items) ->
        val sorted = items.sortedWith(compareBy<Sms> { it.timestamp }.thenBy { it.key })
        Conversation(key, sorted.last().peer, sorted)
    }
    .sortedByDescending { it.latest.timestamp }

/** Incoming server summaries replace earlier versions of the same conversation. */
fun mergeConversations(existing: List<Conversation>, incoming: List<Conversation>): List<Conversation> =
    (existing.associateBy { it.key } + incoming.associateBy { it.key }).values
        .sortedWith(compareByDescending<Conversation> { it.latest.timestamp }.thenByDescending { it.latest.key })

data class Connection(val endpoint: String, val token: String)

/** A visible history footer must not chain requests as each page completes. */
class ScrollPageTrigger {
    private var requestedThisScroll = false

    fun shouldLoad(scrolling: Boolean, endVisible: Boolean, canLoad: Boolean): Boolean {
        if (!scrolling) {
            requestedThisScroll = false
            return false
        }
        if (requestedThisScroll || !endVisible || !canLoad) return false
        requestedThisScroll = true
        return true
    }
}

fun validateConnection(endpoint: String, token: String): Connection {
    val uri = java.net.URI(endpoint.trim())
    require(uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.userInfo == null &&
        uri.query == null && uri.fragment == null && (uri.path.isNullOrEmpty() || uri.path == "/")) {
        "Enter the HTTPS receiver origin, such as https://your-deployment.convex.site"
    }
    require(token.isNotBlank() && token.none { it.isWhitespace() || it.code < 32 }) {
        "Enter the receiver’s read token without spaces."
    }
    return Connection(endpoint.trim().trimEnd('/'), token)
}
