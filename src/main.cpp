// racecar-35 dash firmware — GPS + tach + IMU to dash bridge
//
// Reads u-blox GNSS over Serial2 (pin 7=RX2, pin 8=TX2), an opto-
// isolated tach pulse on pin 9 (FlexPWM input capture / FreqMeasure),
// and an MPU-6050 IMU over Wire (pin 18=SDA, pin 19=SCL),
// then forwards all three to the CrowPanel ESP32 dash over Serial3 (pin
// 14=TX3, pin 15=RX3 -> CrowPanel UART0).
//
// Wire format on Serial3, 115200 8N1, '\n'-terminated, line types:
//
//   GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>,<gps_status>
//   ENG,<rpm>,<oil_psi_x10>,<coolant_f_x10>
//   ECU,<rpm>,<clt_f_x10>,<map_x10>,<tps_x10>,<afr_x10>,<iat_f_x10>,<bat_x10>   [planned]
//   IMU,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
//   SD,REC,<0|1>,<filename>,<samples>     emitted on session open/close + 1 Hz during
//   CLD,<live_ok>,<queue_depth>          emitted whenever either field changes (cloud status)
//
// Example: GPS,3,12,40.123456,-74.123456,67.5,123.4,2
//          ENG,3450,650,2185
//          ECU,3450,1850,420,150,145,720,138
//          IMU,0.02,-0.98,0.12,1.3,-0.5,0.2
//
// ECU is planned — emitted once the MS3 CAN transceiver (SN65HVD230) is wired
// to CAN1 (pin 22 TX, pin 23 RX) and FlexCAN decoding lands here. Fields are
// MS3-derived: coolant, MAP, TPS, AFR, IAT, battery. -1 in any slot means
// "MS3 doesn't broadcast / didn't broadcast yet". The dash uses these when
// settings.sensor_type == 1 (MegaSquirt); ENG fields are used when == 0 (Direct).
//
// GPS fields:
//   fix          0=None 1=DR 2=2D 3=3D 4=3D+DR 5=Time-only
//   sats         satellites in view
//   lat,lon      decimal degrees, 6 decimals
//   speed_mph    ground speed in mph, 1 decimal
//   heading_deg  heading of motion in degrees, 1 decimal
//   gps_status   0=OFF 1=RAW 2=OK 3=STALE (see gpsStatus())
//
// ENG fields:
//   rpm          integer engine RPM, derived from tach pulse frequency
//                with RPM_PULSES_PER_REV. Reports 0 when no pulses for
//                ~750 ms (engine off / wire disconnected).
//   oil_psi_x10  oil pressure in PSI * 10. Generic 5V 0.5-4.5V transducer
//                on pin A2 through a 10k/20k divider. -1 on sensor fault
//                (open or short — out-of-band voltage at the input).
//   coolant_f_x10 coolant temperature in degF * 10. NTC thermistor on
//                pin A3 with a 150 ohm pullup to 3.3V (sized for the
//                VDO 1600-22 ohm curve). -1 on open/short.
//
// IMU fields (averaged over samples since last emit):
//   ax/ay/az     accelerometer in g, 2 decimals (±2g range, 16384 LSB/g)
//   gx/gy/gz     gyroscope in deg/s, 1 decimal (±250°/s range, 131 LSB/°s)
//
// MPU-6050 mounting note: with the GY-521 board flat, component-side up,
// pins toward the rear of the car:
//   +X = forward (longitudinal, braking/accel)
//   +Y = right (lateral, right-hand turns read negative ay)
//   +Z = up (vertical)

#include <Arduino.h>
#include <Wire.h>
#include "FXUtil.h"        // FlasherX: in-application reflash for Teensy 4.x
extern "C" {
  #include "FlashTxx.h"     // low-level flash primitives (firmware_buffer_init, etc.)
}

// Compile-time firmware version. Increment via the release process when
// publishing new firmware artifacts to firmware/manifest.json on main.
// Format: "MAJOR.MINOR.PATCH" — dash compares versions as semver strings.
// Teensy version is bumped in lock-step with the dash via scripts/release.sh.
#define FIRMWARE_VERSION "0.1.9"

#include <SPI.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#include <SdFat.h>
#include <TimeLib.h>
#include <SparkFun_u-blox_GNSS_Arduino_Library.h>
#include <FreqMeasure.h>

namespace {
  HardwareSerial& GPS_SERIAL  = Serial2;            // pins 7 (RX2), 8 (TX2)
  HardwareSerial& DASH_SERIAL = Serial3;            // pin 14 (TX3) -> CrowPanel UART0 RX
  constexpr uint32_t GPS_BAUD_PRIMARY  = 38400;     // SparkFun GPS-RTK boards default
  constexpr uint32_t GPS_BAUD_FALLBACK = 9600;      // u-blox factory default
  constexpr uint32_t DASH_BAUD         = 921600;   // up from 115200 — 8× faster for
                                                  // telemetry + file uploads. Both sides
                                                  // must agree; if you mix old + new
                                                  // firmware versions you'll see garbage.
  // NEO-M9N supports 25 Hz max nav rate. We push that hard for high-rate
  // logging and live telemetry. M8-class modules cap at 18 Hz — drop this
  // to 18 if you ever swap chips.
  constexpr uint8_t  NAV_RATE_HZ       = 25;

  // Tach input: opto-isolated pulse on pin 9 (FreqMeasure-capable on T4.x).
  // PULSES_PER_REV depends on where the tap is taken — calibrate by reading
  // the dash at a known idle (e.g. 800 RPM should display ~800). Common
  // values for a 4-cyl 4-stroke:
  //   2.0  = wasted-spark coil-negative, distributor coil-neg, most ECU tach
  //   1.0  = single COP coil trigger
  //   0.5  = once-per-2-revs cam-position pulse
  constexpr float    RPM_PULSES_PER_REV = 2.0f;
  constexpr uint32_t RPM_TIMEOUT_MS     = 750;      // no pulses in this window -> RPM = 0

  // ---- Oil pressure: generic 5V 0.5-4.5V transducer, 150 PSI full scale ----
  // Wired through a 10k / 20k voltage divider so the 0.5-4.5V sensor output
  // arrives at the ADC pin as 0.33-3.00V (safe for the not-5V-tolerant Teensy).
  constexpr int   OIL_ADC_PIN        = A2;          // physical pin 16
  constexpr float OIL_DIVIDER_RATIO  = 2.0f / 3.0f; // V_adc = V_sensor * R2/(R1+R2)
  constexpr float OIL_V_AT_ZERO_PSI  = 0.5f;        // sensor V at 0 PSI
  constexpr float OIL_V_AT_FULL_PSI  = 4.5f;        // sensor V at full scale
  constexpr float OIL_PSI_FULL_SCALE = 150.0f;      // PSI at OIL_V_AT_FULL_PSI

  // ---- Coolant temp: MH 1600-22 ohm VDO-curve NTC thermistor ----
  // 150 ohm pullup to 3.3V (sized for ~22-700 ohm working range = optimal
  // ADC resolution from 100F to 250F). Steinhart-Hart coefficients fitted
  // to the published 3-point VDO curve (100F=700R, 180F=110R, 250F=22R).
  // CALIBRATE FOR YOUR SPECIFIC SENDER: measure R at 3 known temperatures
  // (ice bath, room, boiling) and re-fit via thinksrs.com NTC calculator.
  constexpr int   COOLANT_ADC_PIN    = A3;          // physical pin 17
  constexpr float COOLANT_PULLUP_OHM = 150.0f;
  constexpr float COOLANT_VREF       = 3.3f;
  constexpr float COOLANT_SH_A       = 2.097e-3f;
  constexpr float COOLANT_SH_B       = 1.346e-4f;
  constexpr float COOLANT_SH_C       = 8.387e-7f;

  // EMA smoothing for both analog sensors. Alpha=0.2 -> ~5-sample time
  // constant at 25 Hz = ~200 ms tau. Kills the per-sample ADC jitter that
  // cheap industrial transducers exhibit without lagging real changes.
  constexpr float SENSOR_EMA_ALPHA   = 0.2f;
}

SFE_UBLOX_GNSS myGNSS;

// ---------------------------------------------------------------------------
// Inbound commands from the CrowPanel dash (Serial3 RX).
// Wire format: line-oriented, '\n'-terminated:
//   REC,<0|1>          start/stop recording
//   TRACK,<name>       set the current track name (sent right before REC,1)
//
// SD logging: on REC,1 we open /sessions/session_<unix>_<track>.ndjson and
// append one NDJSON sample per emit window (25 Hz). On REC,0 we flush +
// close. Cloud upload runs alongside — live POSTs during the session in
// Live mode, whole-file POST on close in AfterRace mode. Failed uploads
// get queued to /queue/ and retried when the link comes back up.
// ---------------------------------------------------------------------------
static bool     recording_active = false;
static char     current_track[32] = "UNKNOWN";
static uint32_t session_start_ms = 0;
static uint32_t session_start_unix = 0;   // RTC epoch at REC,1 (0 if RTC unset)

// Cloud / live-stream state lives up here because closeSession() (defined in
// the SD section below) references it. Function definitions are in the cloud
// section further down.
static struct {
    char     host[64]   = "";
    uint16_t port       = 80;
    uint8_t  proto      = 0;     // 0=HTTP, 1=HTTPS (NYI), 2=FTP (NYI)
    uint8_t  stream     = 0;     // 0=Live, 1=AfterRace
    char     email[64]  = "";
    char     api_key[64] = "";
    bool     rec_sd     = true;
    bool     rec_cl     = false;
    uint8_t  inet       = 0;     // 0=Ethernet, 1=WiFi (mirror of dash setting)
} g_cfg;

// Set when the dash sends UPLOAD,CANCEL. Volatile across reboot — deliberately
// in-RAM only so a reboot is the only way to re-enable uploads.
static volatile bool uploads_disabled = false;
// Set true while httpPost() is actively pushing a file; checked from
// pumpDashCommands -> handleDashCommand so a CANCEL during the loop aborts
// the current connection on the next chunk boundary.
static volatile bool upload_in_progress = false;
static volatile bool upload_cancel_pending = false;

