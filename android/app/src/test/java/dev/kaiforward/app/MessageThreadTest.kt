package dev.kaiforward.app

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import org.junit.Assert.*
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class MessageThreadTest {
    private fun sms(key: String) = Sms(key, 1L, "123", "", "received", key, null)

    @Test fun openingAndPaginationUseOnlyTheSelectedConversationAndStopAtEnd() = runTest {
        val calls = mutableListOf<Pair<String, String?>>()
        val thread = MessageThread(this, fetch = { peer, cursor ->
            calls += peer to cursor
            if (cursor == null) MessagePage(listOf(sms("new")), "older") else MessagePage(listOf(sms("old")), null)
        })
        thread.open("123")
        advanceUntilIdle()
        assertEquals(listOf("123" to null), calls)
        thread.moreOrRetry(); thread.moreOrRetry()
        advanceUntilIdle()
        assertEquals(listOf("123" to null, "123" to "older"), calls)
        assertEquals(listOf("new", "old"), thread.state.value.messages.map(Sms::key))
        thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(2, calls.size)
    }

    @Test fun switchingAndClosingThreadsRejectStaleResponses() = runTest {
        val thread = MessageThread(this, fetch = { peer, _ ->
            withContext(NonCancellable) { delay(if (peer == "old") 1000 else 1) }
            MessagePage(listOf(sms(peer)), null)
        })
        thread.open("old"); advanceTimeBy(10)
        thread.open("new"); advanceUntilIdle()
        assertEquals("new", thread.state.value.messages.single().key)
        thread.open(null); advanceUntilIdle()
        assertEquals(ThreadState(), thread.state.value)
    }

    @Test fun failedInitialAndOlderPagesWaitForExplicitRetry() = runTest {
        var fail = true
        val cursors = mutableListOf<String?>()
        val thread = MessageThread(this, fetch = { _, cursor ->
            cursors += cursor
            if (fail) error("Offline")
            MessagePage(listOf(sms(cursor ?: "new")), if (cursor == null) "older" else null)
        })
        thread.open("123"); advanceUntilIdle()
        assertNotNull(thread.state.value.error)
        assertEquals(1, cursors.size)
        fail = false; thread.moreOrRetry(); advanceUntilIdle()
        fail = true; thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(listOf("new"), thread.state.value.messages.map(Sms::key))
        fail = false; thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(listOf(null, null, "older", "older"), cursors)
        assertNull(thread.state.value.error)
    }

    @Test fun searchTargetLoadsContiguousThreadPagesAndResumesAfterFailure() = runTest {
        var fail = true
        val cursors = mutableListOf<String?>()
        val thread = MessageThread(this, fetch = { _, cursor ->
            cursors += cursor
            if (cursor == "older" && fail) error("Offline")
            if (cursor == null) MessagePage(listOf(sms("new")), "older") else MessagePage(listOf(sms("target")), "more")
        })
        thread.open("123", "target"); advanceUntilIdle()
        assertEquals(listOf(null, "older"), cursors)
        assertNotNull(thread.state.value.error)
        fail = false; thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(listOf("new", "target"), thread.state.value.messages.map(Sms::key))
        assertEquals("more", thread.state.value.cursor)
        assertFalse(thread.state.value.busy)
    }

    @Test fun failedRefreshRetriesFromTheNewestPageAndEmptyThreadsAreTerminal() = runTest {
        var fail = false
        val cursors = mutableListOf<String?>()
        val thread = MessageThread(this, fetch = { _, cursor ->
            cursors += cursor
            if (fail) error("Offline")
            MessagePage(emptyList(), null)
        })
        thread.open("123"); advanceUntilIdle()
        assertTrue(thread.state.value.completed)
        thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(1, cursors.size)
        fail = true; thread.refresh(); advanceUntilIdle()
        assertNotNull(thread.state.value.error)
        fail = false; thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(listOf(null, null, null), cursors)
        assertNull(thread.state.value.error)
    }

    @Test fun refreshReplacesHistoryAndRepeatedCursorsStopWithAnError() = runTest {
        var calls = 0
        val thread = MessageThread(this, fetch = { _, _ -> calls++; MessagePage(listOf(sms("$calls")), "same") })
        thread.open("123"); advanceUntilIdle()
        thread.moreOrRetry(); advanceUntilIdle()
        assertNotNull(thread.state.value.error)
        assertEquals(2, calls)
        thread.refresh(); advanceUntilIdle()
        assertNull(thread.state.value.error)
        assertEquals(listOf("3"), thread.state.value.messages.map(Sms::key))
    }
    @Test fun onlyAcceptedNewestPagesAdvanceReadCutoff() = runTest {
        val opened = mutableListOf<Pair<String, Long>>()
        var snapshot = 100L
        var fail = false
        val thread = MessageThread(this, fetch = { _, cursor ->
            if (fail) error("Offline")
            MessagePage(listOf(sms(cursor ?: "new")), if (cursor == null) "older" else null, snapshot++)
        }, onOpened = { key, at -> opened += key to at })
        thread.open("123"); advanceUntilIdle()
        assertEquals(listOf("123" to 100L), opened)
        thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(listOf("123" to 100L), opened)
        assertEquals(100L, thread.state.value.syncedAt)
        fail = true; thread.refresh(); advanceUntilIdle()
        assertEquals(1, opened.size)
        fail = false; thread.moreOrRetry(); advanceUntilIdle()
        assertEquals(listOf("123" to 100L, "123" to 102L), opened)
    }

    @Test fun stalePagesCannotMarkAnotherConversationRead() = runTest {
        val opened = mutableListOf<String>()
        val thread = MessageThread(this, fetch = { peer, _ ->
            withContext(NonCancellable) { delay(if (peer == "old") 1000 else 1) }
            MessagePage(listOf(sms(peer)), null, 100L)
        }, onOpened = { key, _ -> opened += key })
        thread.open("old"); advanceTimeBy(10)
        thread.open("new"); advanceUntilIdle()
        assertEquals(listOf("new"), opened)
    }

}
