package dev.kaiforward.app

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

internal data class UpdateState(val visible: Boolean = false, val busy: Boolean = false,
    val update: AppUpdate? = null, val ready: Boolean = false)

/** Checks and downloads are silent. Only a verified, installable APK can open the dialog. */
internal class AppUpdateController(
    private val scope: CoroutineScope,
    private val installedVersion: Int,
    private val sdk: Int,
    cached: AppUpdate? = null,
    private val fetch: suspend () -> AppUpdate,
    private val remember: (AppUpdate?) -> Unit,
    private val hasDownload: suspend (AppUpdate) -> Boolean,
    private val download: suspend (AppUpdate) -> Unit,
    private val notice: (String) -> Unit,
    private val errorText: (Exception) -> String,
) {
    private val mutable = MutableStateFlow(UpdateState(update = cached?.takeIf { it.versionCode > installedVersion }))
    val state = mutable.asStateFlow()
    private var wifi = false
    private var started = false
    private var pendingWifi = false
    private var pendingManual = false
    private var dismissedVersion: Int? = null

    fun dismiss() {
        dismissedVersion = mutable.value.update?.versionCode
        mutable.update { it.copy(visible = false) }
    }

    fun wifiChanged(available: Boolean, startDownload: Boolean = true) {
        val newlyAvailable = available && !wifi
        wifi = available
        if (startDownload && started && newlyAvailable && !mutable.value.ready) {
            if (mutable.value.busy) pendingWifi = true else resumeOnWifi()
        }
    }

    fun check(fetchLatest: Boolean, manual: Boolean) {
        started = true
        if (mutable.value.busy) {
            if (manual) pendingManual = true
            return
        }
        if (!fetchLatest && mutable.value.update == null) return
        run(manual) {
            if (fetchLatest) {
                val candidate = try { fetch().takeIf { it.versionCode > installedVersion } }
                catch (error: Exception) {
                    if (error is CancellationException) throw error
                    val cached = mutable.value.update
                    if (cached != null && cached.minSdk <= sdk && hasDownload(cached)) {
                        showReady(cached, manual)
                        return@run
                    }
                    throw error
                }
                remember(candidate)
                val unchanged = candidate == mutable.value.update
                mutable.update { it.copy(update = candidate, ready = unchanged && it.ready,
                    visible = unchanged && it.visible) }
            }
            val update = mutable.value.update
            when {
                update == null -> if (manual) notice("You’re up to date.")
                update.minSdk > sdk -> if (manual) notice("This update requires Android API ${update.minSdk} or newer.")
                else -> prepareDownload(update, manual)
            }
        }
    }

    private fun prepare(manual: Boolean) {
        val update = mutable.value.update ?: return
        if (update.minSdk > sdk || mutable.value.busy) return
        run(manual) { prepareDownload(update, manual) }
    }

    private fun resumeOnWifi() {
        if (mutable.value.update == null) check(fetchLatest = true, manual = false)
        else prepare(manual = false)
    }

    private suspend fun prepareDownload(update: AppUpdate, manual: Boolean) {
        if (!hasDownload(update)) {
            mutable.update { it.copy(ready = false, visible = false) }
            if (!wifi) {
                if (manual) notice("Update available. It will download automatically on Wi-Fi.")
                return
            }
            download(update)
        }
        showReady(update, manual)
    }

    private fun showReady(update: AppUpdate, manual: Boolean) {
        mutable.update { it.copy(ready = true, visible = manual || dismissedVersion != update.versionCode) }
    }

    private fun run(manual: Boolean, action: suspend () -> Unit) {
        mutable.update { it.copy(busy = true) }
        scope.launch {
            try { action() }
            catch (error: Exception) {
                if (error is CancellationException) throw error
                if (manual) notice(errorText(error))
            } finally {
                mutable.update { it.copy(busy = false) }
                when {
                    pendingManual -> {
                        pendingManual = false
                        pendingWifi = false
                        check(fetchLatest = true, manual = true)
                    }
                    pendingWifi -> {
                        pendingWifi = false
                        if (wifi && !mutable.value.ready) resumeOnWifi()
                    }
                }
            }
        }
    }
}