static uint8_t  live_buf[2048];
static size_t   live_buf_n          = 0;
static uint32_t live_last_flush_ms  = 0;
static bool     live_failed_session = false;   // give up on live POSTs for this session
static bool     live_status_last_ok = false;   // last successful POST flag
static uint32_t queue_depth         = 0;       // updated by scanQueue()
// Active timezone id sent by the dash (e.g. "ET", "PT", "UTC"). Used today
// only for logging; future SD-filename / cloud-metadata code can consult it.
// The Teensy's RTC and the wire-format TIME line are always UTC.
static char     current_tz[8]    = "UTC";

static void formatSDCard();   // defined below, after SD section
static void openSession();    // forward decl — defined in SD section
static void closeSession();
static void writeSessionSample(uint8_t fix, uint8_t sats,
                               float lat_deg, float lon_deg,
                               float mph, float hdg_deg,
                               uint16_t rpm, int16_t oil_x10, int16_t cool_x10,
                               float ax, float ay, float az,
                               float gx, float gy, float gz);
static void handleCfgLine(const String& line);   // cloud section below

// TimeLib sync provider — reads the Teensy 4.1 built-in RTC.
static time_t getTeensyTime() { return Teensy3Clock.get(); }

static void handleDashCommand(const String& line) {
    if (line.startsWith("REC,")) {
        const int v = line.substring(4).toInt();
        const bool now = (v != 0);
        if (now != recording_active) {
            recording_active = now;
            if (now) {
                session_start_ms   = millis();
                session_start_unix = (uint32_t)::now();
                Serial.printf("[teensy] REC START — track=\"%s\", t=%lums unix=%lu\n",
                              current_track, (unsigned long)session_start_ms,
                              (unsigned long)session_start_unix);
                openSession();
            } else {
                const uint32_t dur = millis() - session_start_ms;
                closeSession();
                Serial.printf("[teensy] REC STOP — duration=%lums\n",
                              (unsigned long)dur);
            }
        }
    } else if (line.startsWith("TRACK,")) {
        const String name = line.substring(6);
        strncpy(current_track, name.c_str(), sizeof(current_track) - 1);
        current_track[sizeof(current_track) - 1] = '\0';
        Serial.printf("[teensy] track set to \"%s\"\n", current_track);
    } else if (line == "SDFORMAT") {
        Serial.println(F("[teensy] SD format requested by dash"));
        formatSDCard();
    } else if (line.startsWith("SETTIME,")) {
        const unsigned long t = strtoul(line.c_str() + 8, nullptr, 10);
        Teensy3Clock.set((time_t)t);
        setTime((time_t)t);
        Serial.printf("[teensy] RTC set to %lu\n", t);
    } else if (line.startsWith("TZ,")) {
        const String tz = line.substring(3);
        strncpy(current_tz, tz.c_str(), sizeof(current_tz) - 1);
        current_tz[sizeof(current_tz) - 1] = '\0';
        Serial.printf("[teensy] timezone set to \"%s\"\n", current_tz);
    } else if (line.startsWith("CFG,")) {
        handleCfgLine(line);   // defined in cloud section below
    } else if (line == "UPLOAD,CANCEL") {
        // Dash requested cancel-all. Latch the disabled flag (clears only
        // on reboot) and signal the in-progress upload loop to bail out at
        // the next chunk boundary.
        uploads_disabled = true;
        if (upload_in_progress) upload_cancel_pending = true;
        Serial.println(F("[cloud] UPLOAD,CANCEL received — uploads disabled until reboot"));
    } else if (line == "VER?") {
        // Dash asked us to re-announce our firmware version. The dash's STATUS
        // page does this whenever it opens, so a freshly-booted dash that
        // missed our boot-time emit can catch up immediately.
        DASH_SERIAL.printf("VER,teensy,%s\n", FIRMWARE_VERSION);
    } else if (line == "FWUPDATE") {
        // Dash kicking off a Teensy OTA. We're about to hand the UART over to
        // the FlasherX hex-receive loop, which is BLOCKING until EOF or error.
        // No telemetry / commands flow during that window. Dash is expected
        // to send Intel HEX lines (':...\n') for the whole image, then either
        // succeed (we never return — flash_move reboots us into new firmware)
        // or fail (we resume normal operation + emit FW,ERR for the dash).
        uint32_t buffer_addr = 0, buffer_size = 0;
        if (firmware_buffer_init(&buffer_addr, &buffer_size) == 0) {
            DASH_SERIAL.println(F("FW,ERR,buffer_init_failed"));
            return;
        }
        Serial.printf("[fwupdate] buffer @ 0x%08lX, size=%lu\n",
                      (unsigned long)buffer_addr, (unsigned long)buffer_size);
        DASH_SERIAL.printf("FW,READY,%lu\n", (unsigned long)buffer_size);
        // Per-line ACK protocol. Each Intel HEX line is parsed, written to
        // the flash buffer, then we send 'A\n' on DASH_SERIAL. The dash waits
        // for that ACK before sending the next line, which gives us reliable
        // flow control across arbitrary flash erase stalls (~100 ms each) at
        // 921 600 baud. The earlier byte-streaming version saw mid-stream
        // byte loss — with ACKs, that's structurally impossible.
        update_firmware_acked(&DASH_SERIAL, &Serial, &DASH_SERIAL,
                              buffer_addr, buffer_size);
        // If we get here, the update failed. Free the buffer and report.
        firmware_buffer_free(buffer_addr, buffer_size);
        DASH_SERIAL.println(F("FW,ERR,update_failed"));
    } else if (line.length() > 0) {
        Serial.printf("[teensy] unknown dash cmd: %s\n", line.c_str());
    }
}

static void pumpDashCommands() {
    static String cmdBuf;
    while (DASH_SERIAL.available()) {
        const char c = (char)DASH_SERIAL.read();
        if (c == '\r') continue;
        if (c == '\n') { handleDashCommand(cmdBuf); cmdBuf = ""; }
        else if (cmdBuf.length() < 256) { cmdBuf += c; }
        else { cmdBuf = ""; }
    }
}

// GPS health tracking. Updated each loop:
//   gnss_lib_ok       = did myGNSS.begin() succeed at boot?
//   gnss_last_fresh_ms = millis() of the most recent successful getPVT(), OR
//                        the most recent raw byte arriving on Serial2 when
//                        the lib couldn't connect.
//   gnss_raw_bytes    = total raw bytes drained from Serial2 when lib NOT ok.
static bool     gnss_lib_ok       = false;
static uint32_t gnss_last_fresh_ms = 0;
static uint32_t gnss_raw_bytes    = 0;

static bool tryConnectGNSS(uint32_t baud) {
    GPS_SERIAL.begin(baud);
    delay(50);
    return myGNSS.begin(GPS_SERIAL);
}

// Status code matching the wire format documented at the top of the file.
static uint8_t gpsStatus() {
    const uint32_t age = millis() - gnss_last_fresh_ms;
    if (gnss_lib_ok)        return (age < 1500) ? 2 /*OK*/   : 3 /*STALE*/;
    if (gnss_raw_bytes > 0) return (age < 1500) ? 1 /*RAW*/  : 0 /*OFF*/;
    return 0; // OFF — never seen a byte
}

// Tach state — averaged over whatever pulses arrived in the last sampling
// window. FreqMeasure is interrupt-driven on pin 9 input capture; we just
// drain its FIFO in loop().
static double   rpm_pulse_sum   = 0.0;     // sum of period-counter ticks
static uint32_t rpm_pulse_count = 0;       // number of periods accumulated
static uint16_t rpm_current     = 0;       // last computed RPM
static uint32_t rpm_last_pulse_ms = 0;     // millis() of most recent pulse

static void pumpTach() {
    while (FreqMeasure.available()) {
        rpm_pulse_sum   += FreqMeasure.read();
        rpm_pulse_count += 1;
        rpm_last_pulse_ms = millis();
    }
}

// ---------------------------------------------------------------------------
// Analog engine sensors: oil pressure (A2) and coolant temp (A3).
//
// Both functions return value * 10 to fit a 0.1-unit resolution into an int,
// or -1 on apparent fault (signal outside the band a working sensor can
// produce). The EMA smoothing state is function-local static, so calling
// these once per emit window (25 Hz) gives a ~200 ms time constant.
// ---------------------------------------------------------------------------
static int16_t readOilPsiX10() {
    const int   raw      = analogRead(OIL_ADC_PIN);
    const float v_adc    = (float)raw * (3.3f / 4095.0f);
    const float v_sensor = v_adc / OIL_DIVIDER_RATIO;
    // Sensor is spec'd 0.5-4.5V. <0.3V = open/no power; >4.7V = short to 5V.
    if (v_sensor < 0.3f || v_sensor > 4.7f) return -1;

    const float psi_scale = OIL_PSI_FULL_SCALE / (OIL_V_AT_FULL_PSI - OIL_V_AT_ZERO_PSI);
    const float psi       = (v_sensor - OIL_V_AT_ZERO_PSI) * psi_scale;

    static float ema = 0.0f;
    ema = ema * (1.0f - SENSOR_EMA_ALPHA) + psi * SENSOR_EMA_ALPHA;
    int v = (int)(ema * 10.0f + 0.5f);
    if (v < 0) v = 0;
    if (v > 30000) v = 30000;
    return (int16_t)v;
}

