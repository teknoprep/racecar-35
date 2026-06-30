package com.racedash.mobile.lap

import com.racedash.mobile.data.Tracks

/** Snapshot of lap state for the UI / recorder. Times in ms, -1 = none yet. */
data class LapState(
    val running: Boolean = false,
    val lapCount: Int = 0,
    val currentLapMs: Long = 0,
    val lastLapMs: Long = -1,
    val bestLapMs: Long = -1,
    val deltaMs: Long = 0,        // live delta vs best lap at the same track position
    val predictedMs: Long = -1,   // projected current-lap time
    val deltaValid: Boolean = false,
)

/**
 * GPS-driven lap timer with a ghost-lap predictive engine (ported from the
 * firmware dash). Start/finish is a geofence around an (approximate) point.
 *
 * Predictive method: time-vs-distance buckets. The best lap's elapsed time at
 * each distance bucket is the "ghost". The live delta is (current elapsed at
 * this distance) - (ghost elapsed at this distance); predicted lap = best lap +
 * live delta. Position-anchored, so it converges to the real time as you finish
 * the lap rather than guessing from a crude pace ratio.
 */
class LapTimer {

    private var sfLat = 0.0
    private var sfLon = 0.0
    private var sfSet = false

    private var armed = false
    private var running = false
    private var lapStartMs = 0L
    private var lapCount = 0
    private var lastLapMs = -1L
    private var bestLapMs = -1L

    private var prevLat = 0.0
    private var prevLon = 0.0
    private var havePrev = false
    private var lapDistM = 0.0

    private val curBucketMs = LongArray(MAX_BUCKETS) { -1 }
    private val bestBucketMs = LongArray(MAX_BUCKETS) { -1 }
    private var bestBucketCount = 0

    fun setStartFinish(lat: Double, lon: Double) {
        sfLat = lat; sfLon = lon; sfSet = (lat != 0.0 || lon != 0.0)
        reset()
    }

    fun reset() {
        armed = false; running = false
        lapStartMs = 0; lapCount = 0; lastLapMs = -1; bestLapMs = -1
        havePrev = false; lapDistM = 0.0
        curBucketMs.fill(-1); bestBucketMs.fill(-1); bestBucketCount = 0
    }

    fun onGps(nowMs: Long, lat: Double, lon: Double, speedMph: Float) {
        if (!sfSet) return

        // Accumulate distance travelled this lap.
        if (havePrev && running) {
            lapDistM += Tracks.haversineKm(prevLat, prevLon, lat, lon) * 1000.0
            val idx = (lapDistM / BUCKET_M).toInt()
            if (idx in 0 until MAX_BUCKETS && curBucketMs[idx] < 0) {
                curBucketMs[idx] = nowMs - lapStartMs
            }
        }
        prevLat = lat; prevLon = lon; havePrev = true

        val dToSf = Tracks.haversineKm(lat, lon, sfLat, sfLon) * 1000.0
        if (dToSf > LAP_RADIUS_M * 1.5) armed = true

        if (armed && dToSf < LAP_RADIUS_M && speedMph > 3f) {
            if (running && (nowMs - lapStartMs) > MIN_LAP_MS) {
                completeLap(nowMs)
            }
            // (Re)start a lap at the line.
            running = true
            lapStartMs = nowMs
            lapDistM = 0.0
            curBucketMs.fill(-1)
            armed = false
        }
    }

    private fun completeLap(nowMs: Long) {
        val lapMs = nowMs - lapStartMs
        lastLapMs = lapMs
        lapCount++
        if (bestLapMs < 0 || lapMs < bestLapMs) {
            bestLapMs = lapMs
            System.arraycopy(curBucketMs, 0, bestBucketMs, 0, MAX_BUCKETS)
            bestBucketCount = (lapDistM / BUCKET_M).toInt().coerceIn(0, MAX_BUCKETS)
        }
    }

    fun snapshot(nowMs: Long): LapState {
        val cur = if (running) (nowMs - lapStartMs) else 0L
        var delta = 0L
        var predicted = bestLapMs
        var deltaValid = false
        if (running && bestLapMs > 0 && bestBucketCount > 0) {
            val idx = (lapDistM / BUCKET_M).toInt().coerceIn(0, bestBucketCount - 1)
            val ghost = bestBucketMs[idx]
            if (ghost >= 0) {
                delta = cur - ghost
                predicted = bestLapMs + delta
                deltaValid = true
            }
        }
        return LapState(
            running = running,
            lapCount = lapCount,
            currentLapMs = cur,
            lastLapMs = lastLapMs,
            bestLapMs = bestLapMs,
            deltaMs = delta,
            predictedMs = predicted,
            deltaValid = deltaValid,
        )
    }

    private companion object {
        const val LAP_RADIUS_M = 75.0
        const val MIN_LAP_MS = 15000L
        const val BUCKET_M = 8.0
        const val MAX_BUCKETS = 2000     // up to ~16 km lap
    }
}
