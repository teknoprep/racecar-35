package com.racedash.mobile.data

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * All persisted user settings. Immutable snapshot; mutate via
 * [SettingsRepository.update] which writes through to SharedPreferences.
 */
data class Settings(
    // --- Engine (drives the acoustic RPM math) ---
    val cylinders: Int = 4,
    val strokes: Int = 4,            // 4 or 2
    val idleRpm: Int = 800,
    val redlineRpm: Int = 8000,

    // --- Display ---
    val rpmBarMin: Int = 0,
    val rpmBarMax: Int = 8000,
    val shiftRpm: Int = 6500,
    val useMph: Boolean = true,      // false = km/h

    // --- Audio RPM ---
    val audioRpmEnabled: Boolean = true,

    // --- Recording ---
    val autoSelectTrack: Boolean = true,

    // --- Cloud upload (After Race) ---
    val cloudEnabled: Boolean = false,
    val uploadUrl: String = "",          // e.g. https://host:port/upload
    val userEmail: String = "",          // sent as X-User-Email
    val apiKey: String = "",             // sent as X-API-Key
    val autoUpload: Boolean = true,      // upload automatically when a session stops
) {
    /** Power strokes per crank revolution. 4-stroke = cyl/2, 2-stroke = cyl. */
    val firingsPerRev: Double
        get() = if (strokes == 2) cylinders.toDouble() else cylinders / 2.0
}

class SettingsRepository(context: Context) {
    private val prefs = context.applicationContext
        .getSharedPreferences("dash", Context.MODE_PRIVATE)

    private val _state = MutableStateFlow(load())
    val state: StateFlow<Settings> = _state.asStateFlow()

    val current: Settings get() = _state.value

    fun update(transform: (Settings) -> Settings) {
        val next = clamp(transform(_state.value))
        save(next)
        _state.value = next
    }

    private fun clamp(s: Settings): Settings {
        val cyl = s.cylinders.coerceIn(1, 16)
        val strokes = if (s.strokes == 2) 2 else 4
        val idle = s.idleRpm.coerceIn(300, 5000)
        val redline = s.redlineRpm.coerceIn(idle + 1000, 20000)
        val barMax = s.rpmBarMax.coerceIn(2000, 20000)
        val barMin = s.rpmBarMin.coerceIn(0, barMax - 1000)
        val shift = s.shiftRpm.coerceIn(barMin + 500, barMax)
        return s.copy(
            cylinders = cyl, strokes = strokes, idleRpm = idle, redlineRpm = redline,
            rpmBarMax = barMax, rpmBarMin = barMin, shiftRpm = shift,
        )
    }

    private fun load(): Settings = Settings(
        cylinders = prefs.getInt("cyl", 4),
        strokes = prefs.getInt("strokes", 4),
        idleRpm = prefs.getInt("idle", 800),
        redlineRpm = prefs.getInt("redline", 8000),
        rpmBarMin = prefs.getInt("rpm_min", 0),
        rpmBarMax = prefs.getInt("rpm_max", 8000),
        shiftRpm = prefs.getInt("shift", 6500),
        useMph = prefs.getBoolean("mph", true),
        audioRpmEnabled = prefs.getBoolean("audio_rpm", true),
        autoSelectTrack = prefs.getBoolean("auto_trk", true),
        cloudEnabled = prefs.getBoolean("cl_en", false),
        uploadUrl = prefs.getString("cl_url", "") ?: "",
        userEmail = prefs.getString("cl_email", "") ?: "",
        apiKey = prefs.getString("cl_key", "") ?: "",
        autoUpload = prefs.getBoolean("cl_auto", true),
    )

    private fun save(s: Settings) {
        prefs.edit()
            .putInt("cyl", s.cylinders)
            .putInt("strokes", s.strokes)
            .putInt("idle", s.idleRpm)
            .putInt("redline", s.redlineRpm)
            .putInt("rpm_min", s.rpmBarMin)
            .putInt("rpm_max", s.rpmBarMax)
            .putInt("shift", s.shiftRpm)
            .putBoolean("mph", s.useMph)
            .putBoolean("audio_rpm", s.audioRpmEnabled)
            .putBoolean("auto_trk", s.autoSelectTrack)
            .putBoolean("cl_en", s.cloudEnabled)
            .putString("cl_url", s.uploadUrl)
            .putString("cl_email", s.userEmail)
            .putString("cl_key", s.apiKey)
            .putBoolean("cl_auto", s.autoUpload)
            .apply()
    }
}
