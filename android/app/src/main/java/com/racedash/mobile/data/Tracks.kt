package com.racedash.mobile.data

import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * A track with an approximate start/finish point. lat/lon double as the
 * start/finish line location for lap timing (same simplification the firmware
 * dash uses). radiusKm is how close the GPS fix must be for "auto-select".
 */
data class Track(
    val name: String,
    val lat: Double,
    val lon: Double,
    val radiusKm: Double = 2.0,
) {
    val isUnknown: Boolean get() = name == UNKNOWN_NAME

    companion object {
        const val UNKNOWN_NAME = "(no track / unknown)"
    }
}

object Tracks {
    /** Seed list of common US road courses (mirrors the firmware dash table). */
    val ALL: List<Track> = listOf(
        Track("Laguna Seca", 36.58472, -121.75306, 2.0),
        Track("Sonoma Raceway", 38.16139, -122.45444, 2.0),
        Track("Thunderhill", 39.53889, -122.33139, 2.0),
        Track("Willow Springs", 34.87139, -118.26083, 2.0),
        Track("Buttonwillow", 35.48667, -119.54639, 2.0),
        Track("COTA", 30.13472, -97.63500, 2.5),
        Track("Road America", 43.79806, -87.99028, 3.0),
        Track("Watkins Glen", 42.33667, -76.92722, 2.5),
        Track("VIR", 36.56083, -79.20694, 3.0),
        Track("Road Atlanta", 34.14861, -83.81639, 2.0),
        Track("Sebring", 27.45472, -81.34861, 3.0),
        Track("Daytona", 29.18500, -81.07306, 3.0),
        Track("Mid-Ohio", 40.68889, -82.63611, 2.0),
        Track("Barber", 33.53056, -86.61750, 2.0),
        Track("Lime Rock", 41.92778, -73.38389, 1.5),
        // NJMP (Millville, NJ) — two circuits.
        Track("NJMP Thunderbolt", 39.41480, -75.07900, 1.5),
        Track("NJMP Lightning", 39.40830, -75.08380, 1.5),
        // Thompson Speedway Motorsports Park road course (Thompson, CT).
        Track("Thompson Speedway", 41.98730, -71.83660, 1.5),
        // New Hampshire Motor Speedway (Loudon, NH) — all configs.
        Track("NHMS Oval", 43.36180, -71.46250, 1.0),
        Track("NHMS Road Course", 43.36350, -71.46000, 1.0),
        Track("NHMS North Road Course", 43.36400, -71.45980, 1.0),
        Track("NHMS South Road Course", 43.36140, -71.46180, 1.0),
        Track(Track.UNKNOWN_NAME, 0.0, 0.0, 0.0),
    )

    /** Real tracks only (excludes the synthetic UNKNOWN row). */
    val REAL: List<Track> get() = ALL.filter { !it.isUnknown }

    fun byName(name: String): Track? = ALL.firstOrNull { it.name == name }

    /** Great-circle distance in kilometres. */
    fun haversineKm(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6371.0088
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = sin(dLat / 2) * sin(dLat / 2) +
            cos(Math.toRadians(lat1)) * cos(Math.toRadians(lat2)) *
            sin(dLon / 2) * sin(dLon / 2)
        return r * 2 * atan2(sqrt(a), sqrt(1 - a))
    }

    /** Closest real track to a fix, with its distance (km), or null if no fix. */
    fun nearest(lat: Double, lon: Double): Pair<Track, Double>? {
        return REAL
            .map { it to haversineKm(lat, lon, it.lat, it.lon) }
            .minByOrNull { it.second }
    }
}
