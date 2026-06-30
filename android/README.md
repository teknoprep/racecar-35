# RaceDash Mobile (Android)

A standalone, **phone-only** track dash + data logger for non-MS3Pro cars. Uses
the phone's own sensors — no car wiring required:

| Channel | Source on the phone |
| --- | --- |
| GPS (position / speed / heading / sats) | `FusedLocationProvider` + `GnssStatus` |
| Gyro + accelerometer (lateral / longitudinal G) | `SensorManager` (TYPE_GYROSCOPE / TYPE_ACCELEROMETER) |
| **RPM** | **Microphone** — acoustic estimate from the engine's firing frequency, configured by cylinders + stroke |
| Lap timing | GPS start/finish geofence + ghost-lap predictive engine (ported from the firmware dash) |
| Recording | NDJSON to app storage (same descriptive-key shape the web analyzer consumes) |

## How the acoustic RPM works
A 4-stroke engine fires `cylinders/2` times per crank revolution, so the exhaust
note has a fundamental at `f_fire = (RPM/60) × (cylinders/2)` Hz — **hundreds of
Hz, not thousands** (an 8000-RPM four-cylinder is ~267 Hz; an F1 V8 at 18,000 RPM
is ~1.2 kHz). The "high-pitched scream" your ear hears is the *harmonics*, not the
fundamental.

Pipeline (`sensors/AcousticRpm.kt`): `AudioRecord` (UNPROCESSED, no AGC) →
box-decimate → Hann window → FFT → **Harmonic Product Spectrum** over the firing
band (rejects 2×/½× octave errors) → parabolic peak interp → confidence gate →
EMA + slew-limit smoothing. Set **Cylinders / Stroke / Idle / Redline** in
Settings — these define the search band and the Hz→RPM conversion.

> Honest expectation: a *rough* tach (≈±200–500 RPM in good conditions), not
> logging-grade. It's the price of "no extra hardware." Confidence is shown live
> next to the RPM number (`mic NN%`); low-confidence frames read `----`.

## Build
No Gradle wrapper jar is committed — open the **`android/`** folder in
**Android Studio** (Hedgehog or newer) and let it sync; it provisions Gradle
8.7 from `gradle/wrapper/gradle-wrapper.properties` automatically. Then Run on a
device.

CLI alternative (with a local Gradle 8.7 + Android SDK installed):
```bash
cd android
gradle wrapper          # one-time: generates the wrapper jar/scripts
./gradlew assembleDebug # -> app/build/outputs/apk/debug/app-debug.apk
```

Requirements: JDK 17, Android SDK 34, a device on Android 8.0+ (minSdk 26) with
Google Play Services (for fused location). Grant **Location** + **Microphone**
when prompted.

## Project layout
```
app/src/main/java/com/racedash/mobile/
  data/      Tracks, Settings (SharedPreferences), SessionRecorder (NDJSON)
  sensors/   LocationProvider, MotionProvider, AcousticRpm (+ Fft)
  lap/       LapTimer (ghost-lap predictive)
  vm/        DashViewModel (sensor fusion, 20 Hz state + logging), DashState
  ui/        MainActivity, DashScreen, SettingsScreen, TrackPickerScreen, theme/
```

## Recorded data
`<app external files>/sessions/session_<unixtime>_<track>.ndjson`, one JSON
object per ~50 ms:
```json
{"t":1714942567.234,"fix":3,"sats":12,"lat":40.123456,"lon":-74.123450,
 "alt_m":123.4,"speed_mph":67.5,"heading_deg":123.4,"rpm":5800,"rpm_conf":0.74,
 "ax":-0.92,"ay":0.31,"az":0.05,"gx":1.2,"gy":-3.4,"gz":0.8,"lap":2,"lap_ms":48213}
```
Pull via `adb pull` or USB MTP, or wire up the upload endpoint later.

## Not done yet (deliberately, v0.1.0)
- Foreground service for screen-off / backgrounded recording (currently the
  screen is kept on and the app must stay foreground).
- Cloud upload (the NDJSON shape is already analyzer-compatible).
- Editable track list / on-device start-finish naming.
- On-device validation of the acoustic estimator against a known tach.
