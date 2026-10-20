package dev.kaiforward.app

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class ThreadState(
    val conversation: String? = null,
    val messages: List<Sms> = emptyList(),
    val busy: Boolean = false,
    val cursor: String? = null,
    val error: String? = null,
    val completed: Boolean = false,
    val syncedAt: Long? = null,
)

/** A thread owns its history and cursor independently of the inbox and search. */
class MessageThread(
    private val scope: CoroutineScope,
    private val fetch: suspend (String, String?) -> MessagePage,
    private val errorText: (Exception) -> String = { "Unable to load this conversation. Please retry." },
    private val onOpened: (String, Long) -> Unit = { _, _ -> },
) {
    private val mutable = MutableStateFlow(ThreadState())
    val state = mutable.asStateFlow()
    private var job: Job? = null
    private var generation = 0L
    private var target: String? = null

    fun open(conversation: String?, selectedMessage: String? = null) {
        if (conversation == mutable.value.conversation && selectedMessage == target) return
        generation++
        job?.cancel()
        target = selectedMessage
        mutable.value = ThreadState(conversation = conversation)
        if (conversation != null) request(append = false)
    }

    fun refresh() {
        if (mutable.value.conversation == null || mutable.value.busy) return
        request(append = false)
    }

    fun moreOrRetry() {
        val current = mutable.value
        if (current.conversation == null || current.busy) return
        if (current.error != null) request(append = current.completed)
        else if (current.cursor != null) request(append = true)
    }

    private fun request(append: Boolean) {
        val initial = mutable.value
        val conversation = initial.conversation ?: return
        val version = generation
        mutable.value = if (append) initial.copy(busy = true, error = null) else ThreadState(conversation = conversation, busy = true)
        job?.cancel()
        job = scope.launch {
            try {
                var cursor = if (append) initial.cursor else null
                var messages = if (append) initial.messages else emptyList()
                var syncedAt = if (append) initial.syncedAt else null
                do {
                    val page = fetch(conversation, cursor)
                    ensureActive()
                    if (version != generation) return@launch
                    check(page.cursor == null || page.cursor != cursor) { "The receiver did not advance the conversation cursor." }
                    val newestPage = cursor == null
                    if (newestPage) syncedAt = page.syncedAt
                    messages = (messages + page.messages).distinctBy(Sms::key)
                    cursor = page.cursor
                    // Search targets are reached through contiguous conversation pages.
                    val seeking = target != null && messages.none { it.key == target } && cursor != null
                    mutable.value = ThreadState(conversation, messages, busy = seeking, cursor = cursor, completed = true, syncedAt = syncedAt)
                    if (newestPage && syncedAt != null) onOpened(conversation, syncedAt)
                } while (seeking)
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                if (version == generation) mutable.value = mutable.value.copy(busy = false, error = errorText(error))
            }
        }
    }
}
