package dev.kaiforward.app

import android.app.Application
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Handler
import android.os.Looper
import androidx.core.content.FileProvider
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.ConnectionPool
import okhttp3.Dns
import okhttp3.Credentials
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

class AppUpdateViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application
    private val prefs = app.getSharedPreferences("app-updates", Application.MODE_PRIVATE)
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(15, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS)
        .callTimeout(5, TimeUnit.MINUTES).build()
    private val connectivity = app.getSystemService(ConnectivityManager::class.java)
    @Volatile private var downloadCall: Call? = null
    @Volatile private var downloadNetwork: Network? = null
    private val messages = Channel<String>(Channel.BUFFERED)
    internal val notices = messages.receiveAsFlow()
    private val controller = AppUpdateController(viewModelScope, BuildConfig.VERSION_CODE, Build.VERSION.SDK_INT,
        cached = prefs.getString("candidate", null)?.let { saved ->
            runCatching { AppUpdate.parse(saved, BuildConfig.UPDATE_URL.toHttpUrl()) }.getOrNull()
        },
        fetch = {
            prefs.edit().putLong("lastAttempt", System.currentTimeMillis()).apply()
            fetchUpdate()
        },
        remember = { candidate -> prefs.edit().putString("candidate", candidate?.toJson()).apply() },
        hasDownload = { update -> withContext(Dispatchers.IO) {
            if (!apk(update).isFile) false
            else try { verifyApk(update); true }
            catch (error: Exception) {
                if (error is CancellationException) throw error
                apk(update).delete()
                false
            }
        } },
        download = ::download,
        notice = { messages.trySend(it) },
        errorText = ::errorText)
    internal val state = controller.state
    private var installing = false
    private var observedWifi = wifiNetwork()
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
            networkChanged()
        }
        override fun onLost(network: Network) { networkChanged() }
    }

    init {
        controller.wifiChanged(wifiNetwork() != null, startDownload = false)
        connectivity.registerDefaultNetworkCallback(networkCallback, Handler(Looper.getMainLooper()))
    }

    private fun wifiNetwork(): Network? = connectivity.activeNetwork?.takeIf { network ->
        connectivity.getNetworkCapabilities(network)?.let { capabilities ->
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
                capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
        } == true
    }

    private fun networkChanged() {
        val wifi = wifiNetwork()
        if (downloadNetwork != null && wifi != downloadNetwork) downloadCall?.cancel()
        if (wifi != observedWifi) {
            controller.wifiChanged(false)
            observedWifi = wifi
        }
        controller.wifiChanged(wifi != null)
    }

    internal fun dismiss() = controller.dismiss()

    internal fun checkForUpdates(manual: Boolean = false) {
        val now = System.currentTimeMillis()
        val previous = prefs.getLong("lastAttempt", 0)
        val due = manual || now < previous || now - previous >= TimeUnit.HOURS.toMillis(6)
        controller.wifiChanged(wifiNetwork() != null, startDownload = false)
        controller.check(fetchLatest = due, manual = manual)
    }

    private suspend fun fetchUpdate(): AppUpdate = withContext(Dispatchers.IO) {
        val primary = BuildConfig.UPDATE_URL.toHttpUrl()
        val response = try { response(primary) }
        catch (error: java.net.UnknownHostException) {
            if (primary.host != "kaiforward.permafrost06.com") throw error
            // Older DNS caches may not know the newly created subdomain yet.
            response("https://permafrost06.com/kaiforward/updates/latest.json".toHttpUrl())
        }
        response.use { reply ->
            val body = checkNotNull(reply.body) { "Empty update response." }
            val output = ByteArrayOutputStream()
            body.byteStream().use { input ->
                val buffer = ByteArray(4096)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    check(output.size() + count <= 64 * 1024) { "Update manifest is too large." }
                    output.write(buffer, 0, count)
                }
            }
            AppUpdate.parse(output.toString("UTF-8"), reply.request.url)
        }
    }

    // Follow only HTTPS redirects to our two explicitly trusted hosts. Credentials never go elsewhere.
    private fun response(start: HttpUrl, transport: OkHttpClient = client, wifi: Network? = null): Response {
        var url = start
        repeat(6) {
            trustedUpdateUrl(url)
            if (wifi != null && wifiNetwork() != wifi) throw WifiRequiredException()
            val call = transport.newCall(Request.Builder().url(url)
                .header("Authorization", Credentials.basic("kaiforward", BuildConfig.APP_TOKEN))
                .header("Cache-Control", "no-cache").build())
            if (wifi != null) {
                downloadCall = call
                if (wifiNetwork() != wifi) { call.cancel(); throw WifiRequiredException() }
            }
            val reply = call.execute()
            if (reply.code in setOf(301, 302, 303, 307, 308)) {
                val location = reply.header("Location")
                reply.close()
                url = location?.let(url::resolve) ?: error("Invalid update redirect.")
            } else {
                if (reply.isSuccessful) return reply
                val code = reply.code
                reply.close()
                error(if (code == 401 || code == 403) "Update access was denied. Contact the app owner."
                    else "Update server returned HTTP $code. Try again later.")
            }
        }
        error("Too many update redirects.")
    }

    private fun apk(update: AppUpdate) = File(app.cacheDir, "updates/${update.versionCode}-${update.sha256.take(16)}.apk")

    private class WifiRequiredException : IOException()

    private suspend fun download(update: AppUpdate) = withContext(Dispatchers.IO) {
        val network = wifiNetwork() ?: throw WifiRequiredException()
        // Bind both DNS and sockets to this network; a new mobile default must not take over.
        val transport = client.newBuilder().socketFactory(network.socketFactory)
            .dns(object : Dns {
                override fun lookup(hostname: String) = network.getAllByName(hostname).toList()
            })
            .connectionPool(ConnectionPool()).build()
        downloadNetwork = network
        try {
            val jobContext = coroutineContext
            response(update.apkUrl, transport, network).use { reply ->
                val body = checkNotNull(reply.body) { "Empty APK response." }
                check(body.contentLength() == -1L || body.contentLength() == update.size) {
                    "Update size does not match the manifest."
                }
                body.byteStream().use { input ->
                    saveUpdate(input, apk(update), update) {
                        jobContext.ensureActive()
                        if (wifiNetwork() != network) throw WifiRequiredException()
                    }
                }
            }
            verifyApk(update)
        } finally {
            downloadCall = null
            downloadNetwork = null
            transport.connectionPool.evictAll()
        }
    }

    @Suppress("DEPRECATION")
    private fun verifyApk(update: AppUpdate) {
        val file = apk(update)
        check(file.length() == update.size && file.inputStream().use { input ->
            val digest = MessageDigest.getInstance("SHA-256")
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
            digest.digest().joinToString("") { "%02x".format(it) } == update.sha256
        }) { "Downloaded update is missing or damaged. Download it again." }
        val pm = app.packageManager
        val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val candidate = checkNotNull(pm.getPackageArchiveInfo(file.path, flags)) { "Cannot read the update APK." }
        val installed = pm.getPackageInfo(app.packageName, flags)
        fun code(info: PackageInfo) = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()
        check(candidate.packageName == app.packageName && code(candidate) == update.versionCode.toLong() &&
            code(candidate) > code(installed) && candidate.applicationInfo?.minSdkVersion == update.minSdk) {
            "APK version or package does not match the update."
        }
        fun signers(info: PackageInfo) = (if (Build.VERSION.SDK_INT >= 28)
            info.signingInfo?.apkContentsSigners else info.signatures)?.map { it.toCharsString() }?.toSet().orEmpty()
        val currentSigners = signers(installed)
        check(currentSigners.isNotEmpty() && currentSigners == signers(candidate)) {
            "Update signing certificate does not match this installation."
        }
    }

    internal fun install() {
        val update = state.value.update ?: return
        if (!state.value.ready || installing) return
        installing = true
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) { verifyApk(update) }
                check(app.packageManager.canRequestPackageInstalls()) { "Allow KaiForward to install updates, then tap Install." }
                val uri = FileProvider.getUriForFile(app, app.packageName + ".updates", apk(update))
                app.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                messages.trySend(errorText(error))
            } finally { installing = false }
        }
    }

    private fun errorText(error: Exception): String = when (error) {
        is WifiRequiredException -> "Update download is waiting for Wi-Fi."
        is java.net.UnknownHostException -> "Cannot resolve the update server. DNS may still be updating. Try again later."
        is javax.net.ssl.SSLException -> "Cannot establish a secure connection to the update server."
        is IOException -> "Cannot download the update. Check your internet connection and try again."
        is IllegalStateException, is IllegalArgumentException -> error.message ?: "Invalid update response."
        else -> "Unable to update the app. Try again later."
    }

    override fun onCleared() {
        connectivity.unregisterNetworkCallback(networkCallback)
        downloadCall?.cancel()
        messages.close()
        super.onCleared()
    }
}