static int16_t readCoolantFx10() {
    const int raw = analogRead(COOLANT_ADC_PIN);
    // Saturated low = thermistor open; saturated high = shorted to GND.
    if (raw <= 4 || raw >= 4091) return -1;

    const float v = (float)raw * (COOLANT_VREF / 4095.0f);
    // Divider: V = Vref * R / (R + Rpullup)  ->  R = Rpullup * V / (Vref - V)
    const float R = COOLANT_PULLUP_OHM * v / (COOLANT_VREF - v);
    if (R < 5.0f || R > 100000.0f) return -1;

    const float lnR  = logf(R);
    const float invT = COOLANT_SH_A + COOLANT_SH_B * lnR
                                    + COOLANT_SH_C * lnR * lnR * lnR;
    if (invT <= 0.0f) return -1;
    const float T_K = 1.0f / invT;
    const float T_F = (T_K - 273.15f) * (9.0f / 5.0f) + 32.0f;
    if (T_F < -40.0f || T_F > 400.0f) return -1;

    static float ema = 0.0f;
    static bool  ema_init = false;
    if (!ema_init) { ema = T_F; ema_init = true; }
    else           { ema = ema * (1.0f - SENSOR_EMA_ALPHA) + T_F * SENSOR_EMA_ALPHA; }
    return (int16_t)(ema * 10.0f + 0.5f);
}

// ---------------------------------------------------------------------------
// IMU — MPU-6050 over Wire (SDA=18, SCL=19). Direct register reads; no
// external library needed. Samples are accumulated every loop iteration and
// averaged into the next emit window, matching the tach averaging pattern.
//
// Rate-limited to one I2C burst every 4 ms (~250 Hz), giving ~10 samples per
// 40 ms GPS window. The MPU-6050 internal ODR with DLPF=0x03 is 1 kHz so
// polling at 250 Hz never re-reads stale data.
// ---------------------------------------------------------------------------
constexpr uint8_t MPU6050_ADDR = 0x68;   // AD0 tied to GND

static bool imu_present = false;

static struct {
    float   ax_sum = 0, ay_sum = 0, az_sum = 0;
    float   gx_sum = 0, gy_sum = 0, gz_sum = 0;
    uint32_t n     = 0;
    // Last averaged output (used by emitToDash):
    float   ax = 0, ay = 0, az = 0;
    float   gx = 0, gy = 0, gz = 0;
} imu;

static bool setupIMU() {
    Wire.begin();
    Wire.setClock(400000);
    // Wake: clear SLEEP bit in PWR_MGMT_1 (reg 0x6B)
    Wire.beginTransmission(MPU6050_ADDR);
    Wire.write(0x6B);
    Wire.write(0x00);
    if (Wire.endTransmission(true) != 0) {
        Serial.println(F("MPU-6050 NOT found on Wire (SDA=18, SCL=19)"));
        return false;
    }
    // DLPF = 44 Hz bandwidth (CONFIG reg 0x1A = 0x03) — attenuates
    // chassis vibration above the GPS update rate.
    Wire.beginTransmission(MPU6050_ADDR);
    Wire.write(0x1A);
    Wire.write(0x03);
    Wire.endTransmission(true);
    Serial.println(F("MPU-6050 ready (±2g / ±250 deg/s, DLPF 44 Hz)"));
    return true;
}

// Read one 14-byte burst from the MPU-6050 and accumulate into the averager.
// Returns silently if the device is absent or the burst is incomplete.
static void readIMU() {
    if (!imu_present) return;
    static uint32_t lastReadMs = 0;
    const uint32_t now = millis();
    if (now - lastReadMs < 4) return;   // cap at ~250 Hz
    lastReadMs = now;

    Wire.beginTransmission(MPU6050_ADDR);
    Wire.write(0x3B);   // ACCEL_XOUT_H — burst start
    Wire.endTransmission(false);
    if (Wire.requestFrom(MPU6050_ADDR, (uint8_t)14, (uint8_t)true) < 14) return;

    const int16_t raw_ax = (int16_t)((Wire.read() << 8) | Wire.read());
    const int16_t raw_ay = (int16_t)((Wire.read() << 8) | Wire.read());
    const int16_t raw_az = (int16_t)((Wire.read() << 8) | Wire.read());
    Wire.read(); Wire.read();   // temp — discard
    const int16_t raw_gx = (int16_t)((Wire.read() << 8) | Wire.read());
    const int16_t raw_gy = (int16_t)((Wire.read() << 8) | Wire.read());
    const int16_t raw_gz = (int16_t)((Wire.read() << 8) | Wire.read());

    imu.ax_sum += raw_ax * (1.0f / 16384.0f);
    imu.ay_sum += raw_ay * (1.0f / 16384.0f);
    imu.az_sum += raw_az * (1.0f / 16384.0f);
    imu.gx_sum += raw_gx * (1.0f / 131.0f);
    imu.gy_sum += raw_gy * (1.0f / 131.0f);
    imu.gz_sum += raw_gz * (1.0f / 131.0f);
    imu.n++;
}

// Average accumulated samples into imu.ax/ay/az/gx/gy/gz and reset.
// Called from emitToDash() — safe to call even if n==0 (retains last values).
static void flushImu() {
    if (imu.n == 0) return;
    const float inv = 1.0f / (float)imu.n;
    imu.ax = imu.ax_sum * inv;  imu.ax_sum = 0;
    imu.ay = imu.ay_sum * inv;  imu.ay_sum = 0;
    imu.az = imu.az_sum * inv;  imu.az_sum = 0;
    imu.gx = imu.gx_sum * inv;  imu.gx_sum = 0;
    imu.gy = imu.gy_sum * inv;  imu.gy_sum = 0;
    imu.gz = imu.gz_sum * inv;  imu.gz_sum = 0;
    imu.n  = 0;
}

// ---------------------------------------------------------------------------
// Ethernet — W5500 on SPI0.
//   CS=10  MOSI=11  MISO=12  SCK=13  /INT=5  /RST=6
// DHCP at boot; lease maintained by Ethernet.maintain() in loop().
// Emits ETH,<ip> to dash when the assigned address changes.
// ---------------------------------------------------------------------------
static constexpr int ETH_CS_PIN  = 10;
static constexpr int ETH_RST_PIN =  6;
// Locally-administered MAC (bit 1 of first byte = 1). Avoids any chance of
// colliding with a real OUI; some routers / DHCP servers reject requests
// from MACs in registered OUI blocks if the device isn't recognised.
static const byte    ETH_MAC[]   = { 0x02, 0xE9, 0xE5, 0x00, 0x01, 0x35 };
static char          eth_ip_str[16] = "0.0.0.0";
static bool          eth_hw_present = false;   // true only if W5500 chip was detected at boot

// Cached boot-time diagnostics so the periodic [eth] debug print (in loop)
// can keep showing what happened at boot without re-running the SPI probes.
static const char*   eth_chip_name   = "?";       // "NONE" | "W5500" | "W5200" | "W5100"
static const char*   eth_link_boot   = "?";       // "UP" | "DOWN" | "(no chip)"
static const char*   eth_dhcp_result = "skipped"; // "OK" | "failed" | "(no link)" | "(no chip)"

// Raw SPI read of the W5500 VERSIONR register (0x0039 in common block).
// Bypasses the Ethernet library to give us the unfiltered byte the chip
// returned. Result is cached in eth_raw_versionr for the periodic [eth-dbg]
// printout. Helps distinguish between "MISO disconnected" (0xFF), "MISO
// shorted" (0x00), "wrong chip" (something else), and "W5500 OK" (0x04).
static uint8_t eth_raw_versionr = 0xAA;   // sentinel "not yet probed"
static char    eth_miso_test[40] = "(not run)";

// Tests the MISO wire (Teensy pin 12) by toggling the internal pull and
// reading. If the wire is properly connected to a driven W5500 output, the
// pull won't dominate. If the wire is dangling, the pull will dominate and
// we'll see the pull's value reflected in the read.
//
// Interpretation:
//   "CSlo:U=1 D=0  CShi:U=1 D=0"  → MISO is FLOATING (pull dominates always)
//                                    → MISO wire is broken, OR CS isn't
//                                      reaching the W5500 (chip stays in high-Z)
//   "CSlo:U=X D=X  CShi:U=1 D=0"  → MISO is driven when CS low (correct!)
//                                    → if rawVERSIONR is still wrong, it's a
//                                      clocking issue (SCK/MOSI)
//   "CSlo:U=0 D=0  CShi:U=0 D=0"  → MISO shorted to GND
//   "CSlo:U=1 D=1  CShi:U=1 D=1"  → MISO shorted to VCC
static void misoPinDiagnostic() {
    // Pin 12 is MISO on Teensy 4.1 SPI0.
    constexpr int MISO_PIN = 12;
    pinMode(ETH_CS_PIN, OUTPUT);

    auto sample = [](int pin, int mode) {
        pinMode(pin, mode);
        delayMicroseconds(50);
        return digitalRead(pin);
    };

    // CS HIGH (chip de-selected → MISO should be in high-Z, pull dominates)
    digitalWrite(ETH_CS_PIN, HIGH);
    delayMicroseconds(20);
    int hiU = sample(MISO_PIN, INPUT_PULLUP);
    int hiD = sample(MISO_PIN, INPUT_PULLDOWN);

    // CS LOW (chip selected → MISO should be driven by chip)
    digitalWrite(ETH_CS_PIN, LOW);
    delayMicroseconds(20);
    int loU = sample(MISO_PIN, INPUT_PULLUP);
    int loD = sample(MISO_PIN, INPUT_PULLDOWN);

    digitalWrite(ETH_CS_PIN, HIGH);
    pinMode(MISO_PIN, INPUT);

    snprintf(eth_miso_test, sizeof(eth_miso_test),
             "CSlo:U=%d,D=%d  CShi:U=%d,D=%d", loU, loD, hiU, hiD);
    Serial.printf("[eth] miso pin test: %s\n", eth_miso_test);
}

