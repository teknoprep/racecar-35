package com.racedash.mobile.sensors

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.location.GnssStatus
import android.location.Location
import android.location.LocationManager
import android.os.Looper
import androidx.annotation.RequiresPermission
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority

/** A GPS fix, normalised to the units the dash wants. */
data class GpsSample(
    val timeMs: Long,
    val lat: Double,
    val lon: Double,
    val speedMph: Float,
    val headingDeg: Float,
    val altM: Float,
    val accuracyM: Float,
    val hasFix: Boolean,
    val sats: Int,
)

/**
 * Wraps FusedLocationProvider for position/speed/heading and a GnssStatus
 * callback for the live satellite count. Requests the fastest interval the
 * platform will give (phone GNSS is typically ~1 Hz, a few chipsets do 5 Hz).
 */
class LocationProvider(private val context: Context) {

    private val fused: FusedLocationProviderClient =
        LocationServices.getFusedLocationProviderClient(context)
    private val locationManager =
        context.getSystemService(Context.LOCATION_SERVICE) as LocationManager

    private var onSample: ((GpsSample) -> Unit)? = null
    private var satCount: Int = 0

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val loc = result.lastLocation ?: return
            onSample?.invoke(toSample(loc))
        }
    }

    private val gnssCallback = object : GnssStatus.Callback() {
        override fun onSatelliteStatusChanged(status: GnssStatus) {
            var used = 0
            for (i in 0 until status.satelliteCount) if (status.usedInFix(i)) used++
            satCount = used
        }
    }

    @RequiresPermission(anyOf = [Manifest.permission.ACCESS_FINE_LOCATION])
    fun start(onSample: (GpsSample) -> Unit) {
        this.onSample = onSample
        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, 200L)
            .setMinUpdateIntervalMillis(100L)
            .setMaxUpdateDelayMillis(0L)
            .build()
        try {
            fused.requestLocationUpdates(request, locationCallback, Looper.getMainLooper())
            locationManager.registerGnssStatusCallback(gnssCallback, null)
        } catch (_: SecurityException) {
        }
    }

    fun stop() {
        try {
            fused.removeLocationUpdates(locationCallback)
            locationManager.unregisterGnssStatusCallback(gnssCallback)
        } catch (_: Exception) {
        }
        onSample = null
    }

    @SuppressLint("MissingPermission")
    private fun toSample(loc: Location): GpsSample {
        val mph = loc.speed * 2.2369363f               // m/s -> mph
        val heading = if (loc.hasBearing()) loc.bearing else 0f
        return GpsSample(
            timeMs = loc.time,
            lat = loc.latitude,
            lon = loc.longitude,
            speedMph = mph,
            headingDeg = heading,
            altM = if (loc.hasAltitude()) loc.altitude.toFloat() else 0f,
            accuracyM = if (loc.hasAccuracy()) loc.accuracy else 99f,
            hasFix = true,
            sats = satCount,
        )
    }
}
