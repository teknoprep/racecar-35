package com.racedash.mobile.vm

import android.annotation.SuppressLint
import android.app.Application
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.racedash.mobile.data.Sample
import com.racedash.mobile.data.SessionRecorder
import com.racedash.mobile.data.Settings
import com.racedash.mobile.data.SettingsRepository
import com.racedash.mobile.data.SessionStore
import com.racedash.mobile.data.Track
import com.racedash.mobile.data.Tracks
import com.racedash.mobile.data.UpdateInfo
import com.racedash.mobile.data.UpdateManager
import com.racedash.mobile.data.Uploader
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
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.abs

class DashViewModel(app: Application) : AndroidViewModel(app) {

    val settingsRepo = SettingsRepository(app)
    val settings: StateFlow<Settings> get() = settingsRepo.state

    private val location = LocationProvider(app)
    private val motion = MotionProvider(app)
    private val recorder = SessionRecorder(app)
    private val store = SessionStore(app)
    private val uploader = Uploader()
    private val updateManager = UpdateManager(app)
    private val lapTimer = LapTimer()
    private val acoustic = AcousticRpm(::onRpm)

    private val _state = MutableStateFlow(DashState())
    val state: StateFlow<DashState> = _state.asStateFlow()

    private val _update = MutableStateFlow(UpdateState())
    val update: StateFlow<UpdateState> = _update.asStateFlow()
    @Volatile private var pendingUpdate: UpdateInfo? = null

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

    @Volatile private var uploadStatus = ""
    @Volatile private var pendingUploads = 0

    // --- Auto-stop at 0 speed ---
    // hasMoved arms the stop once the car has actually driven; zeroSpeedSince
    // is the elapsedRealtime of the first standstill tick of this stretch.
    @Volatile private var hasMoved = false
    @Volatile private var zeroSpeedSince = 0L

    /** One upload at a time: manual, after-STOP and Wi-Fi triggers all funnel here. */
    private val uploadGate = AtomicBoolean(false)

    // Auto-upload on Wi-Fi: the callback fires for an already-connected network
    // on register and again on every (re)connect — e.g. back at the trailer.
    private val connMgr: ConnectivityManager? =
        app.getSystemService(ConnectivityManager::class.java)

