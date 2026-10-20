package dev.kaiforward.app

import okhttp3.HttpUrl
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.io.IOException
import java.security.MessageDigest

internal const val MAX_UPDATE_BYTES = 100L * 1024 * 1024
internal val updateHosts = setOf("permafrost06.com", "kaiforward.permafrost06.com")

internal fun trustedUpdateUrl(url: HttpUrl): HttpUrl {
    require(url.isHttps && url.host in updateHosts && url.port == 443 &&
        url.username.isEmpty() && url.password.isEmpty() && url.fragment == null && url.query == null) {
        "Untrusted update address."
    }
    return url
}

internal data class AppUpdate(val versionCode: Int, val versionName: String, val apkUrl: HttpUrl,
    val sha256: String, val size: Long, val minSdk: Int, val notes: String) {
    fun toJson(): String = JSONObject().put("schemaVersion", 1).put("packageName", "dev.kaiforward.app")
        .put("versionCode", versionCode).put("versionName", versionName).put("apkUrl", apkUrl.toString())
        .put("sha256", sha256).put("size", size).put("minSdk", minSdk).put("notes", notes).toString()
    companion object {
        fun parse(json: String, manifestUrl: HttpUrl): AppUpdate {
            val root = JSONObject(json)
            require(root.getInt("schemaVersion") == 1) { "Unsupported update manifest." }
            require(root.getString("packageName") == "dev.kaiforward.app") { "Wrong update package." }
            val code = root.getLong("versionCode")
            val name = root.getString("versionName")
            val digest = root.getString("sha256").lowercase()
            val size = root.getLong("size")
            val sdk = root.getInt("minSdk")
            require(code in 1..Int.MAX_VALUE && name.isNotBlank() && name.length <= 80 &&
                digest.matches(Regex("[0-9a-f]{64}")) && size in 1..MAX_UPDATE_BYTES && sdk >= 26) {
                "Invalid update manifest."
            }
            val url = requireNotNull(manifestUrl.resolve(root.getString("apkUrl"))) { "Invalid APK address." }
            return AppUpdate(code.toInt(), name, trustedUpdateUrl(url), digest, size, sdk,
                root.optString("notes").take(4000))
        }
    }
}

/** Write only a complete, size- and hash-verified APK. Failed transfers leave no installable file. */
internal fun saveUpdate(input: InputStream, target: File, update: AppUpdate, progress: (Int) -> Unit) {
    target.parentFile?.mkdirs()
    val partial = File(target.parentFile, target.name + ".part")
    target.delete()
    try {
        val digest = MessageDigest.getInstance("SHA-256")
        var count = 0L
        partial.outputStream().use { output ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                count += read
                if (count > update.size) throw IOException("Update download is larger than expected.")
                digest.update(buffer, 0, read)
                output.write(buffer, 0, read)
                progress((count * 100 / update.size).toInt())
            }
        }
        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        check(count == update.size && actual == update.sha256) { "Update verification failed. Try downloading again." }
        check(partial.renameTo(target)) { "Cannot save the update." }
    } finally { partial.delete() }
}
