package dev.kaiforward.app

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class SearchState(
    val query: String = "",
    val messages: List<Sms> = emptyList(),
    val names: Map<String, String> = emptyMap(),
    val busy: Boolean = false,
    val cursor: String? = null,
    val error: String? = null,
    val completed: Boolean = false,
)

/** Search owns its requests and cursor; it never advances the inbox's history. */
class MessageSearch(
    private val scope: CoroutineScope,
    private val fetch: suspend (String, String?) -> MessagePage,
    private val names: suspend (List<String>) -> Map<String, String> = { emptyMap() },
    private val errorText: (Exception) -> String = { "Search failed. Please retry." },
) {
    private val mutable = MutableStateFlow(SearchState())
    val state = mutable.asStateFlow()
    private var job: Job? = null
    private var generation = 0L

    fun query(raw: String) {
        val query = raw.trim()
        if (query == mutable.value.query) return
        generation++
        job?.cancel()
        mutable.value = SearchState(query = query, busy = query.isNotEmpty())
        if (query.isNotEmpty()) request(append = false, debounce = true)
    }

    fun moreOrRetry() {
        val current = mutable.value
        if (current.busy || current.query.isBlank()) return
        if (current.error != null) request(append = current.completed)
        else if (current.cursor != null) request(append = true)
    }

    private fun request(append: Boolean, debounce: Boolean = false) {
        val current = mutable.value
        val version = generation
        val cursor = if (append) current.cursor else null
        mutable.value = current.copy(busy = true, error = null)
        job?.cancel()
        job = scope.launch {
            try {
                if (debounce) delay(350)
                val page = fetch(current.query, cursor)
                ensureActive()
                if (version != generation) return@launch
                val messages = (if (append) current.messages + page.messages else page.messages).distinctBy(Sms::key)
                val contacts = try { names(messages.map(Sms::peer)) }
                    catch (error: CancellationException) { throw error }
                    catch (_: Exception) { emptyMap() }
                ensureActive()
                if (version == generation) mutable.value = SearchState(current.query, messages, contacts,
                    cursor = page.cursor, completed = true)
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                if (version == generation) mutable.value = current.copy(busy = false, error = errorText(error))
            }
        }
    }
}
