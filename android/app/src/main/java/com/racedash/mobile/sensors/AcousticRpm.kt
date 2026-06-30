package com.racedash.mobile.sensors

import android.Manifest
import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import androidx.annotation.RequiresPermission
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min

/** One acoustic RPM estimate. [confidence] 0..1; rpm < 0 means "not confident". */
data class RpmEstimate(val rpm: Int, val confidence: Float, val firingHz: Float)

/**
 * Estimates engine RPM from the microphone by finding the exhaust "firing
 * frequency" and converting it with the engine geometry.
 *
 *   4-stroke: f_fire = (RPM/60) * (cylinders/2)   ->  RPM = f_fire * 60 / firingsPerRev
 *
 * Pipeline: AudioRecord (UNPROCESSED) -> box-decimate -> Hann window -> FFT ->
 * Harmonic Product Spectrum over the firing band (rejects octave errors) ->
 * parabolic peak interp -> confidence gate -> EMA + slew-limit smoothing.
 *
 * The firing fundamental for a street car lives at ~25-500 Hz; we lean on the
 * harmonics (still strong above the phone mic's low-end roll-off) via HPS so it
 * works even at idle.
 */
class AcousticRpm(
    private val onEstimate: (RpmEstimate) -> Unit,
) {
    @Volatile private var firingsPerRev: Double = 2.0          // 4-cyl 4-stroke
    @Volatile private var fMinHz: Double = 20.0
    @Volatile private var fMaxHz: Double = 300.0

    private var thread: Thread? = null
    @Volatile private var running = false

    private var smoothedRpm = -1f

    /** Update engine geometry + expected RPM band (called when settings change). */
    fun setEngine(cylinders: Int, strokes: Int, idleRpm: Int, redlineRpm: Int) {
        val fpr = if (strokes == 2) cylinders.toDouble() else cylinders / 2.0
        firingsPerRev = fpr.coerceAtLeast(0.25)
        // Margin so we don't clip just below idle / just above redline.
        fMinHz = max(8.0, idleRpm / 60.0 * firingsPerRev * 0.7)
        fMaxHz = min((SAMPLE_RATE / DECIM) / 2.5, redlineRpm / 60.0 * firingsPerRev * 1.15)
    }

    @RequiresPermission(Manifest.permission.RECORD_AUDIO)
    fun start() {
        if (running) return
        running = true
        smoothedRpm = -1f
        thread = Thread({ runLoop() }, "AcousticRpm").apply { start() }
    }

    fun stop() {
        running = false
        thread?.join(500)
        thread = null
    }

    @SuppressLint("MissingPermission")
    private fun runLoop() {
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuf <= 0) {
            Log.e(TAG, "AudioRecord unsupported on this device")
            return
        }
        val bufBytes = max(minBuf, READ_CHUNK * 4)
        val recorder = openRecorder(bufBytes) ?: return

        val window = DoubleArray(N)            // circular decimated-sample window
        var pos = 0
        var filled = 0
        var sinceHop = 0
        var accSum = 0.0
        var accCount = 0

        val raw = ShortArray(READ_CHUNK)
        try {
            recorder.startRecording()
            while (running) {
                val read = recorder.read(raw, 0, raw.size)
                if (read <= 0) continue
                for (i in 0 until read) {
                    accSum += raw[i].toDouble()
                    if (++accCount >= DECIM) {
                        // One decimated sample (box low-pass = anti-alias).
                        window[pos] = accSum / DECIM
                        pos = (pos + 1) % N
                        accSum = 0.0; accCount = 0
                        if (filled < N) filled++
                        if (++sinceHop >= HOP && filled >= N) {
                            sinceHop = 0
                            analyze(window, pos)
                        }
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "capture loop error", e)
        } finally {
            try { recorder.stop() } catch (_: Exception) {}
            recorder.release()
        }
    }

    @SuppressLint("MissingPermission")
    private fun openRecorder(bufBytes: Int): AudioRecord? {
        // Prefer UNPROCESSED (raw, no AGC/NS); fall back as availability varies.
        val sources = intArrayOf(
            MediaRecorder.AudioSource.UNPROCESSED,
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.MIC,
        )
        for (src in sources) {
            try {
                val r = AudioRecord(
                    src, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, bufBytes,
                )
                if (r.state == AudioRecord.STATE_INITIALIZED) return r
                r.release()
            } catch (e: Exception) {
                Log.w(TAG, "audio source $src unavailable", e)
            }
        }
        Log.e(TAG, "no usable audio source")
        return null
    }

    /** Run one FFT/HPS analysis on the decimated window starting at [start]. */
    private fun analyze(window: DoubleArray, start: Int) {
        val re = DoubleArray(N)
        val im = DoubleArray(N)
        // Linearise + Hann window + DC removal.
        var mean = 0.0
        for (i in 0 until N) mean += window[(start + i) % N]
        mean /= N
        for (i in 0 until N) {
            val w = 0.5 - 0.5 * Math.cos(2.0 * Math.PI * i / (N - 1))
            re[i] = (window[(start + i) % N] - mean) * w
        }
        Fft.transform(re, im)

        val half = N / 2
        val mag = DoubleArray(half)
        for (i in 0 until half) mag[i] = Math.hypot(re[i], im[i])

        val fs2 = SAMPLE_RATE.toDouble() / DECIM
        val binHz = fs2 / N
        val kMin = max(1, Math.floor(fMinHz / binHz).toInt())
        val kMax = min(half - 1, Math.ceil(fMaxHz / binHz).toInt())
        if (kMax <= kMin + 1) return

        // --- Band-pass isolation (keep ONLY the engine, drop everything else) ---
        // The only frequencies that carry RPM are the firing fundamental
        // [fMin..fMax] and the harmonics HPS relies on (up to MAX_HARMONICS x
        // fMax). Zero every bin outside that passband so out-of-band sound -
        // sub-bass road/exhaust drone & wind buffeting BELOW the band, and
        // voices / wind hiss / cabin music ABOVE it - can't move the peak or
        // lift the noise floor. This is the "isolate the RPM sound, remove all
        // other sounds" stage the dash needs.
        val kHiMask = min(half - 1, MAX_HARMONICS * kMax)
        for (i in 0 until kMin) mag[i] = 0.0
        for (i in kHiMask + 1 until half) mag[i] = 0.0

        // In-band noise floor = median magnitude across the passband. A real
        // engine peak towers over it; broadband noise (wind/road) doesn't.
        val bandLen = kHiMask - kMin + 1
        val bandSorted = DoubleArray(bandLen)
        System.arraycopy(mag, kMin, bandSorted, 0, bandLen)
        bandSorted.sort()
        val noiseFloor = bandSorted[bandLen / 2] + 1e-9
        var peakMag = 0.0
        for (i in kMin..kMax) if (mag[i] > peakMag) peakMag = mag[i]
        val snr = peakMag / noiseFloor

        // Harmonic Product Spectrum (log-sum form) across the firing band.
        var bestK = kMin
        var bestScore = -Double.MAX_VALUE
        var scoreSum = 0.0
        var scoreCount = 0
        val scores = DoubleArray(kMax - kMin + 1)
        for (k in kMin..kMax) {
            val maxH = min(MAX_HARMONICS, (half - 1) / k)
            var s = 0.0
            for (h in 1..maxH) s += ln(mag[h * k] + 1e-9)
            s /= maxH
            scores[k - kMin] = s
            scoreSum += s
            scoreCount++
            if (s > bestScore) { bestScore = s; bestK = k }
        }

        // Parabolic interpolation around the winning bin for sub-bin accuracy.
        var kInterp = bestK.toDouble()
        if (bestK > kMin && bestK < kMax) {
            val a = scores[bestK - kMin - 1]
            val b = scores[bestK - kMin]
            val c = scores[bestK - kMin + 1]
            val denom = (a - 2 * b + c)
            if (denom != 0.0) kInterp = bestK + 0.5 * (a - c) / denom
        }

        val firingHz = kInterp * binHz
        val rpm = (firingHz * 60.0 / firingsPerRev).toFloat()

        // Confidence: how much the winning bin stands out from the band mean.
        val mean2 = scoreSum / scoreCount
        var varSum = 0.0
        for (s in scores) varSum += (s - mean2) * (s - mean2)
        val std = Math.sqrt(varSum / scoreCount) + 1e-9
        val z = (bestScore - mean2) / std
        // Confidence requires BOTH a harmonic series that stands out within the
        // band (z) AND the peak rising clearly above the in-band noise floor
        // (SNR). Limited by the weaker of the two, so a windy/road-noisy frame
        // with no genuine engine peak collapses to ~0 instead of guessing.
        val zConf = (z / 4.0).coerceIn(0.0, 1.0)
        val snrConf = ((snr - SNR_FLOOR) / (SNR_FULL - SNR_FLOOR)).coerceIn(0.0, 1.0)
        val confidence = min(zConf, snrConf).toFloat()

        emit(rpm, confidence, firingHz.toFloat())
    }

    private fun emit(rpmRaw: Float, confidence: Float, firingHz: Float) {
        if (confidence < CONF_GATE) {
            // Not sure enough this window — report low confidence, hold nothing.
            onEstimate(RpmEstimate(-1, confidence, firingHz))
            return
        }
        smoothedRpm = if (smoothedRpm < 0f) {
            rpmRaw
        } else {
            // Slew-limit (engine can't jump arbitrarily fast) then EMA.
            val capped = rpmRaw.coerceIn(smoothedRpm - MAX_SLEW, smoothedRpm + MAX_SLEW)
            smoothedRpm + EMA_ALPHA * (capped - smoothedRpm)
        }
        onEstimate(RpmEstimate(smoothedRpm.toInt().coerceAtLeast(0), confidence, firingHz))
    }

    private companion object {
        const val TAG = "AcousticRpm"
        const val SAMPLE_RATE = 44100
        const val DECIM = 8                       // -> ~5512 Hz working rate
        const val N = 2048                        // FFT size (~371 ms window)
        const val HOP = N / 3                      // ~8 Hz update rate
        const val READ_CHUNK = 2048
        const val MAX_HARMONICS = 5
        const val CONF_GATE = 0.18f
        const val SNR_FLOOR = 3.0                  // peak/median below this = noise
        const val SNR_FULL = 12.0                  // peak/median above this = clean tone
        const val EMA_ALPHA = 0.45f
        const val MAX_SLEW = 1200f                // RPM per analysis frame
    }
}
