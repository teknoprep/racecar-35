package com.racedash.mobile.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/** Parsed contents of android/app-manifest.json (served from GitHub raw). */
data class UpdateInfo(
    val versionCode: Long,
    val versionName: String,
    val url: String,
    val notes: String,
    val sha256: String,
)

/**
 * OTA self-updater for the side-loaded APK (same pattern as the Teensy/dash
 * firmware OTA): fetch a tiny JSON manifest from GitHub raw, compare versionCode
 * with what's installed, download the release APK (sha256-verified) and hand it
 * to the system package installer.
 *
 * Android requires the user to (a) allow "install unknown apps" for us, and
 * (b) confirm each install in the system dialog — we never install silently.
 */
class UpdateManager(context: Context) {
    private val app = context.applicationContext

    fun currentVersionCode(): Long {
        val pi = app.packageManager.getPackageInfo(app.packageName, 0)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) pi.longVersionCode
        else @Suppress("DEPRECATION") pi.versionCode.toLong()
    }

    fun currentVersionName(): String =
        app.packageManager.getPackageInfo(app.packageName, 0).versionName ?: "?"

    suspend fun fetchManifest(): UpdateInfo? = withContext(Dispatchers.IO) {
        var conn: HttpURLConnection? = null
        try {
            conn = (URL(MANIFEST_URL).openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"; connectTimeout = 8000; readTimeout = 8000
            }
            if (conn.responseCode !in 200..299) return@withContext null
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            val j = JSONObject(body)
            UpdateInfo(
                versionCode = j.optLong("versionCode", 0),
                versionName = j.optString("versionName", "?"),
                url = j.optString("url", ""),
                notes = j.optString("notes", ""),
                sha256 = j.optString("sha256", ""),
            )
        } catch (e: Exception) {
            Log.e(TAG, "manifest fetch failed", e); null
        } finally {
            conn?.disconnect()
        }
    }

    suspend fun download(info: UpdateInfo, onProgress: (Int) -> Unit): File? =
        withContext(Dispatchers.IO) {
            if (info.url.isBlank()) return@withContext null
            var conn: HttpURLConnection? = null
            try {
                val dir = File(app.cacheDir, "updates").apply { mkdirs() }
                dir.listFiles()?.forEach { it.delete() }
                val out = File(dir, "RaceDash-${info.versionName}.apk")
                conn = (URL(info.url).openConnection() as HttpURLConnection).apply {
                    instanceFollowRedirects = true; connectTimeout = 10000; readTimeout = 30000
                }
                val total = conn.contentLengthLong
                conn.inputStream.use { input ->
                    out.outputStream().use { os ->
                        val buf = ByteArray(64 * 1024); var read = 0L; var n: Int
                        while (input.read(buf).also { n = it } > 0) {
                            os.write(buf, 0, n); read += n
                            if (total > 0) onProgress(((read * 100) / total).toInt())
                        }
                    }
                }
                if (info.sha256.isNotBlank()) {
                    val got = sha256(out)
                    if (!got.equals(info.sha256, ignoreCase = true)) {
                        Log.e(TAG, "sha256 mismatch want=${info.sha256} got=$got")
                        out.delete(); return@withContext null
                    }
                }
                out
            } catch (e: Exception) {
                Log.e(TAG, "download failed", e); null
            } finally {
                conn?.disconnect()
            }
        }

    fun sha256(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { ins ->
            val buf = ByteArray(64 * 1024); var n: Int
            while (ins.read(buf).also { n = it } > 0) md.update(buf, 0, n)
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    /** Whether we're allowed to launch an APK install (Android 8+ gate). */
    fun canInstall(): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            app.packageManager.canRequestPackageInstalls() else true

    fun installPermissionIntent(): Intent =
        Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${app.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

    fun installApk(file: File) {
        val uri = FileProvider.getUriForFile(app, "${app.packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        app.startActivity(intent)
    }

    companion object {
        private const val TAG = "UpdateManager"
        const val MANIFEST_URL =
            "https://raw.githubusercontent.com/teknoprep/racecar-35/main/android/app-manifest.json"
    }
}
