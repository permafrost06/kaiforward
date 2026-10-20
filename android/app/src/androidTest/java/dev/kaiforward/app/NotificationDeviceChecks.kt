package dev.kaiforward.app

import android.Manifest
import android.app.Activity
import android.app.Instrumentation
import android.app.NotificationManager
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import java.util.UUID

/** Synthetic checks exercise the real notification manager without contacting the receiver. */
object NotificationDeviceChecks {
    fun run(runner: Instrumentation) {
        val result = Bundle()
        val prefix = "notification-test-${UUID.randomUUID()}"
        val context = object : ContextWrapper(runner.targetContext) {
            override fun getSharedPreferences(name: String, mode: Int) =
                super.getSharedPreferences("$prefix-$name", mode)
        }
        val manager = context.getSystemService(NotificationManager::class.java)
        val keys = (1..4).map { "$prefix-$it" }
        fun active() = manager.activeNotifications.filter { it.tag in keys.map { key -> "message:$key" } }
        fun awaitCount(count: Int) {
            val deadline = SystemClock.elapsedRealtime() + 3_000
            while (active().size != count && SystemClock.elapsedRealtime() < deadline) SystemClock.sleep(50)
            check(active().size == count) { "Expected $count separate notifications" }
        }
        try {
            check(Build.VERSION.SDK_INT < 33 || context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
                PackageManager.PERMISSION_GRANTED) { "Grant notification permission before running this check" }
            check(manager.areNotificationsEnabled()) { "Enable app notifications before running this check" }
            MessageReadStore(context).save(MessageReadStatus(firstSyncAt = 0))
            val payloads = keys.mapIndexed { index, key -> mapOf(
                "sender" to "KaiForward test", "body" to "Synthetic notification ${index + 1}",
                "conversation" to if (index == 3) "$prefix-other" else prefix,
                "messageKey" to key, "receivedAt" to (index + 1).toString(),
            ) }
            payloads.forEach { context.showMessageNotification(it) }
            awaitCount(4)
            val notifications = active().associateBy { it.tag }
            check(notifications.values.map { it.notification.contentIntent }.distinct().size == 4) {
                "Message tap actions share an identity"
            }
            check(notifications.values.map { it.notification.actions.single().actionIntent }.distinct().size == 4) {
                "Mark-read actions share an identity"
            }
            context.showMessageNotification(payloads.first())
            awaitCount(4)
            MarkMessageReadReceiver().onReceive(context, Intent(MarkMessageReadReceiver.ACTION)
                .putExtra("conversation", prefix).putExtra("receivedAt", 2L))
            awaitCount(2)
            check(active().map { it.tag }.toSet() == setOf("message:${keys[2]}", "message:${keys[3]}")) {
                "Mark-read dismissed a newer or unrelated notification"
            }
            check(MessageReadStore(context).load().openedThrough[prefix] == 2L)
            result.putString("stream", "Notifications OK: alerts coexist, actions stay distinct, replays are suppressed, and mark-read preserves newer/unrelated alerts.\n")
            runner.finish(Activity.RESULT_OK, result)
        } catch (error: Exception) {
            result.putString("stream", "Notification check failed: ${error.message}\n")
            runner.finish(Activity.RESULT_CANCELED, result)
        } finally {
            keys.forEach { manager.cancel("message:$it", 0) }
            context.deleteSharedPreferences("$prefix-push")
            context.deleteSharedPreferences("$prefix-message-read-status")
        }
    }
}
