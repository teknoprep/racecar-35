package com.racedash.mobile.data

import android.content.Context
import android.util.Log
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.util.Locale

/**
 * One recorded sample line. Mirrors the firmware NDJSON shape and adds the
 * phone-specific fields (acoustic rpm + confidence, full 6-axis IMU, lap).
 */
data class Sample(
    val tEpoch: Double,
    val fix: Int,
    val sats: Int,
    val lat: Double,
    val lon: Double,
    val speedMph: Float,
    val headingDeg: Float,
    val altM: Float,
    val rpm: Int,
    val rpmConf: Float,
    val ax: Float, val ay: Float, val az: Float,
    val gx: Float, val gy: Float, val gz: Float,
    val lap: Int,
    val lapMs: Long,
)

/**
 * Writes a session as newline-delimited JSON to app-specific external storage:
 *   <externalFilesDir>/sessions/session_<unixtime>_<track>.ndjson
 * No storage permission is needed for app-specific dirs. The file is the same
 * descriptive-key NDJSON the web analyzer already consumes.
 */
class SessionRecorder(private val context: Context) {

    private var writer: BufferedWriter? = null
    var currentFile: File? = null
        private set
    var sampleCount: Int = 0
        private set
    private var lastFlushMs = 0L

    val isRecording: Boolean get() = writer != null

    @Synchronized
    fun start(trackName: String): File? {
        stop()
        return try {
            val dir = File(context.getExternalFilesDir(null), "sessions").apply { mkdirs() }
            val unix = System.currentTimeMillis() / 1000
            val safe = trackName.ifBlank { "UNKNOWN" }
                .replace(Regex("[^A-Za-z0-9_-]"), "_")
                .take(40)
            val f = File(dir, "session_${unix}_$safe.ndjson")
            writer = BufferedWriter(FileWriter(f, false))
            currentFile = f
            sampleCount = 0
            lastFlushMs = System.currentTimeMillis()
            Log.i(TAG, "recording -> ${f.absolutePath}")
            f
        } catch (e: Exception) {
            Log.e(TAG, "start failed", e)
            writer = null
            null
        }
    }

    @Synchronized
    fun write(s: Sample) {
        val w = writer ?: return
        try {
            w.append(toJson(s)).append('\n')
            sampleCount++
            val now = System.currentTimeMillis()
            if (now - lastFlushMs >= 1000) {
                w.flush()
                lastFlushMs = now
            }
        } catch (e: Exception) {
            Log.e(TAG, "write failed", e)
        }
    }

    @Synchronized
    fun stop(): File? {
        val f = currentFile
        try {
            writer?.flush()
            writer?.close()
        } catch (_: Exception) {
        }
        writer = null
        currentFile = null
        return f
    }

    private fun toJson(s: Sample): String {
        val l = Locale.US
        val rpmField = if (s.rpm >= 0) s.rpm.toString() else "null"
        return buildString {
            append('{')
            append("\"t\":").append(String.format(l, "%.3f", s.tEpoch))
            append(",\"fix\":").append(s.fix)
            append(",\"sats\":").append(s.sats)
            append(",\"lat\":").append(String.format(l, "%.6f", s.lat))
            append(",\"lon\":").append(String.format(l, "%.6f", s.lon))
            append(",\"alt_m\":").append(String.format(l, "%.1f", s.altM))
            append(",\"speed_mph\":").append(String.format(l, "%.1f", s.speedMph))
            append(",\"heading_deg\":").append(String.format(l, "%.1f", s.headingDeg))
            append(",\"rpm\":").append(rpmField)
            append(",\"rpm_conf\":").append(String.format(l, "%.2f", s.rpmConf))
            append(",\"ax\":").append(String.format(l, "%.3f", s.ax))
            append(",\"ay\":").append(String.format(l, "%.3f", s.ay))
            append(",\"az\":").append(String.format(l, "%.3f", s.az))
            append(",\"gx\":").append(String.format(l, "%.2f", s.gx))
            append(",\"gy\":").append(String.format(l, "%.2f", s.gy))
            append(",\"gz\":").append(String.format(l, "%.2f", s.gz))
            append(",\"lap\":").append(s.lap)
            append(",\"lap_ms\":").append(s.lapMs)
            append('}')
        }
    }

    private companion object {
        const val TAG = "SessionRecorder"
    }
}
