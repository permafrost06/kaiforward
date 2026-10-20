package dev.kaiforward.app

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.ContactsContract
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

data class MessagePage(val messages: List<Sms>, val cursor: String?, val syncedAt: Long? = null)
data class ConversationPage(val conversations: List<Conversation>, val cursor: String?, val syncedAt: Long? = null)

class MessageRepository(private val context: Context) {
    suspend fun page(connection: Connection, cursor: String?): MessagePage {
        val suffix = cursor?.let { "?cursor=" + Uri.encode(it) } ?: ""
        return request(connection, "/v1/messages$suffix")
    }

    suspend fun search(connection: Connection, query: String, cursor: String?): MessagePage = request(connection,
        "/v1/messages/search", JSONObject().put("query", query).put("cursor", cursor ?: JSONObject.NULL).toString())

    suspend fun thread(connection: Connection, conversation: String, cursor: String?): MessagePage = request(connection,
        "/v1/messages/thread", JSONObject().put("conversation", conversation).put("cursor", cursor ?: JSONObject.NULL).toString())

    suspend fun conversations(connection: Connection, cursor: String?): ConversationPage {
        val suffix = cursor?.let { "?cursor=" + Uri.encode(it) } ?: ""
        val json = requestJson(connection, "/v1/conversations$suffix")
        val items = json.getJSONArray("conversations")
        return ConversationPage((0 until items.length()).map { index ->
            val item = items.getJSONObject(index)
            Conversation(item.getString("key"), item.getString("number"), listOf(sms(item.getJSONObject("lastMessage"))),
                if (item.isNull("lastIncomingAt")) null else item.getLong("lastIncomingAt"))
        }, if (json.isNull("cursor")) null else json.getString("cursor"),
            if (json.has("syncedAt")) json.getLong("syncedAt") else null)
    }

    private fun sms(item: JSONObject) = Sms(item.getString("key"), item.getLong("timestamp"), item.getString("sender"),
        item.getString("recipient"), item.getString("direction"), item.getString("body"),
        if (item.has("simSlot") && !item.isNull("simSlot")) item.getInt("simSlot") else null,
        if (item.has("receivedAt")) item.getLong("receivedAt") else item.getLong("timestamp"))

    private suspend fun request(connection: Connection, path: String, body: String? = null): MessagePage {
        val json = requestJson(connection, path, body)
        val items = json.getJSONArray("messages")
        return MessagePage((0 until items.length()).map { sms(items.getJSONObject(it)) },
            if (json.isNull("cursor")) null else json.getString("cursor"),
            if (json.has("syncedAt")) json.getLong("syncedAt") else null)
    }

    private suspend fun requestJson(connection: Connection, path: String, body: String? = null): JSONObject = withContext(Dispatchers.IO) {
        val request = URL("${connection.endpoint}$path").openConnection() as HttpURLConnection
        try {
            request.connectTimeout = 15_000
            request.readTimeout = 20_000
            request.instanceFollowRedirects = false
            request.setRequestProperty("Authorization", "Bearer ${connection.token}")
            request.setRequestProperty("Accept", "application/json")
            if (body != null) {
                val bytes = body.toByteArray(Charsets.UTF_8)
                request.requestMethod = "POST"
                request.doOutput = true
                request.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                request.setFixedLengthStreamingMode(bytes.size)
                request.outputStream.use { it.write(bytes) }
            }
            when (request.responseCode) {
                200 -> Unit
                400 -> error("Invalid request or history cursor. Refresh this view and try again.")
                401, 403 -> error("Access denied. The app credential must match the receiver’s read token.")
                404 -> error("Message endpoint not found. Deploy the updated Convex receiver.")
                503 -> error("Message viewing is not configured. Set KAIFORWARD_READ_TOKEN on the receiver.")
                else -> error("Receiver returned HTTP ${request.responseCode}. Try again shortly.")
            }
            JSONObject(request.inputStream.bufferedReader().use { it.readText() })
        } finally { request.disconnect() }
    }

    fun hasContactsPermission() = context.checkSelfPermission(Manifest.permission.READ_CONTACTS) == PackageManager.PERMISSION_GRANTED

    suspend fun names(numbers: List<String>): Map<String, String> = withContext(Dispatchers.IO) {
        if (!hasContactsPermission()) return@withContext emptyMap()
        buildMap {
            for (number in numbers.distinct()) {
                // Alphanumeric SMS sender IDs aren't telephone contacts.
                if (number.none { it.isDigit() } || number.any { it.isLetter() }) continue
                val name = try {
                    val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number))
                    context.contentResolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)?.use {
                        if (it.moveToFirst()) it.getString(0) else null
                    }
                } catch (_: SecurityException) { null }
                if (!name.isNullOrBlank()) put(conversationKey(number), name)
            }
        }
    }
}
