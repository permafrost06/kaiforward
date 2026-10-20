package dev.kaiforward.app

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject

/** Only local cutoffs are persisted; no message bodies or read state leave this device. */
class MessageReadStore(context: Context) {
    private val prefs = context.getSharedPreferences("message-read-status", Context.MODE_PRIVATE)

    fun load(): MessageReadStatus {
        val opened = JSONObject(prefs.getString("openedThrough", "{}") ?: "{}")
        return MessageReadStatus(
            if (prefs.contains("firstSyncAt")) prefs.getLong("firstSyncAt", 0) else null,
            opened.keys().asSequence().associateWith { opened.getLong(it) },
        )
    }

    fun observe(listener: SharedPreferences.OnSharedPreferenceChangeListener) =
        prefs.registerOnSharedPreferenceChangeListener(listener)

    fun stopObserving(listener: SharedPreferences.OnSharedPreferenceChangeListener) =
        prefs.unregisterOnSharedPreferenceChangeListener(listener)

    // Both the inbox and notification actions merge into the latest persisted cutoffs.
    fun update(change: (MessageReadStatus) -> MessageReadStatus): MessageReadStatus = synchronized(lock) {
        val previous = load()
        change(previous).also { if (it != previous) save(it) }
    }

    fun save(status: MessageReadStatus) {
        val baseline = status.firstSyncAt ?: return
        prefs.edit().putLong("firstSyncAt", baseline)
            .putString("openedThrough", JSONObject(status.openedThrough).toString()).apply()
    }
    companion object { private val lock = Any() }
}
