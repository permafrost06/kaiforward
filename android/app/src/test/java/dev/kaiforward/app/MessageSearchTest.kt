package dev.kaiforward.app

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import org.junit.Assert.*
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class MessageSearchTest {
    private fun message(id: String) = Sms(id, 1L, "12345", "", "received", "Match $id", null)

    @Test fun typingIsDebouncedAndOnlyTheLatestQueryIsRequested() = runTest {
        val calls = mutableListOf<String>()
        val search = MessageSearch(this, fetch = { query, _ -> calls += query; MessagePage(listOf(message(query)), null) })
        search.query("one")
        advanceTimeBy(200)
        search.query("two")
        advanceTimeBy(349)
        runCurrent()
        assertTrue(calls.isEmpty())
        advanceUntilIdle()
        assertEquals(listOf("two"), calls)
        assertEquals("two", search.state.value.messages.single().key)
    }

    @Test fun staleResponsesCannotReplaceANewerQuery() = runTest {
        val search = MessageSearch(this, fetch = { query, _ ->
            // Model a blocking transport that finishes even after cancellation.
            withContext(NonCancellable) { delay(if (query == "old") 1000 else 1) }
            MessagePage(listOf(message(query)), null)
        })
        search.query("old")
        advanceTimeBy(400)
        search.query("new")
        advanceUntilIdle()
        assertEquals("new", search.state.value.query)
        assertEquals(listOf("new"), search.state.value.messages.map(Sms::key))
        assertFalse(search.state.value.busy)
    }

    @Test fun paginationUsesTheSearchCursorAndStopsAtTheLastPage() = runTest {
        val cursors = mutableListOf<String?>()
        val search = MessageSearch(this, fetch = { _, cursor ->
            cursors += cursor
            if (cursor == null) MessagePage(listOf(message("1")), "next")
            else MessagePage(listOf(message("1"), message("2")), null)
        })
        search.query("term")
        advanceUntilIdle()
        assertEquals(1, cursors.size) // A cursor never fetches the next page by itself.
        search.moreOrRetry()
        search.moreOrRetry() // Busy requests cannot overlap.
        advanceUntilIdle()
        assertEquals(listOf(null, "next"), cursors)
        assertEquals(listOf("1", "2"), search.state.value.messages.map(Sms::key))
        search.moreOrRetry()
        advanceUntilIdle()
        assertEquals(2, cursors.size)
    }

    @Test fun errorsKeepPriorResultsAndRetryTheFailedPageOnlyOnRequest() = runTest {
        var fail = true
        val calls = mutableListOf<String?>()
        val search = MessageSearch(this, fetch = { _, cursor ->
            calls += cursor
            if (cursor == null) MessagePage(listOf(message("1")), "next")
            else if (fail) error("Temporary failure")
            else MessagePage(listOf(message("2")), null)
        })
        search.query("term")
        advanceUntilIdle()
        search.moreOrRetry()
        advanceUntilIdle()
        assertNotNull(search.state.value.error)
        assertEquals(listOf("1"), search.state.value.messages.map(Sms::key))
        assertEquals(listOf(null, "next"), calls)
        fail = false
        search.moreOrRetry()
        advanceUntilIdle()
        assertEquals(listOf(null, "next", "next"), calls)
        assertNull(search.state.value.error)
        assertEquals(2, search.state.value.messages.size)
    }

    @Test fun emptyResultsCompleteAndClearingSearchCancelsPendingWork() = runTest {
        var calls = 0
        val search = MessageSearch(this, fetch = { _, _ -> calls++; MessagePage(emptyList(), null) })
        search.query("missing")
        advanceUntilIdle()
        assertTrue(search.state.value.completed)
        assertFalse(search.state.value.busy)
        search.query("pending")
        search.query("  ")
        advanceUntilIdle()
        assertEquals(1, calls)
        assertEquals(SearchState(), search.state.value)
    }
}