static void rawProbeW5500() {
    SPI.begin();
    pinMode(ETH_CS_PIN, OUTPUT);
    digitalWrite(ETH_CS_PIN, HIGH);
    delayMicroseconds(5);

    // Drop to 1 MHz for the probe — slow enough to tolerate marginal
    // breadboard connections / long jumpers. W5500 is happy at any speed
    // up to ~80 MHz.
    SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
    digitalWrite(ETH_CS_PIN, LOW);
    delayMicroseconds(1);
    SPI.transfer(0x00);     // VERSIONR address high byte
    SPI.transfer(0x39);     // VERSIONR address low byte
    SPI.transfer(0x00);     // control: common block, read, VDM
    eth_raw_versionr = SPI.transfer(0x00);   // <-- the byte we care about
    digitalWrite(ETH_CS_PIN, HIGH);
    SPI.endTransaction();

    // Try 3 reads — if it's flaky we'll see varying values. All identical
    // means the bus is stable; varying means there's a marginal connection.
    uint8_t r2 = 0xAA, r3 = 0xAA;
    SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
    digitalWrite(ETH_CS_PIN, LOW);
    delayMicroseconds(1);
    SPI.transfer(0x00); SPI.transfer(0x39); SPI.transfer(0x00);
    r2 = SPI.transfer(0x00);
    digitalWrite(ETH_CS_PIN, HIGH);
    SPI.endTransaction();
    delayMicroseconds(50);
    SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
    digitalWrite(ETH_CS_PIN, LOW);
    delayMicroseconds(1);
    SPI.transfer(0x00); SPI.transfer(0x39); SPI.transfer(0x00);
    r3 = SPI.transfer(0x00);
    digitalWrite(ETH_CS_PIN, HIGH);
    SPI.endTransaction();

    Serial.printf("[eth] raw VERSIONR=0x%02X (3 reads: 0x%02X,0x%02X,0x%02X)  (W5500=0x04, none=0xFF/0x00)\n",
                  eth_raw_versionr, eth_raw_versionr, r2, r3);
}

static void setupEthernet() {
    pinMode(ETH_RST_PIN, OUTPUT);
    digitalWrite(ETH_RST_PIN, LOW);   delay(25);
    digitalWrite(ETH_RST_PIN, HIGH);  delay(200);  // W5500 PLL lock up to 150 ms after RST

    // MISO pin diagnostic — checks whether MISO is being driven by the chip
    // or just floating. Runs first because it's the most likely failure mode.
    misoPinDiagnostic();

    // Raw SPI probe BEFORE the Ethernet library probe — gives us a ground-truth
    // read of MISO so we can tell wiring problems apart from chip problems.
    rawProbeW5500();

    Ethernet.init(ETH_CS_PIN);

    // CRITICAL: Ethernet.hardwareStatus() returns NoHardware UNTIL Ethernet.begin()
    // has run, because chip detection happens inside W5100.init() which is only
    // called from begin(). So we have to call begin() first, then interpret the
    // results. begin() also performs DHCP at the same time — no separate API.
    //
    // Timeout: 10 s lease + 4 s per-response. Most DHCP servers reply within
    // 1-2 s, but some routers under load take 4-6 s. 10 s gives plenty of
    // headroom while still keeping boot under ~12 s if DHCP fails entirely.
    Serial.print(F("[eth] DHCP..."));
    const int begin_result = Ethernet.begin(const_cast<byte*>(ETH_MAC), 10000, 4000);

    const EthernetHardwareStatus hw = Ethernet.hardwareStatus();
    eth_chip_name = (hw == EthernetW5500) ? "W5500" :
                    (hw == EthernetW5200) ? "W5200" :
                    (hw == EthernetW5100) ? "W5100" : "NONE";

    if (hw == EthernetNoHardware) {
        eth_link_boot   = "(no chip)";
        eth_dhcp_result = "(no chip)";
        Serial.println(F(" no chip detected"));
        return;
    }
    eth_hw_present = true;

    const EthernetLinkStatus lnk = Ethernet.linkStatus();
    eth_link_boot = (lnk == LinkON)  ? "UP" :
                    (lnk == LinkOFF) ? "DOWN" : "unknown";

    if (begin_result == 0) {
        eth_dhcp_result = (lnk == LinkOFF) ? "(no link)" : "failed";
        Serial.printf(" failed  (chip=%s, link=%s)\n", eth_chip_name, eth_link_boot);
    } else {
        eth_dhcp_result = "OK";
        IPAddress ip = Ethernet.localIP();
        snprintf(eth_ip_str, sizeof(eth_ip_str), "%d.%d.%d.%d",
                 ip[0], ip[1], ip[2], ip[3]);
        Serial.printf(" OK  IP: %s  (chip=%s, link=%s)\n",
                      eth_ip_str, eth_chip_name, eth_link_boot);
    }
}

// ---------------------------------------------------------------------------
// NTP — query a public time server once at boot to set the Teensy 4.1 RTC.
// Uses 0.pool.ntp.org by default. The RTC drifts only ~50 ppm and a track
// session lasts a few hours at most, so a single boot sync is enough.
// (For long-running deployments, we'd add a periodic non-blocking re-sync.)
// ---------------------------------------------------------------------------
constexpr const char* NTP_SERVER       = "0.pool.ntp.org";
constexpr uint16_t    NTP_LOCAL_PORT   = 8888;
constexpr uint16_t    NTP_PACKET_SIZE  = 48;
constexpr uint32_t    NTP_TO_UNIX_SECS = 2208988800UL;  // 1900→1970 epoch delta

static EthernetUDP ntpUdp;
static const char* ntp_status = "(not run)";   // for the [eth-dbg] line

static bool queryNtpOnce() {
    uint8_t pkt[NTP_PACKET_SIZE] = {0};
    pkt[0]  = 0xE3;   // LI=11 unsync, VN=4, Mode=3 client
    pkt[1]  = 0;      // stratum
    pkt[2]  = 6;      // poll interval
    pkt[3]  = 0xEC;   // peer clock precision
    pkt[12] = 49; pkt[13] = 0x4E; pkt[14] = 49; pkt[15] = 52;   // ref id "1N14"

    if (!ntpUdp.beginPacket(NTP_SERVER, 123)) return false;   // includes DNS
    ntpUdp.write(pkt, NTP_PACKET_SIZE);
    if (!ntpUdp.endPacket()) return false;

    const uint32_t deadline = millis() + 1500;
    while ((int32_t)(deadline - millis()) > 0) {
        if (ntpUdp.parsePacket() >= (int)NTP_PACKET_SIZE) {
            ntpUdp.read(pkt, NTP_PACKET_SIZE);
            // Bytes 40-43: transmit timestamp seconds (big-endian, since 1900).
            const uint32_t ntpSecs = ((uint32_t)pkt[40] << 24) |
                                     ((uint32_t)pkt[41] << 16) |
                                     ((uint32_t)pkt[42] <<  8) |
                                     ((uint32_t)pkt[43]);
            if (ntpSecs < NTP_TO_UNIX_SECS) return false;   // bogus packet
            const uint32_t unixSecs = ntpSecs - NTP_TO_UNIX_SECS;
            Teensy3Clock.set((time_t)unixSecs);
            setTime((time_t)unixSecs);
            Serial.printf("[ntp] OK  unix=%lu\n", (unsigned long)unixSecs);
            return true;
        }
        delay(5);
    }
    return false;
}

static void setupNtp() {
    if (!eth_hw_present || strcmp(eth_ip_str, "0.0.0.0") == 0) {
        Serial.println(F("[ntp] skipped (no IP)"));
        ntp_status = "skipped";
        return;
    }
    if (!ntpUdp.begin(NTP_LOCAL_PORT)) {
        Serial.println(F("[ntp] UDP begin failed"));
        ntp_status = "udp-fail";
        return;
    }
    Serial.printf("[ntp] querying %s ...\n", NTP_SERVER);
    for (int attempt = 1; attempt <= 3; ++attempt) {
        if (queryNtpOnce()) {
            ntp_status = "OK";
            ntpUdp.stop();
            return;
        }
        Serial.printf("[ntp] attempt %d timed out\n", attempt);
        delay(300);
    }
    Serial.println(F("[ntp] giving up — RTC unchanged"));
    ntp_status = "failed";
    ntpUdp.stop();
}

// Returns true and updates eth_ip_str if the current IP differs from the
// last-emitted value. Call after Ethernet.maintain() so lease renewals
// trigger a fresh ETH line to the dash.
static bool ethIpChanged() {
    IPAddress ip = Ethernet.localIP();
    char buf[16];
    snprintf(buf, sizeof(buf), "%d.%d.%d.%d", ip[0], ip[1], ip[2], ip[3]);
    if (strcmp(buf, eth_ip_str) == 0) return false;
    strncpy(eth_ip_str, buf, sizeof(eth_ip_str));
    return true;
}

// ---------------------------------------------------------------------------
// SD card — Teensy 4.1 built-in SDIO slot (BUILTIN_SDCARD constant).
// Detects whether a card is present and whether it has a valid FAT filesystem.
// Supports on-demand FAT32 format triggered by a dash SDFORMAT command.
// ---------------------------------------------------------------------------
static SdFat32 sdFat;

enum SdCardStatus : uint8_t {
    SD_CARD_NONE       = 0,   // no card detected
    SD_CARD_NEEDS_FMT  = 1,   // card present, no valid FAT filesystem
    SD_CARD_READY      = 2,   // mounted OK
    SD_CARD_ERROR      = 3,   // hardware error
    SD_CARD_FORMATTING = 4,   // format in progress
};
static SdCardStatus sd_card_status = SD_CARD_NONE;
static uint32_t     sd_total_mb    = 0;
static uint32_t     sd_free_mb     = 0;

static void detectSD() {
    sd_total_mb = sd_free_mb = 0;
    if (sdFat.begin(SdioConfig(FIFO_SDIO))) {
        sd_card_status = SD_CARD_READY;
        sd_total_mb = (uint32_t)(sdFat.card()->sectorCount() / 2048ULL);
        const uint64_t freeSects = (uint64_t)sdFat.vol()->freeClusterCount()
                                 * sdFat.vol()->sectorsPerCluster();
        sd_free_mb = (uint32_t)(freeSects / 2048ULL);
    } else {
        // Try card-only init — hardware responding means FS is missing/corrupt.
        SdioCard rawCard;
        if (rawCard.begin(SdioConfig(FIFO_SDIO))) {
            sd_card_status = SD_CARD_NEEDS_FMT;
            sd_total_mb = (uint32_t)(rawCard.sectorCount() / 2048ULL);
        } else {
            sd_card_status = SD_CARD_NONE;
        }
    }
}

