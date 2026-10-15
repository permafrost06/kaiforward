package dev.kaiforward.app

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class ViewerState(
    val connected: Boolean = false,
    val endpoint: String = "",
    val conversations: List<Conversation> = emptyList(),
    val names: Map<String, String> = emptyMap(),
    val contactsAllowed: Boolean = false,
    val busy: Boolean = false,
    val cursor: String? = null,
    val error: String? = null,
    val refreshedAt: Long? = null,
)

class MessagesViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = MessageRepository(application)
    private val store = ConnectionStore(application)
    private var connection = store.load()
    private var loadJob: Job? = null
    private var contactsJob: Job? = null
    private val mutable = MutableStateFlow(ViewerState(connected = connection != null,
        endpoint = connection?.endpoint.orEmpty(), contactsAllowed = repository.hasContactsPermission()))
    val state = mutable.asStateFlow()
    private val search = MessageSearch(viewModelScope,
        fetch = { query, cursor -> repository.search(connection ?: error("Connect to the receiver first."), query, cursor) },
        names = repository::names, errorText = ::errorText)
    val searchState = search.state
    private val thread = MessageThread(viewModelScope,
        fetch = { key, cursor -> repository.thread(connection ?: error("Connect to the receiver first."), key, cursor) },
        errorText = ::errorText)
    val threadState = thread.state
    fun openThread(key: String?, selectedMessage: String?) = thread.open(key, selectedMessage)
    fun moreThreadMessages() = thread.moreOrRetry()
    fun refreshThread() = thread.refresh()

    fun search(query: String) = search.query(query)
    fun moreSearchResults() = search.moreOrRetry()
    init { if (connection != null) refresh() }

    fun connect(endpoint: String, token: String) {
        if (mutable.value.busy) return
        val candidate = try { validateConnection(endpoint, token) } catch (_: Exception) {
            mutable.update { it.copy(error = "Enter a valid HTTPS receiver origin and read token (without spaces).") }
            return
        }
        loadJob = viewModelScope.launch {
            mutable.update { it.copy(busy = true, error = null) }
            try {
                val page = repository.conversations(candidate, null)
                withContext(Dispatchers.IO) { store.save(candidate) }
                connection = candidate
                mutable.value = ViewerState(true, candidate.endpoint, page.conversations,
                    contactsAllowed = repository.hasContactsPermission(), cursor = page.cursor, refreshedAt = System.currentTimeMillis())
                resolveContacts()
            } catch (error: Exception) { failure(error) }
            finally { mutable.update { it.copy(busy = false) } }
        }
    }

    fun refresh() = load(false)
    fun loadOlder() {
        val current = mutable.value
        if (current.cursor != null || current.error != null) load(current.cursor != null)
    }
    private fun load(older: Boolean) {
        val current = connection ?: return
        if (mutable.value.busy) return
        loadJob = viewModelScope.launch {
            mutable.update { it.copy(busy = true, error = null) }
            try {
                val page = repository.conversations(current, if (older) mutable.value.cursor else null)
                mutable.update {
                    // Refresh restarts both the conversation list and its pagination chain.
                    it.copy(conversations = if (older) mergeConversations(it.conversations, page.conversations) else page.conversations, cursor = page.cursor,
                        refreshedAt = System.currentTimeMillis())
                }
                resolveContacts()
            } catch (error: Exception) { failure(error) }
            finally { mutable.update { it.copy(busy = false) } }
        }
    }
    fun resolveContacts() {
        contactsJob?.cancel()
        val allowed = repository.hasContactsPermission()
        mutable.update { it.copy(contactsAllowed = allowed, names = if (allowed) it.names else emptyMap()) }
        contactsJob = viewModelScope.launch {
            try {
                val names = repository.names(mutable.value.conversations.map(Conversation::number))
                mutable.update { it.copy(names = names) }
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                mutable.update { it.copy(names = emptyMap()) }
            }
        }
    }
    fun disconnect() {
        search.query("")
        thread.open(null)
        loadJob?.cancel()
        contactsJob?.cancel()
        store.clear()
        connection = null
        mutable.value = ViewerState(contactsAllowed = repository.hasContactsPermission())
    }
    fun clearError() { mutable.update { it.copy(error = null) } }
    private fun failure(error: Exception) {
        if (error is CancellationException) throw error
        mutable.update { it.copy(error = errorText(error)) }
    }

    private fun errorText(error: Exception): String = when (error) {
            is java.net.UnknownHostException -> "Cannot reach the receiver. Check the address and your internet connection."
            is java.net.SocketTimeoutException -> "The receiver took too long to respond. Try again."
            is javax.net.ssl.SSLException -> "A secure connection could not be established. Check the receiver’s HTTPS certificate."
            is java.io.IOException -> "Connection interrupted. Check your internet connection and try again."
            is IllegalStateException -> error.message ?: "Unable to load messages."
            else -> "Unable to read the receiver response. Check that the updated receiver is deployed."
    }
}
