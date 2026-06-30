package com.racedash.mobile.vm

import android.annotation.SuppressLint
import android.app.Application
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.racedash.mobile.data.Sample
import com.racedash.mobile.data.SessionRecorder
import com.racedash.mobile.data.Settings
import com.racedash.mobile.data.SettingsRepository
import com.racedash.mobile.data.Track
import com.racedash.mobile.data.Tracks
import com.racedash.mobile.lap.LapTimer
import com.racedash.mobile.sensors.AcousticRpm
import com.racedash.mobile.sensors.LocationProvider
import com.racedash.mobile.sensors.MotionProvider
import com.racedash.mobile.sensors.RpmEstimate
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import kotlin.math.abs

class DashViewModel(app: Application) : AndroidViewModel(app) {

    val settingsRepo = SettingsRepository(app)
    val settings: StateFlow<Settings> get() = settingsRepo.state

    private val location = LocationProvider(app)
    private val motion = MotionProvider(app)
    private val recorder = SessionRecorder(app)
    private val lapTimer = LapTimer()
    private val acoustic = AcousticRpm(::onRpm)

    private val _state = MutableStateFlow(DashState())
    val state: StateFlow<DashState> = _state.asStateFlow()

    // --- Latest sensor values (written from callbacks, read by the ticker) ---
    @Volatile private var lat = 0.0
    @Volatile private var lon = 0.0
    @Volatile private var speedMph = 0f
    @Volatile private var heading = 0f
    @Volatile private var altM = 0f
    @Volatile private var sats = 0
    @Volatile private var lastFixElapsed = 0L
    @Volatile private var haveFix = false

    @Volatile private var rpm = -1
    @Volatile private var rpmConf = 0f

    // Full 6-axis IMU (latG/longG also feed the on-screen G readout).
    @Volatile private var latG = 0f      // ax (g)
    @Volatile private var longG = 0f     // ay (g)
    @Volatile private var accZ = 0f      // az (g)
    @Volatile private var gyrX = 0f      // deg/s
    @Volatile private var gyrY = 0f
    @Volatile private var gyrZ = 0f

    @Volatile private var locationGranted = false
    @Volatile private var audioGranted = false
    @Volatile private var sensorsStarted = false
    @Volatile private var motionStarted = false
    @Volatile private var audioStarted = false

    @Volatile private var selectedTrack: Track? = null
    @Volatile private var recording = false

    init {
        // Push current engine geometry into the acoustic estimator and keep it
        // in sync as the user edits engine settings.
        viewModelScope.launch {
            settingsRepo.state.collect { s ->
                acoustic.setEngine(s.cylinders, s.strokes, s.idleRpm, s.redlineRpm)
            }
        }
        // Off the main thread: this loop does file I/O while recording.
        viewModelScope.launch(Dispatchers.Default) { ticker() }
    }

    /** Called by the Activity once runtime permissions are resolved. */
    fun onPermissions(location: Boolean, audio: Boolean) {
        locationGranted = location
        audioGranted = audio
        startSensors()
    }

    @SuppressLint("MissingPermission")
    private fun startSensors() {
        if (locationGranted && !sensorsStarted) {
            sensorsStarted = true
            try {
                location.start { g ->
                    lat = g.lat; lon = g.lon; speedMph = g.speedMph
                    heading = g.headingDeg; altM = g.altM; sats = g.sats
                    haveFix = g.hasFix; lastFixElapsed = SystemClock.elapsedRealtime()
                    lapTimer.onGps(lastFixElapsed, g.lat, g.lon, g.speedMph)
                }
            } catch (_: SecurityException) {
                sensorsStarted = false
            }
        }
        if (!motionStarted) {
            motionStarted = true
            motion.start { m ->
                latG = m.ax; longG = m.ay; accZ = m.az
                gyrX = m.gx; gyrY = m.gy; gyrZ = m.gz
            }
        }
        maybeStartAudio()
    }

    @SuppressLint("MissingPermission")
    private fun maybeStartAudio() {
        val want = audioGranted && settingsRepo.current.audioRpmEnabled
        if (want && !audioStarted) {
            audioStarted = true
            try {
                acoustic.start()
            } catch (_: SecurityException) {
                audioStarted = false
            }
        } else if (!settingsRepo.current.audioRpmEnabled && audioStarted) {
            acoustic.stop(); audioStarted = false; rpm = -1
        }
    }