    private val wifiCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = maybeAutoUploadOnWifi()
        override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) =
            maybeAutoUploadOnWifi()
    }

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

        // Watch for Wi-Fi and drain pending sessions as soon as it appears.
        // No-op unless cloud upload + on-Wi-Fi auto-upload are both enabled.
        runCatching {
            connMgr?.registerNetworkCallback(
                NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .build(),
                wifiCallback,
            )
        }
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
        if (recording) stopRecording() else startRecording()
        return recording
    }

    private fun startRecording() {
        // Auto-pick the closest track if enabled and none chosen.
        if (selectedTrack == null && settingsRepo.current.autoSelectTrack) {
            nearest()?.let { (t, d) -> if (d <= t.radiusKm) selectTrack(t) }
        }
        val name = selectedTrack?.name ?: Track.UNKNOWN_NAME
        recorder.start(name)
        recording = recorder.isRecording
        hasMoved = false
        zeroSpeedSince = 0L
    }

    /**
     * Close the session and auto-upload it when that setting is on. Shared by
     * the START/STOP button and the 0-speed auto-stop; [auto] only changes the
     * status text.
     */
    private fun stopRecording(auto: Boolean = false) {
        if (!recording) return
        val closed = recorder.stop()
        recording = false
        hasMoved = false
        zeroSpeedSince = 0L
        val cfg = settingsRepo.current
        if (closed != null && cfg.cloudEnabled && cfg.autoUpload) {
            viewModelScope.launch(Dispatchers.IO) { uploadList(listOf(closed)) }
        } else if (auto) {
            uploadStatus = "auto-stopped: ${cfg.autoStopSeconds}s at 0 speed"
        }
    }

    /**
     * Stop the session after [Settings.autoStopSeconds] at a standstill. Needs a
     * live fix (a GPS dropout must not end a session) and only arms after the
     * car has moved once, so hitting START while parked doesn't kill it.
     */
    private fun maybeAutoStop(now: Long, fixOk: Boolean) {
        val cfg = settingsRepo.current
        val speed = speedMph.coerceAtLeast(0f)
        if (speed >= MOVING_MPH) hasMoved = true
        if (!cfg.autoStopEnabled || !hasMoved || !fixOk) {
            zeroSpeedSince = 0L
            return
        }
        if (speed >= STOPPED_MPH) {
            zeroSpeedSince = 0L
        } else if (zeroSpeedSince == 0L) {
            zeroSpeedSince = now
        } else if (now - zeroSpeedSince >= cfg.autoStopSeconds * 1000L) {
            stopRecording(auto = true)
        }
    }

    val isRecording: Boolean get() = recording

    // --- Cloud upload -----------------------------------------------------

    /** Manually upload every pending session (not the one being recorded). */
    fun uploadPending() {
        viewModelScope.launch(Dispatchers.IO) {
            uploadList(store.pending(recorder.currentFile))
        }
    }

    /**
     * Drain every pending session the moment Wi-Fi is available — the phone
     * usually records on the road, so this gets yesterday's sessions off the
     * device once it is back on a home/trailer network.
     */
    fun maybeAutoUploadOnWifi() {
        val cfg = settingsRepo.current
        if (!cfg.cloudEnabled || !cfg.autoUploadOnWifi) return
        if (uploadGate.get() || !onWifi()) return
        // Quiet no-op when there is nothing waiting, so the frequent Wi-Fi
        // capability callbacks don't keep re-writing the status line.
        val pending = store.pending(recorder.currentFile)
        if (pending.isEmpty()) return
        viewModelScope.launch(Dispatchers.IO) { uploadList(pending) }
    }

    /** Re-check Wi-Fi auto-upload after the user flips a cloud setting. */
    fun onUploadSettingsChanged() = maybeAutoUploadOnWifi()

    private fun onWifi(): Boolean {
        val cm = connMgr ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork ?: return false) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
    }

    private suspend fun uploadList(files: List<File>) {
        if (!uploadGate.compareAndSet(false, true)) {
            uploadStatus = "upload already in progress"
            return
        }
        try {
            val cfg = settingsRepo.current
            when {
                !cfg.cloudEnabled -> { uploadStatus = "cloud upload is off"; return }
                cfg.uploadUrl.isBlank() -> { uploadStatus = "set an upload URL first"; return }
                files.isEmpty() -> { uploadStatus = "nothing to upload"; return }
            }
            val ucfg = Uploader.Config(cfg.uploadUrl, cfg.apiKey, cfg.userEmail)
            var ok = 0
            var fail = 0
            files.forEachIndexed { i, f ->
                uploadStatus = "uploading ${i + 1}/${files.size}\u2026"
                val r = uploader.upload(f, ucfg)
                if (r.isSuccess) { store.markUploaded(f); ok++ }
                else { fail++; uploadStatus = "failed: ${r.exceptionOrNull()?.message ?: "error"}" }
            }
            uploadStatus = if (fail == 0) "uploaded $ok ✓" else "uploaded $ok, $fail failed"
            pendingUploads = store.pending(recorder.currentFile).size
        } finally {
            uploadGate.set(false)
        }
    }

    // --- OTA self-update --------------------------------------------------

    fun appVersion(): String = updateManager.currentVersionName()

    /** Check GitHub for a newer APK. [manual]=true shows an "up to date" / error
     *  dialog; automatic checks stay silent unless an update is actually found. */
    fun checkForUpdate(manual: Boolean) {
        viewModelScope.launch {
            _update.value = UpdateState(UpdatePhase.CHECKING)
            val info = updateManager.fetchManifest()
            _update.value = when {
                info == null ->
                    if (manual) UpdateState(UpdatePhase.ERROR, message = "Couldn't reach the update server.")
                    else UpdateState()
                info.versionCode > updateManager.currentVersionCode() -> {
                    pendingUpdate = info
                    UpdateState(UpdatePhase.AVAILABLE, versionName = info.versionName, notes = info.notes)
                }
                else ->
                    if (manual) UpdateState(UpdatePhase.UPTODATE, versionName = updateManager.currentVersionName())
                    else UpdateState()
            }
        }
    }

    fun downloadAndInstall() {
        val info = pendingUpdate ?: return
        if (!updateManager.canInstall()) {
            _update.value = _update.value.copy(phase = UpdatePhase.NEED_PERMISSION)
            return
        }
        viewModelScope.launch {
            _update.value = UpdateState(UpdatePhase.DOWNLOADING, versionName = info.versionName)
            val file = updateManager.download(info) { p ->
                _update.value = _update.value.copy(progress = p)
            }
            if (file == null) {
                _update.value = UpdateState(UpdatePhase.ERROR, message = "Download failed or checksum mismatch.")
                return@launch
            }
            _update.value = UpdateState(UpdatePhase.INSTALLING, versionName = info.versionName)
            updateManager.installApk(file)
        }
    }

    fun installPermissionIntent(): Intent = updateManager.installPermissionIntent()
    fun dismissUpdate() { _update.value = UpdateState() }

    // --- 20 Hz state rebuild + logging ------------------------------------

    private suspend fun ticker() {
        var n = 0
        while (true) {
            val now = SystemClock.elapsedRealtime()
            if (n++ % 20 == 0) pendingUploads = store.pending(recorder.currentFile).size
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
                maybeAutoStop(now, fixOk)
            } else {
                hasMoved = false
                zeroSpeedSince = 0L
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
                pendingUploads = pendingUploads,
                uploadStatus = uploadStatus,
            )
            delay(50)
        }
    }

    override fun onCleared() {
        super.onCleared()
        if (recording) recorder.stop()
        runCatching { connMgr?.unregisterNetworkCallback(wifiCallback) }
        location.stop()
        motion.stop()
        acoustic.stop()
    }

    private companion object {
        const val MOVING_MPH = 3f   // car has driven: auto-stop may arm
        const val STOPPED_MPH = 1f  // treated as 0 speed for auto-stop
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