static void emitSdStatus() {
    switch (sd_card_status) {
        case SD_CARD_READY:
            DASH_SERIAL.printf("SD,READY,%lu,%lu\n",
                               (unsigned long)sd_total_mb, (unsigned long)sd_free_mb);
            break;
        case SD_CARD_NEEDS_FMT:
            DASH_SERIAL.printf("SD,FMT,%lu\n", (unsigned long)sd_total_mb);
            break;
        case SD_CARD_NONE:
            DASH_SERIAL.printf("SD,NONE\n");   break;
        case SD_CARD_ERROR:
            DASH_SERIAL.printf("SD,ERR\n");    break;
        case SD_CARD_FORMATTING:
            DASH_SERIAL.printf("SD,ACTIVE\n"); break;
    }
    Serial.printf("[sd] %s  total=%luMB free=%luMB\n",
                  sd_card_status == SD_CARD_READY      ? "READY"        :
                  sd_card_status == SD_CARD_NEEDS_FMT  ? "NEEDS_FORMAT" :
                  sd_card_status == SD_CARD_FORMATTING ? "FORMATTING"   :
                  sd_card_status == SD_CARD_ERROR      ? "ERROR"        : "NONE",
                  (unsigned long)sd_total_mb, (unsigned long)sd_free_mb);
}

static void formatSDCard() {
    sd_card_status = SD_CARD_FORMATTING;
    emitSdStatus();
    SdioCard rawCard;
    if (!rawCard.begin(SdioConfig(FIFO_SDIO))) {
        Serial.println(F("[sd] format: card init failed"));
        sd_card_status = SD_CARD_ERROR;
        emitSdStatus();
        return;
    }
    FatFormatter fmt;
    static uint8_t fmtBuf[512];
    const bool ok = fmt.format(&rawCard, fmtBuf, &Serial);
    Serial.printf("[sd] format %s\n", ok ? "OK" : "FAILED");
    detectSD();
    emitSdStatus();
}

// ---------------------------------------------------------------------------
// Session writer — NDJSON sample log on the SD card.
//
// File path: /sessions/session_<unix>_<track>.ndjson  (or session_nortc_<ms>_<track>.
// ndjson when RTC isn't set). Track name is sanitised — anything outside
// [A-Za-z0-9._-] becomes '_'.
//
// One JSON object per line, descriptive keys, per CLAUDE.md:
//   {"t":1714942567.234,"fix":3,"sats":12,"lat":40.123456,"lon":-74.123456,
//    "speed_mph":67.5,"heading_deg":123.4,"rpm":5800,"oil_psi":65.0,
//    "coolant_f":218.5,"ax":0.02,"ay":-0.98,"az":0.12,"gx":1.3,"gy":-0.5,"gz":0.2}
//
// Sub-second timestamp uses session-relative millis offset against the unix
// epoch captured at REC,1 — monotonic, won't jump even if NTP later corrects
// the RTC mid-session.
//
// Periodic flush every ~1 s so a power loss costs <=1 s of samples, not the
// whole session.
// ---------------------------------------------------------------------------
static File32  session_file;
static bool    session_file_open = false;
static uint32_t session_samples       = 0;
static uint32_t session_last_flush_ms = 0;
static char    session_path[80]      = "";

