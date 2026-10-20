package dev.kaiforward.app

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.*
import org.junit.Test
import java.io.IOException

@OptIn(ExperimentalCoroutinesApi::class)
class AppUpdateControllerTest {
    private val update = AppUpdate(15, "0.7.3", "https://kaiforward.permafrost06.com/updates/app.apk".toHttpUrl(),
        "0".repeat(64), 10, 26, "A release")

    private inner class Fixture(scope: CoroutineScope, cached: AppUpdate? = null) {
        val notices = mutableListOf<String>()
        var fetches = 0
        var downloads = 0
        var saved: AppUpdate? = cached
        var latest = update
        var downloaded = false
        var fetchFailure: Exception? = null
        var fetchGate: CompletableDeferred<Unit>? = null
        var downloadGate: CompletableDeferred<Unit>? = null
        val controller = AppUpdateController(scope, 14, 35, cached,
            fetch = { fetches++; fetchGate?.await(); fetchFailure?.let { throw it }; latest },
            remember = { saved = it }, hasDownload = { downloaded },
            download = { downloads++; downloadGate?.await(); downloaded = true },
            notice = notices::add, errorText = { "Failed: ${it.message}" })
    }

    @Test fun manualCheckWithNoUpdateOnlyShowsToast() = runTest {
        val fixture = Fixture(this).apply { latest = update.copy(versionCode = 14) }
        fixture.controller.check(fetchLatest = true, manual = true)
        assertFalse(fixture.controller.state.value.visible)
        advanceUntilIdle()
        assertEquals(listOf("You’re up to date."), fixture.notices)
        assertFalse(fixture.controller.state.value.visible)
        assertEquals(0, fixture.downloads)
    }

    @Test fun checkAndWifiDownloadRemainSilentUntilDownloadIsVerified() = runTest {
        val fixture = Fixture(this).apply { downloadGate = CompletableDeferred() }
        fixture.controller.wifiChanged(true)
        fixture.controller.check(fetchLatest = true, manual = true)
        runCurrent()
        assertTrue(fixture.controller.state.value.busy)
        assertFalse(fixture.controller.state.value.visible)
        assertFalse(fixture.controller.state.value.ready)
        fixture.downloadGate!!.complete(Unit)
        advanceUntilIdle()
        assertTrue(fixture.controller.state.value.ready)
        assertTrue(fixture.controller.state.value.visible)
        assertEquals(1, fixture.downloads)
        assertTrue(fixture.notices.isEmpty())
    }

    @Test fun cellularCheckWaitsAndWifiArrivalDownloadsWithoutAnotherCheckOrTap() = runTest {
        val fixture = Fixture(this)
        fixture.controller.check(fetchLatest = true, manual = true)
        advanceUntilIdle()
        assertEquals(0, fixture.downloads)
        assertFalse(fixture.controller.state.value.visible)
        assertEquals(listOf("Update available. It will download automatically on Wi-Fi."), fixture.notices)
        fixture.controller.wifiChanged(true)
        advanceUntilIdle()
        assertEquals(1, fixture.fetches)
        assertEquals(1, fixture.downloads)
        assertTrue(fixture.controller.state.value.visible)
    }

    @Test fun failedDownloadNeverPromptsAndRestartsWhenWifiReturns() = runTest {
        val fixture = Fixture(this).apply { downloadGate = CompletableDeferred() }
        fixture.controller.wifiChanged(true)
        fixture.controller.check(fetchLatest = true, manual = false)
        runCurrent()
        fixture.controller.wifiChanged(false)
        fixture.downloadGate!!.completeExceptionally(IOException("Wi-Fi lost"))
        advanceUntilIdle()
        assertFalse(fixture.controller.state.value.ready)
        assertFalse(fixture.controller.state.value.visible)
        assertTrue(fixture.notices.isEmpty())
        fixture.downloadGate = null
        fixture.controller.wifiChanged(true)
        advanceUntilIdle()
        assertEquals(2, fixture.downloads)
        assertTrue(fixture.controller.state.value.ready)
    }

    @Test fun verifiedDownloadIsRestoredAfterRestartWithoutWifiOrRedownloading() = runTest {
        val fixture = Fixture(this, cached = update).apply { downloaded = true }
        fixture.controller.check(fetchLatest = false, manual = false)
        advanceUntilIdle()
        assertTrue(fixture.controller.state.value.visible)
        assertEquals(0, fixture.fetches)
        assertEquals(0, fixture.downloads)
    }

    @Test fun offlineRestartStillOffersAnAlreadyVerifiedDownload() = runTest {
        val fixture = Fixture(this, cached = update).apply {
            downloaded = true
            fetchFailure = IOException("Offline")
        }
        fixture.controller.check(fetchLatest = true, manual = false)
        advanceUntilIdle()
        assertTrue(fixture.controller.state.value.visible)
        assertTrue(fixture.controller.state.value.ready)
        assertTrue(fixture.notices.isEmpty())
        assertEquals(0, fixture.downloads)
    }

    @Test fun laterDismissalIsRespectedUntilManualCheck() = runTest {
        val fixture = Fixture(this).apply { downloaded = true }
        fixture.controller.check(fetchLatest = true, manual = false)
        advanceUntilIdle()
        fixture.controller.dismiss()
        fixture.controller.check(fetchLatest = false, manual = false)
        advanceUntilIdle()
        assertFalse(fixture.controller.state.value.visible)
        fixture.controller.check(fetchLatest = true, manual = true)
        advanceUntilIdle()
        assertTrue(fixture.controller.state.value.visible)
        assertEquals(0, fixture.downloads)
    }

    @Test fun backgroundFailuresAreQuietAndManualFailuresUseToast() = runTest {
        val fixture = Fixture(this).apply { fetchFailure = IOException("DNS unavailable") }
        fixture.controller.check(fetchLatest = true, manual = false)
        advanceUntilIdle()
        assertTrue(fixture.notices.isEmpty())
        fixture.controller.check(fetchLatest = true, manual = true)
        advanceUntilIdle()
        assertEquals(listOf("Failed: DNS unavailable"), fixture.notices)
        assertFalse(fixture.controller.state.value.visible)
    }

    @Test fun wifiArrivalRechecksAfterAnOfflineCheckWithoutWaitingSixHours() = runTest {
        val fixture = Fixture(this).apply { fetchFailure = IOException("Offline") }
        fixture.controller.check(fetchLatest = true, manual = false)
        advanceUntilIdle()
        fixture.fetchFailure = null
        fixture.controller.wifiChanged(true)
        advanceUntilIdle()
        assertEquals(2, fixture.fetches)
        assertEquals(1, fixture.downloads)
        assertTrue(fixture.controller.state.value.visible)
        assertTrue(fixture.notices.isEmpty())
    }

    @Test fun incompatibleUpdatesNeverDownloadOrPrompt() = runTest {
        val fixture = Fixture(this).apply { latest = update.copy(minSdk = 36) }
        fixture.controller.wifiChanged(true)
        fixture.controller.check(fetchLatest = true, manual = true)
        advanceUntilIdle()
        assertEquals(0, fixture.downloads)
        assertFalse(fixture.controller.state.value.visible)
        assertEquals(listOf("This update requires Android API 36 or newer."), fixture.notices)
    }

    @Test fun installedOrOlderCachedReleasesAreIgnored() = runTest {
        val fixture = Fixture(this, cached = update.copy(versionCode = 14))
        fixture.controller.check(fetchLatest = false, manual = false)
        advanceUntilIdle()
        assertEquals(0, fixture.downloads)
        assertNull(fixture.controller.state.value.update)
    }
}
