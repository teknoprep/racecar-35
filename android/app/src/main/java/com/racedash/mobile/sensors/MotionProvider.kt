package com.racedash.mobile.sensors

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager

/** Accelerometer (g) + gyroscope (deg/s) snapshot. */
data class MotionSample(
    val ax: Float, val ay: Float, val az: Float,    // g
    val gx: Float, val gy: Float, val gz: Float,    // deg/s
)

/**
 * Phone IMU equivalent of the firmware's MPU-6050. Accelerometer is reported in
 * g (raw m/s^2 / 9.80665); gyro in deg/s (rad/s * 57.2958). SENSOR_DELAY_GAME
 * is ~50 Hz which is plenty for lateral/longitudinal G on a dash.
 */
class MotionProvider(context: Context) {

    private val sensorManager =
        context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val accel: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val gyro: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)

    private var onSample: ((MotionSample) -> Unit)? = null

    private var ax = 0f; private var ay = 0f; private var az = 0f
    private var gx = 0f; private var gy = 0f; private var gz = 0f

    private val listener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER -> {
                    ax = event.values[0] / 9.80665f
                    ay = event.values[1] / 9.80665f
                    az = event.values[2] / 9.80665f
                }
                Sensor.TYPE_GYROSCOPE -> {
                    gx = event.values[0] * 57.29578f
                    gy = event.values[1] * 57.29578f
                    gz = event.values[2] * 57.29578f
                }
            }
            onSample?.invoke(MotionSample(ax, ay, az, gx, gy, gz))
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    fun start(onSample: (MotionSample) -> Unit) {
        this.onSample = onSample
        accel?.let { sensorManager.registerListener(listener, it, SensorManager.SENSOR_DELAY_GAME) }
        gyro?.let { sensorManager.registerListener(listener, it, SensorManager.SENSOR_DELAY_GAME) }
    }

    fun stop() {
        sensorManager.unregisterListener(listener)
        onSample = null
    }
}