// Replace anything outside [A-Za-z0-9._-] with '_'. Truncates to outsize-1.
static void sanitizeName(const char* in, char* out, size_t outsize) {
    if (outsize == 0) return;
    size_t n = 0;
    for (; in[n] != '\0' && n + 1 < outsize; ++n) {
        const char c = in[n];
        const bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                        (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
        out[n] = ok ? c : '_';
    }
    out[n] = '\0';
    if (n == 0) { strncpy(out, "UNKNOWN", outsize); out[outsize-1] = '\0'; }
}

static void emitSessionStatus(bool active) {
    // SD,REC,<0|1>,<filename>,<samples>
    const char* fname = session_path[0] ? session_path : "";
    DASH_SERIAL.printf("SD,REC,%u,%s,%lu\n",
                       active ? 1 : 0, fname, (unsigned long)session_samples);
    Serial.printf("[sd] SESSION %s  file=%s  samples=%lu\n",
                  active ? "OPEN" : "CLOSE", fname, (unsigned long)session_samples);
}

static void openSession() {
    session_file_open    = false;
    session_samples      = 0;
    session_last_flush_ms = millis();
    session_path[0]      = '\0';

    if (sd_card_status != SD_CARD_READY) {
        Serial.println(F("[sd] openSession: card not READY — skipping"));
        emitSessionStatus(false);
        return;
    }

    if (!sdFat.exists("/sessions")) {
        if (!sdFat.mkdir("/sessions")) {
            Serial.println(F("[sd] mkdir /sessions failed"));
            emitSessionStatus(false);
            return;
        }
    }

    char trackSafe[32];
    sanitizeName(current_track, trackSafe, sizeof(trackSafe));

    if (session_start_unix > 0) {
        snprintf(session_path, sizeof(session_path),
                 "/sessions/session_%lu_%s.ndjson",
                 (unsigned long)session_start_unix, trackSafe);
    } else {
        snprintf(session_path, sizeof(session_path),
                 "/sessions/session_nortc_%lu_%s.ndjson",
                 (unsigned long)session_start_ms, trackSafe);
    }

    if (!session_file.open(session_path, O_WRITE | O_CREAT | O_TRUNC)) {
        Serial.printf("[sd] open %s FAILED\n", session_path);
        session_path[0] = '\0';
        emitSessionStatus(false);
        return;
    }
    session_file_open = true;
    Serial.printf("[sd] opened %s\n", session_path);
    emitSessionStatus(true);
}

// Forward decl — cloud helpers live below this block but closeSession() uses them.
static constexpr int HTTP_STATUS_CANCELLED = -1;
static int  httpPost(const char* path, const uint8_t* body, size_t body_len, File32* file_body);
static bool moveToQueue(const char* src_path);
static void scanQueue();
static void emitCloudStatus();
static void liveStreamAppend(const char* line, size_t n);
static void emitUploadStart(const char* filename, uint32_t total);
static void emitUploadProg(uint32_t done);
static void emitUploadDone(const char* status);

static void closeSession() {
    if (session_file_open) {
        session_file.sync();
        session_file.close();
        session_file_open = false;
    }
    emitSessionStatus(false);

    // Decide whether to upload now, queue for later, or do nothing.
    // (Order matters: the path we use depends on whether the file made it to SD.)
    const bool have_file = (session_path[0] != '\0' && sdFat.exists(session_path));
    if (have_file && g_cfg.rec_cl) {
        bool tried_now    = false;
        bool upload_ok    = false;
        if (g_cfg.stream == 1 && !live_failed_session && !uploads_disabled) {
            // AfterRace mode: try to POST the whole file now.
            File32 f;
            if (f.open(session_path, O_READ)) {
                const uint32_t sz = f.fileSize();
                Serial.printf("[cloud] after-race POST %s (%lu bytes)...\n",
                              session_path, (unsigned long)sz);
                // Filename only — don't ship the full SD path to the dash UI.
                const char* base = strrchr(session_path, '/');
                emitUploadStart(base ? base + 1 : session_path, sz);
                const int status = httpPost("/upload", nullptr, sz, &f);
                f.close();
                tried_now = true;
                upload_ok = (status >= 200 && status < 300);
                if (status == HTTP_STATUS_CANCELLED) {
                    emitUploadDone("CANCELLED");
                    Serial.println(F("[cloud] after-race cancelled by dash"));
                } else if (upload_ok) {
                    emitUploadDone("OK");
                    sdFat.remove(session_path);
                    Serial.println(F("[cloud] after-race OK — file deleted"));
                } else {
                    emitUploadDone("FAIL");
                    Serial.printf("[cloud] after-race failed (status=%d)\n", status);
                }
            }
        }
        if (!upload_ok && !tried_now) {
            // Live mode where streaming failed, OR after-race not attempted (no link).
            // Push the whole file to /queue/ for later retry.
            moveToQueue(session_path);
        } else if (!upload_ok && tried_now) {
            moveToQueue(session_path);
        }
        scanQueue();
        emitCloudStatus();
    }

    // Reset session-scoped state.
    live_failed_session = false;
    live_buf_n          = 0;
    session_path[0]     = '\0';

    // Refresh free-MB count so the dash sees the new value next status emit.
    if (sd_card_status == SD_CARD_READY) {
        const uint64_t freeSects = (uint64_t)sdFat.vol()->freeClusterCount()
                                 * sdFat.vol()->sectorsPerCluster();
        sd_free_mb = (uint32_t)(freeSects / 2048ULL);
    }
}

static void writeSessionSample(uint8_t fix, uint8_t sats,
                               float lat_deg, float lon_deg,
                               float mph, float hdg_deg,
                               uint16_t rpm, int16_t oil_x10, int16_t cool_x10,
                               float ax, float ay, float az,
                               float gx, float gy, float gz) {
    if (!session_file_open) return;

    // Sub-second epoch timestamp anchored at session start (monotonic).
    const uint32_t dt_ms  = millis() - session_start_ms;
    const uint32_t whole  = dt_ms / 1000;
    const uint32_t frac   = dt_ms % 1000;
    const uint32_t t_sec  = session_start_unix + whole;

    // Hand-rolled NDJSON — avoids ArduinoJson dep, ~250 bytes/sample.
    // -1 sentinels for oil/coolant become JSON null so the server can
    // distinguish "sensor faulted" from a real zero.
    char buf[320];
    int n = 0;
    if (session_start_unix > 0) {
        n = snprintf(buf, sizeof(buf),
            "{\"t\":%lu.%03lu,\"fix\":%u,\"sats\":%u,\"lat\":%.6f,\"lon\":%.6f,"
            "\"speed_mph\":%.1f,\"heading_deg\":%.1f,\"rpm\":%u,",
            (unsigned long)t_sec, (unsigned long)frac,
            fix, sats, lat_deg, lon_deg, mph, hdg_deg, rpm);
    } else {
        // No RTC: emit relative ms since session start as "t_ms" instead of "t".
        n = snprintf(buf, sizeof(buf),
            "{\"t_ms\":%lu,\"fix\":%u,\"sats\":%u,\"lat\":%.6f,\"lon\":%.6f,"
            "\"speed_mph\":%.1f,\"heading_deg\":%.1f,\"rpm\":%u,",
            (unsigned long)dt_ms,
            fix, sats, lat_deg, lon_deg, mph, hdg_deg, rpm);
    }
    if (n < 0 || n >= (int)sizeof(buf)) return;

    if (oil_x10 < 0)  n += snprintf(buf+n, sizeof(buf)-n, "\"oil_psi\":null,");
    else              n += snprintf(buf+n, sizeof(buf)-n, "\"oil_psi\":%.1f,",  oil_x10 * 0.1f);
    if (cool_x10 < 0) n += snprintf(buf+n, sizeof(buf)-n, "\"coolant_f\":null,");
    else              n += snprintf(buf+n, sizeof(buf)-n, "\"coolant_f\":%.1f,", cool_x10 * 0.1f);

    n += snprintf(buf+n, sizeof(buf)-n,
        "\"ax\":%.2f,\"ay\":%.2f,\"az\":%.2f,\"gx\":%.1f,\"gy\":%.1f,\"gz\":%.1f}\n",
        ax, ay, az, gx, gy, gz);
    if (n < 0 || n >= (int)sizeof(buf)) return;

    const int written = session_file.write((const uint8_t*)buf, (size_t)n);
    if (written != n) {
        Serial.printf("[sd] short write (%d/%d) — closing session\n", written, n);
        session_file.close();
        session_file_open = false;
        emitSessionStatus(false);
        return;
    }
    session_samples++;

    // Feed live cloud streamer (no-op if cloud disabled / Live mode off /
    // session already gave up on live this run).
    liveStreamAppend(buf, (size_t)n);

    // Periodic flush so power-loss costs <=1 s, and 1 Hz status heartbeat to dash.
    if (millis() - session_last_flush_ms >= 1000) {
        session_last_flush_ms = millis();
        session_file.sync();
        emitSessionStatus(true);   // sends SD,REC,1,<file>,<samples>
    }
}

// ---------------------------------------------------------------------------
// Cloud upload (HTTP only — HTTPS + FTP deferred; see CLAUDE.md).
//
// Settings arrive from the dash as CFG,<key>,<val> lines (see handleCfgLine).
// Three upload paths share one POST primitive (httpPostNdjson):
//
//   1. Live stream (rec_cl=1, cl_strm=0): liveStreamAppend() accumulates
//      NDJSON lines in a small RAM buffer. Every ~200 ms we POST the buffer
//      to /stream. If a POST fails we set live_failed_session=true; the rest
//      of the file then becomes a queue candidate on closeSession().
//   2. After Race (rec_cl=1, cl_strm=1): no live POSTs. closeSession() POSTs
//      the whole file to /upload. Failure -> move file to /queue/.
//   3. Queue walker: on link-up and once per 10 s when link is up + no
//      active session, walk /queue/ oldest-first and POST each to /upload.
//      Delete on 2xx, leave on failure for next pass.
//
// HTTP request format (both endpoints):
//   POST /stream HTTP/1.1                  (or /upload)
//   Host: <cl_host>
//   Content-Type: application/x-ndjson
//   X-API-Key:    <cl_key>
//   X-User-Email: <cl_email>
//   X-Session-Id: <unix epoch from REC,1>
//   X-Track-Name: <url-encoded track>
//   Content-Length: <n>
// ---------------------------------------------------------------------------
// (g_cfg, live_buf, live_failed_session, queue_depth declared up above near
// recording_active so closeSession() can reference them.)
static void handleCfgLine(const String& line) {
    // line == "CFG,<key>,<value>"
    // 'CFG,' is FOUR characters — c1 was off-by-one as 3, which made every
    // line parse as empty key + 'cl_host,...' value. Every CFG line came in
    // as 'unknown key' and g_cfg never got populated, silently disabling
    // all cloud config. Bug originally landed in Phase B + C.
    const int c1 = 4;                                        // after "CFG,"
    const int c2 = line.indexOf(',', c1);
    if (c2 < 0) return;
    const String key = line.substring(c1, c2);
    const String val = line.substring(c2 + 1);
    if      (key == "cl_host")  { strncpy(g_cfg.host,    val.c_str(), sizeof(g_cfg.host)-1);    g_cfg.host[sizeof(g_cfg.host)-1]=0; }
    else if (key == "cl_port")  { g_cfg.port  = (uint16_t)val.toInt(); }
    else if (key == "cl_proto") { g_cfg.proto = (uint8_t) val.toInt(); }
    else if (key == "cl_strm")  { g_cfg.stream= (uint8_t) val.toInt(); }
    else if (key == "cl_email") { strncpy(g_cfg.email,   val.c_str(), sizeof(g_cfg.email)-1);   g_cfg.email[sizeof(g_cfg.email)-1]=0; }
    else if (key == "cl_key")   { strncpy(g_cfg.api_key, val.c_str(), sizeof(g_cfg.api_key)-1); g_cfg.api_key[sizeof(g_cfg.api_key)-1]=0; }
    else if (key == "rec_sd")   { g_cfg.rec_sd = (val.toInt() != 0); }
    else if (key == "rec_cl")   { g_cfg.rec_cl = (val.toInt() != 0); }
    else if (key == "inet") {
        // Internet routing mode. 0=Ethernet (Teensy owns the network),
        // 1=WiFi (CrowPanel owns it). Phase 1: only relevant for NTP — in
        // WiFi mode the dash pushes SETTIME and we skip our own NTP retries.
        // Phase 3 will route cloud uploads accordingly.
        g_cfg.inet = (uint8_t)val.toInt();
    }
    else {
        Serial.printf("[cfg] unknown key %s\n", key.c_str());
        return;
    }
    Serial.printf("[cfg] %s = %s\n", key.c_str(), val.c_str());
}

// URL-encode the small subset that actually shows up in track names. Anything
// outside [A-Za-z0-9._~-] becomes %XX. Writes up to outsize-1 chars + NUL.
static void urlEncode(const char* in, char* out, size_t outsize) {
    // 'HEX' would collide with Teensy core Print.h's HEX macro — use a name
    // unlikely to be a #define.
    static const char HEX_DIGITS[] = "0123456789ABCDEF";
    size_t o = 0;
    for (size_t i = 0; in[i] && o + 4 < outsize; ++i) {
        const unsigned char c = (unsigned char)in[i];
        const bool safe = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                          (c >= '0' && c <= '9') || c == '.' || c == '_' ||
                          c == '~' || c == '-';
        if (safe) { out[o++] = (char)c; }
        else      { out[o++] = '%'; out[o++] = HEX_DIGITS[c >> 4]; out[o++] = HEX_DIGITS[c & 0xF]; }
    }
    out[o] = '\0';
}

// Emit upload progress to the dash. file_body==nullptr means live-stream
// (no modal); otherwise it's a real session/queue file and the dash should
// show the blocking progress modal.
// (HTTP_STATUS_CANCELLED + these three forward-decls live up top so closeSession()
//  in the SD section can call them — main.cpp is plain C++, no auto-prototyper.)
static void emitUploadStart(const char* filename, uint32_t total) {
    DASH_SERIAL.printf("UPLOAD,START,%s,%lu\n", filename, (unsigned long)total);
}
static void emitUploadProg(uint32_t done) {
    DASH_SERIAL.printf("UPLOAD,PROG,%lu\n", (unsigned long)done);
}
static void emitUploadDone(const char* status) {
    DASH_SERIAL.printf("UPLOAD,DONE,%s\n", status);
}

// Single POST primitive. body may be a contiguous buffer (live stream) or a
// file streamed in fixed-size chunks (after-race / queue). For the file path
// the caller passes body=nullptr + body_len=total + file=open File32. Returns
// HTTP status code, HTTP_STATUS_CANCELLED on user cancel, or 0 on connect/
// transport failure.
static int httpPost(const char* path, const uint8_t* body, size_t body_len,
                    File32* file_body) {
    if (uploads_disabled)                                   return 0;
    if (!eth_hw_present)                                    return 0;
    if (Ethernet.linkStatus() != LinkON)                    return 0;
    if (g_cfg.host[0] == '\0' || g_cfg.port == 0)           return 0;
    if (g_cfg.proto != 0) {
        // HTTPS / FTP not yet wired. Skip silently — these are queued and
        // will sit on the SD card until firmware grows the protocols.
        return 0;
    }

    EthernetClient c;
    c.setConnectionTimeout(800);   // ms; covers DNS + 3-way handshake on LAN
    if (!c.connect(g_cfg.host, g_cfg.port)) {
        Serial.printf("[cloud] connect %s:%u failed\n", g_cfg.host, g_cfg.port);
        return 0;
    }

    // Build a single header blob and write in one go so the W5500 sends a
    // single short TCP segment for the request line + headers.
    char trackEsc[64]; urlEncode(current_track, trackEsc, sizeof(trackEsc));
    char hdr[512];
    int hn = snprintf(hdr, sizeof(hdr),
        "POST %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Content-Type: application/x-ndjson\r\n"
        "X-API-Key: %s\r\n"
        "X-User-Email: %s\r\n"
        "X-Session-Id: %lu\r\n"
        "X-Track-Name: %s\r\n"
        "Content-Length: %u\r\n"
        "Connection: close\r\n"
        "\r\n",
        path, g_cfg.host, g_cfg.api_key, g_cfg.email,
        (unsigned long)session_start_unix, trackEsc, (unsigned)body_len);
    if (hn < 0 || hn >= (int)sizeof(hdr)) { c.stop(); return 0; }
    c.write((const uint8_t*)hdr, (size_t)hn);

    if (file_body) {
        // Stream the file in 1 KB chunks. Rewinds the file to the start first.
        if (!file_body->seek(0)) { c.stop(); return 0; }

        upload_in_progress    = true;
        upload_cancel_pending = false;
        uint8_t chunk[1024];
        size_t  remaining     = body_len;
        uint32_t done         = 0;
        uint32_t last_prog_ms = 0;
        emitUploadProg(0);
        while (remaining > 0 && c.connected()) {
            // Pump dash commands so an in-flight UPLOAD,CANCEL is observed
            // without waiting for the post-POST loop tick.
            pumpDashCommands();
            if (upload_cancel_pending) {
                c.stop();
                upload_in_progress = false;
                return HTTP_STATUS_CANCELLED;
            }
            size_t want = remaining > sizeof(chunk) ? sizeof(chunk) : remaining;
            const int got = file_body->read(chunk, want);
            if (got <= 0) break;
            const int w   = c.write(chunk, (size_t)got);
            if (w != got) { c.stop(); upload_in_progress = false; return 0; }
            remaining -= (size_t)got;
            done      += (size_t)got;
            if (millis() - last_prog_ms >= 250) {
                last_prog_ms = millis();
                emitUploadProg(done);
            }
        }
        emitUploadProg(done);
        upload_in_progress = false;
    } else if (body && body_len > 0) {
        c.write(body, body_len);
    }

    // Read just enough of the response to grab the status code: "HTTP/1.1 NNN ..."
    int status = 0;
    const uint32_t deadline = millis() + 1500;
    String line; line.reserve(64);
    while (millis() < deadline && (c.connected() || c.available())) {
        while (c.available() && (int32_t)(deadline - millis()) > 0) {
            const char ch = (char)c.read();
            if (ch == '\n') goto done_status;
            if (ch != '\r' && line.length() < 60) line += ch;
        }
        delay(1);
    }
done_status:
    if (line.length() >= 12 && line.startsWith("HTTP/")) {
        status = line.substring(9, 12).toInt();
    }
    c.stop();
    return status;
}

// --- Live streamer -------------------------------------------------------
// Accumulates NDJSON sample lines in a small RAM buffer. Flushed periodically
// from emitToDash() via liveStreamMaybeFlush(). (Storage declared up above
// next to recording_active.)
static void emitCloudStatus() {
    DASH_SERIAL.printf("CLD,%u,%lu\n",
                       (unsigned)(live_status_last_ok ? 1 : 0),
                       (unsigned long)queue_depth);
}

static void liveStreamAppend(const char* line, size_t n) {
    if (!g_cfg.rec_cl || g_cfg.stream != 0) return;     // not in Live mode
    if (live_failed_session)                return;     // already gave up for this session
    if (live_buf_n + n > sizeof(live_buf))  return;     // drop — next flush will catch up
    memcpy(live_buf + live_buf_n, line, n);
    live_buf_n += n;
}

static void liveStreamMaybeFlush() {
    if (uploads_disabled) { live_buf_n = 0; return; }
    if (live_buf_n == 0) return;
    if (millis() - live_last_flush_ms < 200) return;
    live_last_flush_ms = millis();

    const int status = httpPost("/stream", live_buf, live_buf_n, nullptr);
    const bool ok = (status >= 200 && status < 300);
    if (ok) {
        live_buf_n = 0;
        if (!live_status_last_ok) { live_status_last_ok = true; emitCloudStatus(); }
    } else {
        Serial.printf("[cloud] live POST failed (status=%d) — session will queue on close\n", status);
        live_failed_session = true;
        live_buf_n = 0;   // drop buffered samples — the SD file has them anyway
        if (live_status_last_ok) { live_status_last_ok = false; emitCloudStatus(); }
    }
}

// --- /queue/ walker ------------------------------------------------------
// Move a session file from /sessions/ to /queue/ atomically. SdFat's rename()
// is fine within the same volume.
static bool moveToQueue(const char* src_path) {
    if (!sdFat.exists("/queue") && !sdFat.mkdir("/queue")) {
        Serial.println(F("[queue] mkdir /queue failed"));
        return false;
    }
    const char* base = strrchr(src_path, '/');
    base = base ? base + 1 : src_path;
    char dst[96];
    snprintf(dst, sizeof(dst), "/queue/%s", base);
    if (!sdFat.rename(src_path, dst)) {
        Serial.printf("[queue] rename %s -> %s failed\n", src_path, dst);
        return false;
    }
    Serial.printf("[queue] %s -> %s\n", src_path, dst);
    return true;
}

static void scanQueue() {
    queue_depth = 0;
    if (sd_card_status != SD_CARD_READY) return;
    if (!sdFat.exists("/queue")) return;
    File32 dir;
    if (!dir.open("/queue", O_READ)) return;
    File32 entry;
    while (entry.openNext(&dir, O_READ)) {
        if (!entry.isDir()) queue_depth++;
        entry.close();
    }
    dir.close();
}

// Try to upload one queued file. Returns true on 2xx + delete; false otherwise.
static bool drainOneQueued() {
    if (sd_card_status != SD_CARD_READY) return false;
    if (!sdFat.exists("/queue")) return false;

    File32 dir;
    if (!dir.open("/queue", O_READ)) return false;
    char  chosen_path[96] = "";
    File32 entry;
    // Pick first regular file. We don't sort by mtime; FAT order is roughly
    // insertion order on freshly-formatted cards, which is good enough.
    while (entry.openNext(&dir, O_READ)) {
        if (!entry.isDir()) {
            char name[64];
            entry.getName(name, sizeof(name));
            snprintf(chosen_path, sizeof(chosen_path), "/queue/%s", name);
            entry.close();
            break;
        }
        entry.close();
    }
    dir.close();
    if (chosen_path[0] == '\0') return false;

    File32 f;
    if (!f.open(chosen_path, O_READ)) {
        Serial.printf("[queue] open %s failed\n", chosen_path);
        return false;
    }
    const uint32_t sz = f.fileSize();
    Serial.printf("[queue] uploading %s (%lu bytes)...\n",
                  chosen_path, (unsigned long)sz);
    const char* base = strrchr(chosen_path, '/');
    emitUploadStart(base ? base + 1 : chosen_path, sz);
    const int status = httpPost("/upload", nullptr, sz, &f);
    f.close();
    if (status == HTTP_STATUS_CANCELLED) {
        emitUploadDone("CANCELLED");
        Serial.printf("[queue] %s cancelled by dash — uploads disabled until reboot\n", chosen_path);
        return false;
    }
    if (status >= 200 && status < 300) {
        emitUploadDone("OK");
        sdFat.remove(chosen_path);
        Serial.printf("[queue] OK — %s deleted\n", chosen_path);
        return true;
    }
    emitUploadDone("FAIL");
    Serial.printf("[queue] %s POST failed (status=%d) — leaving for next pass\n",
                  chosen_path, status);
    return false;
}

static void cloudTick() {
    if (uploads_disabled) return;   // cancel-latched until reboot
    if (recording_active) {
        // Only the live streamer runs during a session — queue walking is
        // strictly between sessions to avoid contending with live POSTs.
        liveStreamMaybeFlush();
        return;
    }
    if (!eth_hw_present || Ethernet.linkStatus() != LinkON) return;
    if (queue_depth == 0) return;

    static uint32_t last_drain_ms = 0;
    if (millis() - last_drain_ms < 10000) return;
    last_drain_ms = millis();

    const bool ok = drainOneQueued();
    if (ok) {
        scanQueue();
        emitCloudStatus();
    }
}

// Compute current RPM from accumulated samples, then reset the accumulator.
// Call from emitToDash() so each emit reflects fresh data.
static uint16_t computeRpmAndReset() {
    if (millis() - rpm_last_pulse_ms > RPM_TIMEOUT_MS) {
        // No pulses in a while — engine off, or wire disconnected.
        rpm_pulse_sum = 0; rpm_pulse_count = 0;
        rpm_current = 0;
        return 0;
    }
    if (rpm_pulse_count > 0) {
        const double freq_hz = FreqMeasure.countToFrequency(rpm_pulse_sum / rpm_pulse_count);
        // RPM = (Hz * 60) / pulses_per_rev
        const double rpm = (freq_hz * 60.0) / RPM_PULSES_PER_REV;
        rpm_pulse_sum = 0; rpm_pulse_count = 0;
        if (rpm < 0)        rpm_current = 0;
        else if (rpm > 65535) rpm_current = 65535;
        else                  rpm_current = (uint16_t)(rpm + 0.5);
    }
    return rpm_current;
}

void setup() {
    pinMode(LED_BUILTIN, OUTPUT);
    Serial.begin(115200);
    DASH_SERIAL.begin(DASH_BAUD);
    // RX buffer sizing matters BIG TIME for Phase 2b firmware updates:
    // FlasherX's flash_write_block() does a 4 KB sector erase whenever the
    // staged image crosses a sector boundary, and that erase can stall the
    // read loop for ~25-100 ms. At 921 600 baud that's 2.3-9.2 KB streaming
    // in while we can't read — a 2 KB buffer overflows badly, losing chars
    // mid-line so the next hex record fails parse + FlasherX aborts the OTA.
    //
    // 32 KB gives ~348 ms of margin: bigger than any plausible flash stall.
    // Plenty of RAM2 to spare (we have 511 KB free for malloc/new). Bonus:
    // also more resilient to any loop() jitter from SD sync / SPI bursts.
    static uint8_t dashRxBuf[32768];
    Serial3.addMemoryForRead(dashRxBuf, sizeof(dashRxBuf));
    setSyncProvider(getTeensyTime);

    // Wait briefly for USB serial; don't block forever if no host is attached.
    unsigned long start = millis();
    while (!Serial && millis() - start < 1500) { /* spin */ }
    Serial.printf("racecar-35 dash teensy boot, firmware v%s\n", FIRMWARE_VERSION);
    // FlasherX check_flash_id() will search the staged OTA image for this
    // literal (FLASH_ID == "fw_teensy41" on Teensy 4.1, defined in FlashTxx.h).
    // We print it here so the linker keeps the string in our image too — the
    // staged image must contain the same literal for the check to pass.
    Serial.println(F("FLASH_ID:" FLASH_ID));
    // Tell the dash what firmware we're running so it can show it in settings
    // and compare against GitHub's manifest.json when "Check for updates" runs.
    DASH_SERIAL.printf("VER,teensy,%s\n", FIRMWARE_VERSION);

    // Tach input on pin 9 (FreqMeasure uses FlexPWM input capture on T4.x).
    // Must be called after Serial.begin to avoid weird interactions.
    FreqMeasure.begin();
    Serial.println(F("FreqMeasure on pin 9: armed"));

    // ADC for oil pressure (A2) and coolant temp (A3). 12-bit + 16-sample
    // hardware averaging gives a stable, noise-floor-free reading at 25 Hz.
    analogReadResolution(12);
    analogReadAveraging(16);
    // Internal pull-down on both ADC pins so a disconnected pin reads ~0 V
    // and the per-channel fault detection (v_sensor < 0.3 V for oil,
    // raw <= 4 for coolant) trips reliably. When a real sensor circuit is
    // wired (oil: 10k/20k divider from a 0.5-4.5 V transducer; coolant:
    // 150 Ω pull-up to 3.3 V with NTC to GND), the external source impedance
    // (~6.7 kΩ oil divider, 150 Ω coolant pull-up) is far stiffer than the
    // internal ~50 kΩ pull-down, so the real reading dominates with a <5%
    // calibration shift absorbed during sensor calibration.
    pinMode(OIL_ADC_PIN,     INPUT_PULLDOWN);
    pinMode(COOLANT_ADC_PIN, INPUT_PULLDOWN);
    Serial.println(F("Analog: oil PSI on A2, coolant degF on A3 (12-bit, x16 avg, pulldown)"));

    // IMU on Wire (SDA=18, SCL=19). Non-fatal if absent.
    imu_present = setupIMU();

    // W5500 Ethernet (SPI0: CS=10, RST=6). Non-fatal if unplugged.
    setupEthernet();
    DASH_SERIAL.printf("ETH,%s\n", eth_ip_str);   // emit immediately so dash picks up IP

    // NTP — sets the Teensy RTC if Ethernet got an IP. Non-fatal otherwise.
    setupNtp();

    // Built-in SDIO SD card. Non-fatal if absent.
    detectSD();
    emitSdStatus();

    // Scan /queue/ once at boot so the dash sees the backlog count right away.
    scanQueue();
    emitCloudStatus();

    // SparkFun RTK boards ship at 38400; bare modules at 9600 — try both.
    if (tryConnectGNSS(GPS_BAUD_PRIMARY) || tryConnectGNSS(GPS_BAUD_FALLBACK)) {
        Serial.println(F("u-blox connected"));
        myGNSS.setUART1Output(COM_TYPE_UBX);     // we don't need NMEA on this link
        myGNSS.setNavigationFrequency(NAV_RATE_HZ);
        myGNSS.setAutoPVT(true);
        gnss_lib_ok = true;
    } else {
        Serial.println(F("u-blox NOT detected on Serial2 (tried 38400 and 9600)"));
        // Leave Serial2 open at 9600 so we can still observe raw bytes flowing
        // from the module (most u-blox modules default-output NMEA at 9600).
        // The dash will report status=RAW or OFF depending on what we see.
        GPS_SERIAL.begin(GPS_BAUD_FALLBACK);
        gnss_lib_ok = false;
    }
}

static void emitToDash() {
    // Only read the SparkFun lib's PVT cache after we've confirmed at least
    // ONE fresh PVT arrived (gnss_last_fresh_ms != 0). Calling getFixType()
    // etc. before that returns uninitialized heap memory — at boot we saw
    // fix=128 sats=121 speed=2 million as a result.
    const bool have_pvt = gnss_lib_ok && gnss_last_fresh_ms != 0;
    const uint8_t fix     = have_pvt ? myGNSS.getFixType()       : 0;
    const uint8_t sats    = have_pvt ? myGNSS.getSIV()           : 0;
    const float   lat_deg = have_pvt ? myGNSS.getLatitude()    * 1e-7f      : 0.0f;
    const float   lon_deg = have_pvt ? myGNSS.getLongitude()   * 1e-7f      : 0.0f;
    const float   mph     = have_pvt ? myGNSS.getGroundSpeed() * 0.00223694f : 0.0f;
    const float   hdg_deg = have_pvt ? myGNSS.getHeading()     * 1e-5f      : 0.0f;
    const uint8_t status  = gpsStatus();

    DASH_SERIAL.printf("GPS,%u,%u,%.6f,%.6f,%.1f,%.1f,%u\n",
                       fix, sats, lat_deg, lon_deg, mph, hdg_deg, status);
    // Echo to USB serial for debugging.
    Serial.printf("GPS,%u,%u,%.6f,%.6f,%.1f,%.1f,%u  (raw_bytes=%lu)\n",
                  fix, sats, lat_deg, lon_deg, mph, hdg_deg, status,
                  (unsigned long)gnss_raw_bytes);

    // Engine RPM + analog sensors (oil PSI, coolant degF). All emitted as
    // integers scaled x10 except RPM. -1 on the analog values signals a
    // sensor fault to the dash (it shows '---' rather than zero).
    const uint16_t rpm          = computeRpmAndReset();
    const int16_t  oil_psi_x10  = readOilPsiX10();
    const int16_t  cool_f_x10   = readCoolantFx10();
    DASH_SERIAL.printf("ENG,%u,%d,%d\n", rpm, oil_psi_x10, cool_f_x10);
    Serial.printf("ENG,%u,%d,%d\n", rpm, oil_psi_x10, cool_f_x10);

    // IMU — flush averaged samples and emit even when absent (zeroes keep the
    // dash parser's field count stable and make it easy to detect a missing IMU).
    flushImu();
    DASH_SERIAL.printf("IMU,%.2f,%.2f,%.2f,%.1f,%.1f,%.1f\n",
                       imu.ax, imu.ay, imu.az, imu.gx, imu.gy, imu.gz);
    Serial.printf("IMU,%.2f,%.2f,%.2f,%.1f,%.1f,%.1f\n",
                  imu.ax, imu.ay, imu.az, imu.gx, imu.gy, imu.gz);

    // SD logging: append one NDJSON sample with all the fields we just emitted.
    if (recording_active && session_file_open) {
        writeSessionSample(fix, sats, lat_deg, lon_deg, mph, hdg_deg,
                           rpm, oil_psi_x10, cool_f_x10,
                           imu.ax, imu.ay, imu.az, imu.gx, imu.gy, imu.gz);
    }

    // Time of day from RTC — piggybacks on the 1 Hz GPS heartbeat so the dash
    // gets a fresh TIME line every emit without a separate periodic block in
    // loop() (which previously was causing UART stalls via Ethernet.maintain()).
    DASH_SERIAL.printf("TIME,%lu\n", (unsigned long)now());
}

void loop() {
    // Heartbeat LED so we can see at a glance the Teensy is alive.
    static unsigned long lastBlink = 0;
    if (millis() - lastBlink >= 500) {
        lastBlink = millis();
        digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
    }

    // Drain commands from the dash (REC, TRACK, SDFORMAT, …).
    pumpDashCommands();

    // NOTE: Ethernet maintenance and periodic SD/ETH status emission used to
    // happen here in a 2 s block. That block was causing UART stalls — when
    // the W5500 was wired and Ethernet.maintain() / Ethernet.localIP() were
    // called, the SPI transactions delayed the loop past the dash's 2 s STALE
    // threshold. We now emit ETH only at boot and TIME inside emitToDash()
    // (1 Hz with the GPS heartbeat). DHCP lease renewal is also dropped — for
    // a track session the lease will outlast it.

    // Periodic ethernet diagnostic output to USB Serial only (NEVER touch
    // DASH_SERIAL here — that's the dash UART). Boot info is cached so we
    // can keep showing what happened without re-running blocking probes.
    // We DO read linkStatus() each tick — it's a single SPI register read,
    // a few hundred microseconds, fine for a 1 Hz GPS heartbeat budget.
    static uint32_t lastEthDbgMs = 0;
    if (millis() - lastEthDbgMs >= 3000) {
        lastEthDbgMs = millis();
        const char* live_link = "n/a";
        if (eth_hw_present) {
            const EthernetLinkStatus lnk = Ethernet.linkStatus();
            live_link = (lnk == LinkON)  ? "UP" :
                        (lnk == LinkOFF) ? "DOWN" : "?";
        }
        Serial.printf("[eth-dbg] chip=%s  link=now=%s  DHCP=%s  ip=%s  NTP=%s\n",
                      eth_chip_name, live_link, eth_dhcp_result, eth_ip_str,
                      ntp_status);
    }

    // Tach FIFO drain — interrupt-driven; we just pull samples off.
    pumpTach();

    // Cloud upload tick — live streamer during recording, queue drainer between.
    cloudTick();

    // IMU accumulate — rate-limited internally to ~250 Hz.
    readIMU();

    // GPS health tracking — exactly one path is active at a time.
    bool freshThisCall = false;
    if (gnss_lib_ok) {
        if (myGNSS.getPVT(0)) {       // non-blocking: returns true ONLY on fresh PVT
            gnss_last_fresh_ms = millis();
            freshThisCall = true;
        }
    } else {
        // Drain raw bytes the lib never claimed. Doesn't try to interpret —
        // we just want to know SOMETHING is on the GPS UART.
        while (GPS_SERIAL.available()) {
            (void)GPS_SERIAL.read();
            gnss_raw_bytes++;
            gnss_last_fresh_ms = millis();
        }
    }

    // Emit on every fresh PVT (== 25 Hz when GPS is reporting) plus a 1 Hz
    // heartbeat fallback so the dash's LINK indicator stays green even
    // without a GPS fix.
    static unsigned long lastEmit = 0;
    if (freshThisCall || millis() - lastEmit >= 1000) {
        lastEmit = millis();
        emitToDash();
    }
}
