package dev.kaiforward.app

import android.Manifest
import android.app.Application
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.ContactsContract
import android.os.Build
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.json.JSONArray
import java.net.HttpURLConnection
import java.net.URI
import java.util.UUID

class KaiForwardApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel("messages", "New messages", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Incoming SMS forwarded by KaiForward"
                lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            })
    }
}

object PushRegistration {
    fun enqueue(context: Context) {
        if (!BuildConfig.PUSH_CONFIGURED) return
        WorkManager.getInstance(context).enqueueUniqueWork("push-registration", ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<PushRegistrationWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build())
    }
}

class PushRegistrationWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        if (!BuildConfig.PUSH_CONFIGURED) return Result.success()
        return try {
            val token = FirebaseMessaging.getInstance().token.await()
            withContext(Dispatchers.IO) {
                val prefs = applicationContext.getSharedPreferences("push", Context.MODE_PRIVATE)
                val installation = prefs.getString("installation", null) ?: UUID.randomUUID().toString().also {
                    check(prefs.edit().putString("installation", it).commit())
                }
                val connection = URI(BuildConfig.RECEIVER_URL + "/v1/push/devices").toURL().openConnection() as HttpURLConnection
                try {
                    connection.requestMethod = "POST"
                    connection.connectTimeout = 15_000
                    connection.readTimeout = 15_000
                    connection.instanceFollowRedirects = false
                    connection.setRequestProperty("Authorization", "Bearer ${BuildConfig.APP_TOKEN}")
                    connection.setRequestProperty("Content-Type", "application/json")
                    connection.doOutput = true
                    connection.outputStream.use { it.write(JSONObject().put("installation", installation).put("token", token).toString().toByteArray(Charsets.UTF_8)) }
                    when (connection.responseCode) {
                        in 200..299 -> Result.success()
                        // A receiver deployment or configuration fix can resolve these later.
                        else -> if (runAttemptCount < 10) Result.retry() else Result.failure()
                    }
                } finally { connection.disconnect() }
            }
        } catch (error: Exception) {
            if (error is CancellationException) throw error
            if (runAttemptCount < 10) Result.retry() else Result.failure()
        }
    }
}

class MessagePushService : FirebaseMessagingService() {
    override fun onNewToken(token: String) { PushRegistration.enqueue(this) }

    override fun onMessageReceived(message: RemoteMessage) {
        // Data messages use the same local notification path in foreground and background.
        val sender = message.data["sender"] ?: return
        val body = message.data["body"] ?: return
        val conversation = message.data["conversation"] ?: return
        val messageKey = message.data["messageKey"] ?: return
        val prefs = getSharedPreferences("push", Context.MODE_PRIVATE)
        val seen = JSONArray(prefs.getString("seenMessages", "[]"))
        if ((0 until seen.length()).any { seen.optString(it) == messageKey }) return
        var title = sender.ifBlank { "Unknown sender" }
        if (checkSelfPermission(Manifest.permission.READ_CONTACTS) == PackageManager.PERMISSION_GRANTED) {
            try {
                val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(sender))
                contentResolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)?.use {
                    if (it.moveToFirst()) title = it.getString(0) ?: title
                }
            } catch (_: SecurityException) { /* Permission may have been revoked. */ }
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
                .setData(Uri.parse("kaiforward://conversation/" + Uri.encode(conversation)))
                .putExtra("conversation", conversation).putExtra("sender", sender).putExtra("messageKey", messageKey),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, "messages")
            .setSmallIcon(R.drawable.ic_notification).setContentTitle(title)
            .setContentText(body).setStyle(Notification.BigTextStyle().bigText(body))
            .setCategory(Notification.CATEGORY_MESSAGE).setOnlyAlertOnce(false)
            .setContentIntent(open).setAutoCancel(true).setVisibility(Notification.VISIBILITY_PRIVATE).build()
        getSystemService(NotificationManager::class.java).notify(conversation, 0, notification)
        val recent = JSONArray()
        for (index in maxOf(0, seen.length() - 99) until seen.length()) recent.put(seen.getString(index))
        recent.put(messageKey)
        prefs.edit().putString("seenMessages", recent.toString()).apply()
    }
}
