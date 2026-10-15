package dev.kaiforward.app

import android.app.Activity
import android.app.Instrumentation
import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.mutableStateOf
import kotlinx.coroutines.runBlocking

/** Opt-in live-device check. Credentials arrive only through instrumentation arguments. */
class DeviceSmokeRunner : Instrumentation() {
    private var options = Bundle()

    override fun onCreate(arguments: Bundle?) {
        options = arguments ?: Bundle()
        super.onCreate(arguments)
        start()
    }

    override fun onStart() {
        if (options.getString("search_pagination") == "true") {
            checkPagination(thread = false)
            return
        }
        if (options.getString("thread_pagination") == "true") {
            checkPagination(thread = true)
            return
        }
        val result = Bundle()
        val endpoint = options.getString("receiver_url")
        val token = options.getString("read_token")
        if (endpoint == null || token == null) {
            result.putString("stream", "Skipped live receiver check: receiver_url and read_token are required.\n")
            finish(Activity.RESULT_OK, result)
            return
        }
        var stage = "connection validation"
        try {
            val connection = validateConnection(endpoint, token)
            val repository = MessageRepository(targetContext)
            runBlocking {
                stage = "newest message page"
                val first = repository.page(connection, null)
                check(first.messages.size <= 25) { "Unexpected page size" }
                var olderCount = 0
                first.cursor?.let { cursor ->
                    stage = "older message page"
                    val older = repository.page(connection, cursor)
                    check(first.messages.none { a -> older.messages.any { b -> a.key == b.key } }) { "Pagination overlap" }
                    olderCount = older.messages.size
                }
                stage = "distinct conversation pages"
                val inbox = repository.conversations(connection, null)
                check(inbox.conversations.map { it.key }.distinct().size == inbox.conversations.size) { "Repeated conversation" }
                check(inbox.conversations.zipWithNext().all { (a, b) -> a.latest.timestamp >= b.latest.timestamp }) { "Conversation order" }
                inbox.cursor?.let { cursor ->
                    val more = repository.conversations(connection, cursor)
                    check(inbox.conversations.none { a -> more.conversations.any { b -> a.key == b.key } }) { "Conversation pagination overlap" }
                }
                stage = "contact lookup"
                val names = repository.names(first.messages.map(Sms::peer))
                stage = "indexed message search"
                val found = repository.search(connection, "test", null)
                check(found.messages.size <= 25) { "Unexpected search page size" }
                found.cursor?.let { cursor ->
                    val next = repository.search(connection, "test", cursor)
                    check(found.messages.none { a -> next.messages.any { b -> a.key == b.key } }) { "Search pagination overlap" }
                }
                stage = "conversation-only pagination"
                val peer = first.messages.firstOrNull()?.peer
                var threadCount = 0
                if (peer != null) {
                    val key = conversationKey(peer)
                    val history = repository.thread(connection, key, null)
                    check(history.messages.all { conversationKey(it.peer) == key }) { "Unrelated thread message" }
                    check(history.messages.zipWithNext().all { (a, b) -> a.timestamp >= b.timestamp }) { "Thread order" }
                    threadCount = history.messages.size
                    history.cursor?.let { cursor ->
                        val older = repository.thread(connection, key, cursor)
                        check(older.messages.all { conversationKey(it.peer) == key }) { "Unrelated older message" }
                        check(history.messages.none { a -> older.messages.any { b -> a.key == b.key } }) { "Thread pagination overlap" }
                    }
                }
                stage = "encrypted connection storage"
                val store = ConnectionStore(targetContext)
                store.save(connection)
                check(store.load() == connection) { "Encrypted connection did not round-trip" }
                result.putString("stream", "Live receiver OK: ${inbox.conversations.size} distinct conversations, ${first.messages.size} newest messages, $olderCount older messages, ${found.messages.size} indexed search results, $threadCount conversation-only messages, ${names.size} contact matches. Encrypted connection restored.\n")
            }
            stage = "activity launch"
            startActivitySync(Intent(targetContext, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            waitForIdleSync()
            finish(Activity.RESULT_OK, result)
        } catch (error: Exception) {
            // Do not include request arguments, contacts, or message content in failure output.
            result.putString("stream", "Live device check failed during $stage (${error.javaClass.simpleName}).\n")
            finish(Activity.RESULT_CANCELED, result)
        }
    }

    private fun checkPagination(thread: Boolean) {
        val result = Bundle()
        var activity: MainActivity? = null
        var stage = "fixture launch"
        try {
            val testActivity = startActivitySync(Intent(targetContext, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
            activity = testActivity
            val screen = mutableStateOf(ViewerState(connected = true, cursor = "page-1", refreshedAt = 1L))
            val searchScreen = mutableStateOf(SearchState(query = "missing", cursor = "page-1", completed = true))
            val message = Sms("fixture", 1L, "12345", "", "received", "Synthetic thread message", 1)
            val conversation = conversations(listOf(message)).single()
            var requests = 0
            runOnMainSync {
                testActivity.setContent {
                    MaterialTheme {
                        val loadOlder = {
                            requests++
                            // Changing the cursor must not chain another request while idle.
                            if (thread) screen.value = screen.value.copy(cursor = "page-${requests + 1}", error = null)
                            else searchScreen.value = searchScreen.value.copy(cursor = "page-${requests + 1}", error = null)
                        }
                        if (thread) ConversationScreen(conversation, screen.value, loadOlder)
                        else SearchScreen("missing", emptyList(), screen.value, searchScreen.value, loadOlder,
                            onOpen = {}, onOpenContact = {})
                    }
                }
            }
            waitForIdleSync()
            SystemClock.sleep(700)
            stage = "idle request count"
            runOnMainSync { check(requests == 0) { "Idle view requested history" } }
            stage = "history button"
            var button = findNodeText(uiAutomation.rootInActiveWindow, if (thread) "Load older messages" else "Load more results")
                ?: error("Missing history action")
            while (!button.isClickable) button = button.parent ?: error("History action is not clickable")
            check(button.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            waitForIdleSync()
            SystemClock.sleep(700)
            stage = "single-page request count"
            runOnMainSync {
                check(requests == 1) { "A single tap chained multiple pages" }
                if (thread) screen.value = screen.value.copy(error = "Synthetic history request failure")
                else searchScreen.value = searchScreen.value.copy(error = "Synthetic search request failure")
            }
            waitForIdleSync()
            SystemClock.sleep(700)
            stage = "failed page retry"
            runOnMainSync { check(requests == 1) { "A failed page retried automatically" } }
            button = findNodeText(uiAutomation.rootInActiveWindow, if (thread) "Retry older messages" else "Retry search") ?: error("Missing retry action")
            while (!button.isClickable) button = button.parent ?: error("Retry is not clickable")
            check(button.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            waitForIdleSync()
            SystemClock.sleep(700)
            runOnMainSync {
                check(requests == 2) { "Retry did not request exactly one page" }
                if (thread) screen.value = screen.value.copy(cursor = null)
                else searchScreen.value = searchScreen.value.copy(cursor = null)
            }
            waitForIdleSync()
            SystemClock.sleep(200)
            stage = "completion label"
            check(findNodeText(uiAutomation.rootInActiveWindow, if (thread) "No older messages" else "No more results") != null)
            runOnMainSync { check(requests == 2) { "Exhausted history requested another page" } }
            result.putString("stream", "${if (thread) "Thread" else "Search"} pagination OK: idle view makes no requests, each tap loads one page, failures wait for retry, and exhausted history stops.\n")
            finish(Activity.RESULT_OK, result)
        } catch (error: Exception) {
            result.putString("stream", "${if (thread) "Thread" else "Search"} pagination check failed at $stage (${error.javaClass.simpleName}).\n")
            finish(Activity.RESULT_CANCELED, result)
        } finally {
            activity?.let { restored -> runOnMainSync { restored.recreate() } }
        }
    }

    // Compose exposes virtual accessibility children; traverse those directly.
    private fun findNodeText(node: AccessibilityNodeInfo?, text: String): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.text?.toString() == text) return node
        for (index in 0 until node.childCount) {
            findNodeText(node.getChild(index), text)?.let { return it }
        }
        return null
    }
}
