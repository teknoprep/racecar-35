package com.racedash.mobile.vm

import com.racedash.mobile.lap.LapState

enum class GpsStatus { OFF, ACQUIRING, OK, STALE }

/** Everything the dash UI renders, rebuilt at ~20 Hz by the ViewModel ticker. */
data class DashState(
    val sensorsActive: Boolean = false,
    val audioActive: Boolean = false,

    // GPS
    val hasFix: Boolean = false,
    val gpsStatus: GpsStatus = GpsStatus.OFF,
    val sats: Int = 0,
    val lat: Double = 0.0,
    val lon: Double = 0.0,
    val speedMph: Float = 0f,
    val headingDeg: Float = 0f,
    val altM: Float = 0f,
    val gpsAgeMs: Long = 0,

    // Engine (acoustic)
    val rpm: Int = -1,
    val rpmConfidence: Float = 0f,

    // IMU
    val latG: Float = 0f,
    val longG: Float = 0f,

    // Recording / track
    val recording: Boolean = false,
    val sampleCount: Int = 0,
    val trackName: String = "",
    val nearestTrackName: String? = null,
    val nearestDistKm: Double? = null,

    // Lap timing
    val lap: LapState = LapState(),
)
