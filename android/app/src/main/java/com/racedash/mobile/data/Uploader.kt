package com.racedash.mobile.data

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * After-Race uploader: whole-file POST of a session NDJSON to the configured
 * endpoint. Mirrors the firmware's cloud convention — `Content-Type:
 * application/x-ndjson` plus `X-API-Key` / `X-User-Email` / `X-Session-Id` /
 * `X-Track-Name` headers. Uses plain HttpURLConnection (no extra deps); runs on
 * the IO dispatcher.
 */
class Uploader {

    data class Config(val url: String, val apiKey: String, val email: String)

    suspend fun upload(file: File, cfg: Config): Result<Int> = withContext(Dispatchers.IO) {
        if (cfg.url.isBlank()) return@withContext Result.failure(IOException("No upload URL set"))
        if (!file.exists()) return@withContext Result.failure(IOException("File missing"))
        val target = try {
            URL(normalizeUrl(cfg.url))
        } catch (e: Exception) {
            return@withContext Result.failure(IOException("Bad URL: ${cfg.url}"))
        }
        val (sessionId, track) = parseName(file.name)
        var conn: HttpURLConnection? = null
        try {
            conn = (target.openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = 8000
                readTimeout = 20000
                doOutput = true
                instanceFollowRedirects = true
                setRequestProperty("Content-Type", "application/x-ndjson")
                setRequestProperty("Accept", "*/*")
                if (cfg.apiKey.isNotBlank()) setRequestProperty("X-API-Key", cfg.apiKey)
                if (cfg.email.isNotBlank()) setRequestProperty("X-User-Email", cfg.email)
                setRequestProperty("X-Session-Id", sessionId)
                setRequestProperty("X-Track-Name", track)
                setFixedLengthStreamingMode(file.length())
            }
            conn.outputStream.use { os -> file.inputStream().use { it.copyTo(os) } }
            val code = conn.responseCode
            Log.i(TAG, "upload ${file.name} -> HTTP $code")
            if (code in 200..299) Result.success(code)
            else {
                val body = readError(conn)
                Result.failure(IOException("HTTP $code${if (body.isNotBlank()) ": $body" else ""}"))
            }
        } catch (e: Exception) {
            // Surface the concrete cause (cleartext blocked, DNS, timeout, refused...).
            Log.e(TAG, "upload failed", e)
            Result.failure(IOException(e.message ?: e.javaClass.simpleName, e))
        } finally {
            conn?.disconnect()
        }
    }

    /** Accept host-only / scheme-less entries by defaulting to http://. */
    private fun normalizeUrl(raw: String): String {
        val s = raw.trim()
        return if (s.startsWith("http://", true) || s.startsWith("https://", true)) s
        else "http://$s"
    }

    /** Best-effort read of an error response body for a useful message. */
    private fun readError(conn: HttpURLConnection): String = try {
        (conn.errorStream ?: conn.inputStream)?.bufferedReader()?.use {
            it.readText().trim().take(200)
        } ?: ""
    } catch (_: Exception) { "" }

    /** session_<unix>_<track>.ndjson -> (sessionId, trackName) */
    private fun parseName(name: String): Pair<String, String> {
        val base = name.removeSuffix(".ndjson")
        val parts = base.split("_")
        val sid = parts.getOrNull(1) ?: "0"
        val track = if (parts.size > 2) parts.subList(2, parts.size).joinToString("_") else "UNKNOWN"
        return sid to track
    }

    private companion object {
        const val TAG = "Uploader"
    }
}