    private fun onRpm(e: RpmEstimate) {
        rpm = e.rpm
        rpmConf = e.confidence
    }

    /** Re-evaluate audio start/stop after a settings change. */
    fun onSettingsChanged() = maybeStartAudio()

    // --- Track selection / start-finish -----------------------------------

    fun nearest(): Pair<Track, Double>? =
        if (haveFix) Tracks.nearest(lat, lon) else null

    fun selectTrack(track: Track) {
        selectedTrack = track
        if (!track.isUnknown) lapTimer.setStartFinish(track.lat, track.lon)
    }

    /** Use the current GPS position as this track's start/finish line. */
    fun setStartFinishHere() {
        if (haveFix) lapTimer.setStartFinish(lat, lon)
    }

    // --- Recording --------------------------------------------------------

    fun toggleRecording(): Boolean {
        if (recording) {
            recorder.stop()
            recording = false
        } else {
            // Auto-pick the closest track if enabled and none chosen.
            if (selectedTrack == null && settingsRepo.current.autoSelectTrack) {
                nearest()?.let { (t, d) -> if (d <= t.radiusKm) selectTrack(t) }
            }
            val name = selectedTrack?.name ?: Track.UNKNOWN_NAME
            recorder.start(name)
            recording = recorder.isRecording
        }
        return recording
    }

    val isRecording: Boolean get() = recording

    // --- 20 Hz state rebuild + logging ------------------------------------

    private suspend fun ticker() {
        while (true) {
            val now = SystemClock.elapsedRealtime()
            val age = if (haveFix) now - lastFixElapsed else 0L
            val status = when {
                !sensorsStarted -> GpsStatus.OFF
                !haveFix -> GpsStatus.ACQUIRING
                age >= 3000 -> GpsStatus.STALE
                else -> GpsStatus.OK
            }
            val fixOk = haveFix && age < 3000
            val lap = lapTimer.snapshot(now)

            if (recording) {
                recorder.write(
                    Sample(
                        tEpoch = System.currentTimeMillis() / 1000.0,
                        fix = if (fixOk) 3 else 0,
                        sats = sats,
                        lat = lat, lon = lon,
                        speedMph = speedMph, headingDeg = heading, altM = altM,
                        rpm = rpm, rpmConf = rpmConf,
                        ax = latG, ay = longG, az = accZ,
                        gx = gyrX, gy = gyrY, gz = gyrZ,
                        lap = lap.lapCount, lapMs = lap.currentLapMs,
                    )
                )
            }

            val near = if (haveFix) Tracks.nearest(lat, lon) else null
            _state.value = DashState(
                sensorsActive = sensorsStarted,
                audioActive = audioStarted,
                hasFix = fixOk,
                gpsStatus = status,
                sats = sats,
                lat = lat, lon = lon,
                speedMph = if (speedMph < 0.5f) 0f else speedMph,
                headingDeg = heading,
                altM = altM,
                gpsAgeMs = age,
                rpm = rpm,
                rpmConfidence = rpmConf,
                latG = latG, longG = longG,
                recording = recording,
                sampleCount = recorder.sampleCount,
                trackName = selectedTrack?.name ?: "",
                nearestTrackName = near?.first?.name,
                nearestDistKm = near?.second,
                lap = lap,
            )
            delay(50)
        }
    }

    override fun onCleared() {
        super.onCleared()
        if (recording) recorder.stop()
        location.stop()
        motion.stop()
        acoustic.stop()
    }
}

/** Format helpers shared by the UI. */
object Fmt {
    fun lapTime(ms: Long): String {
        if (ms < 0) return "--:--.-"
        val totalTenths = ms / 100
        val m = totalTenths / 600
        val s = (totalTenths / 10) % 60
        val t = totalTenths % 10
        return if (m > 0) String.format("%d:%02d.%d", m, s, t)
        else String.format("%d.%d", s, t)
    }

    fun delta(ms: Long): String {
        val sign = if (ms > 0) "+" else if (ms < 0) "-" else "\u00B1"
        val a = abs(ms)
        return sign + String.format("%d.%02d", a / 1000, (a % 1000) / 10)
    }
}
