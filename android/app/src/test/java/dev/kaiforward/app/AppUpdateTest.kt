package dev.kaiforward.app

import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest

class AppUpdateTest {
    private val base = "https://permafrost06.com/kaiforward/updates/latest.json".toHttpUrl()
    private val bytes = "signed APK bytes".toByteArray()
    private val hash = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    private fun manifest() = JSONObject().put("schemaVersion", 1).put("packageName", "dev.kaiforward.app")
        .put("versionCode", 12).put("versionName", "0.7.0").put("apkUrl", "kaiforward-12.apk")
        .put("sha256", hash).put("size", bytes.size).put("minSdk", 26).put("notes", "New updater")

    @Test fun relativeDownloadsAndFutureSubdomainAreSupported() {
        assertEquals("https://permafrost06.com/kaiforward/updates/kaiforward-12.apk",
            AppUpdate.parse(manifest().toString(), base).apkUrl.toString())
        val moved = manifest().put("apkUrl", "https://kaiforward.permafrost06.com/updates/app.apk")
        assertEquals("kaiforward.permafrost06.com", AppUpdate.parse(moved.toString(), base).apkUrl.host)
    }

    @Test fun persistedManifestPreservesItsResolvedDownloadAddress() {
        val update = AppUpdate.parse(manifest().toString(), base)
        assertEquals(update, AppUpdate.parse(update.toJson(), "https://kaiforward.permafrost06.com/updates/latest.json".toHttpUrl()))
    }

    @Test fun credentialsCannotBeSentToOtherHostsOrInsecureAddresses() {
        listOf("http://permafrost06.com/app.apk", "https://example.com/app.apk",
            "https://permafrost06.com.evil.example/app.apk", "https://user:pass@permafrost06.com/app.apk",
            "https://permafrost06.com:444/app.apk", "https://permafrost06.com/app.apk?token=secret").forEach { url ->
            assertThrows(IllegalArgumentException::class.java) {
                AppUpdate.parse(manifest().put("apkUrl", url).toString(), base)
            }
        }
    }

    @Test fun malformedAndWrongPackageManifestsAreRejected() {
        listOf("schemaVersion" to 2, "packageName" to "other.app", "versionCode" to -1,
            "versionCode" to Long.MAX_VALUE, "sha256" to "bad", "size" to 0,
            "size" to MAX_UPDATE_BYTES + 1, "minSdk" to 25).forEach { (key, value) ->
            assertThrows(IllegalArgumentException::class.java) {
                AppUpdate.parse(manifest().put(key, value).toString(), base)
            }
        }
    }

    @Test fun downloadChecksSizeAndHashAndNeverKeepsPartialFiles() {
        val root = Files.createTempDirectory("app-update-test").toFile()
        try {
            val target = File(root, "update.apk")
            val update = AppUpdate.parse(manifest().toString(), base)
            saveUpdate(ByteArrayInputStream(bytes), target, update) { }
            assertArrayEquals(bytes, target.readBytes())
            listOf(bytes.copyOf(bytes.size - 1), bytes + 0, ByteArray(bytes.size)).forEach { bad ->
                assertThrows(Exception::class.java) { saveUpdate(ByteArrayInputStream(bad), target, update) { } }
                assertFalse(target.exists())
                assertFalse(File(root, "update.apk.part").exists())
            }
        } finally { root.deleteRecursively() }
    }
}
