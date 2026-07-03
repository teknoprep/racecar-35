// racecar-35 dash firmware — GPS + MS3Pro CAN + IMU to dash bridge
//
// Reads u-blox GNSS over Serial2 (pin 7=RX2, pin 8=TX2), the MS3Pro
// MegaSquirt ECU over CAN1 (pin 22=TX, pin 23=RX, via SN65HVD230
// 3.3V transceiver), and an MPU-6050 IMU over Wire (pin 18=SDA, 19=SCL),
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
#include <EEPROM.h>
#include "FXUtil.h"        // FlasherX: in-application reflash for Teensy 4.x
extern "C" {
  #include "FlashTxx.h"     // low-level flash primitives (firmware_buffer_init, etc.)
}

// Compile-time firmware version. Increment via the release process when
// publishing new firmware artifacts to firmware/manifest.json on main.
// Format: "MAJOR.MINOR.PATCH" — dash compares versions as semver strings.
// Teensy version is bumped in lock-step with the dash via scripts/release.sh.
#define FIRMWARE_VERSION "0.1.104"

#include <SPI.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#include <SdFat.h>
#include <TimeLib.h>
#include <SparkFun_u-blox_GNSS_Arduino_Library.h>
#include <FreqMeasureMulti.h>
#include <FlexCAN_T4.h>

namespace {
  HardwareSerial& GPS_SERIAL  = Serial2;            // pins 7 (RX2), 8 (TX2)
  HardwareSerial& DASH_SERIAL = Serial3;            // pin 14 (TX3) -> CrowPanel UART0 RX
  constexpr uint32_t GPS_BAUD_TARGET   = 230400;    // RAISED (v0.1.83): 25 Hz UBX-NAV-PVT
                                                    // is ~25 kbit/s; at 38400 that's ~65%
                                                    // util (no headroom -> chronic backlog
                                                    // after any loop stall = GPS STALE). At
                                                    // 230400 it's ~11% -> a 32 KB backlog
                                                    // drains in <1 s.
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

  // Tach input: opto-isolated pulse on pin 9.
  //
  // IMPORTANT (T4.x): the plain `FreqMeasure` library is HARD-WIRED to pin 22
  // on the IMXRT1062 (FlexPWM4 CH0-A) and ignores any pin choice — pin 22 is
  // also our CAN1 TX, so it could never work for the opto tach. We use
  // `FreqMeasureMulti` instead, which takes a pin argument; pin 9 maps to
  // FlexPWM2_2_B and is input-capture capable. See RPM_TACH_PIN below.
  constexpr uint8_t  RPM_TACH_PIN       = 9;
  // PULSES_PER_REV depends on where the tap is taken — calibrate by reading
  // the dash at a known idle (e.g. 800 RPM should display ~800). Common
  // values for a 4-cyl 4-stroke:
  //   2.0  = wasted-spark coil-negative, distributor coil-neg, most ECU tach
  //   1.0  = single COP coil trigger
  //   0.5  = once-per-2-revs cam-position pulse
  // This is only the COMPILE-TIME DEFAULT now — the live value is set from the
  // dash (Settings -> "Tach pulses/rev") via `CFG,rpmppr,<value_x10>` and held
  // in g_cfg.rpm_ppr_x10. Think of it as the divider: a tach reading 2x too
  // high needs pulses/rev = 2, 4x too high needs 4, etc.
  constexpr float    RPM_PULSES_PER_REV = 2.0f;
  constexpr uint32_t RPM_TIMEOUT_MS     = 750;      // no pulses in this window -> RPM = 0

  // CAN bus — MS3Pro "Simplified Dash Broadcasting" on 0x5E8 (1512 dec) base
  // ID at 500 kbps. Verified against the official MegaSquirt CAN Broadcast spec
  // (msextra.com) AND a real capture: frame 0x5E8 = 03 FD 00 00 05 62 FF FF
  // decoded to MAP 102.1kPa / RPM 0 / CLT 137.8F / TPS 0 (key-on, engine off).
  // TunerStudio: CAN-Bus/Testmodes -> Dash Broadcasting, Enable=On, Automatic
  // (locks base to 1512, 50 Hz). All fields big-endian.
  // Transceiver: SN65HVD230 (3.3V). CAN1 TX=pin 22, RX=pin 23.
  // Add a 120Ω termination resistor across CAN H/L at the Teensy end;
  // MS3Pro already has one built-in at the ECU end.
  constexpr uint32_t CAN_BAUD           = 500000;
  constexpr uint32_t CAN_STALE_MS       = 2000;     // no frames → reset fields to -1
  constexpr uint32_t CAN_BASE_ID        = 0x5E8;    // MS3Pro Simplified Dash base (1512)

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
// close. Cloud upload is always After Race (live streaming was removed): the
// session file lands in /queue/ and is uploaded after the session ends,
// dash-driven (Q,GET) or via the queue drain. Nothing POSTs mid-session.
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
    char     email[64]  = "";
    char     api_key[64] = "";
    bool     rec_sd     = true;
    bool     rec_cl     = false;
    uint8_t  inet        = 0;     // 0=Ethernet, 1=WiFi (mirror of dash setting)
    uint8_t  sensor_type = 0;     // 0=Direct (opto tach + ADC), 1=MegaSquirt (CAN)
    uint16_t rpm_ppr_x10 = 20;    // tach pulses/rev x10 (20 = 2.0). Divides the
                                  // opto-tach frequency into RPM in Direct mode.
    int8_t   rpm_smooth  = 0;     // RPM display smoothing trim from the dash slider
    uint8_t  rpm_spike   = 2;     // spike filter: 0=Off 1=Mild 2=Normal 3=Strong (CFG,rpmspk)
                                  // (-10..+10): <0 raw/jumpy, 0 baseline, >0 smoother.
    bool     debug_enabled = false;// dash CFG,dbg_on. Default OFF (v0.1.103) —
                                  // diagnostic tool, enabled from Settings when
                                  // needed. When false, NO on-SD .dbg
                                  // health log is written for a session.
} g_cfg;

// ESP32-S3 dash temp, reported by the dash via the DTEMP line (heat diagnostics).
// Declared here (before handleDashCommand) so the command parser can set it.
static float dash_temp_c = -999.0f;

// Teensy reset-cause forensics (SRC_SRSR). Captured once at boot, then the
// sticky bits are cleared. Distinguishes a power/brownout reset (POR) from a
// watchdog, CPU lockup, overtemp, or the software reset a FlasherX OTA does —
// directly relevant to the "Teensy stopped communicating" mystery: if it keeps
// coming back as POR mid-session, the Teensy is browning out.
static char teensy_reset_reason[24] = "unknown";
static void captureResetReason() {
    uint32_t s = SRC_SRSR;
    SRC_SRSR = s;   // write-1-to-clear so the next boot reflects the NEXT reset
    const char* r = "unknown";
    if      (s & SRC_SRSR_TEMPSENSE_RST_B)    r = "OVERTEMP";
    else if (s & SRC_SRSR_WDOG_RST_B)         r = "watchdog";
    else if (s & SRC_SRSR_WDOG3_RST_B)        r = "watchdog3";
    else if (s & SRC_SRSR_LOCKUP_SYSRESETREQ) r = "lockup/swrst";
    else if (s & SRC_SRSR_IPP_USER_RESET_B)   r = "reset-pin";
    else if (s & SRC_SRSR_JTAG_SW_RST)        r = "jtag-sw";
    else if (s & SRC_SRSR_JTAG_RST_B)         r = "jtag";
    else if (s & SRC_SRSR_CSU_RESET_B)        r = "csu";
    else if (s & SRC_SRSR_IPP_RESET_B)        r = "POR(power)";
    strncpy(teensy_reset_reason, r, sizeof(teensy_reset_reason) - 1);
    teensy_reset_reason[sizeof(teensy_reset_reason) - 1] = 0;
}

// ---------------------------------------------------------------------------
// Start/finish LINE + lap counter. The dash pushes the active track's S/F line
// (CFG,sf,aLat,aLon,bLat,bLon); we count precise LINE crossings on the GPS
// stream and stamp the current lap number into each NDJSON sample so the server
// (and review UI) get exact, driver-matching lap boundaries instead of having
// to re-derive them. Reset at each REC,1.
// ---------------------------------------------------------------------------
static struct {
    bool     has_line = false;
    float    aLat = 0, aLon = 0, bLat = 0, bLon = 0;
    bool     have_prev = false;
    float    prevLat = 0, prevLon = 0;
    int      lap = 0;                 // current lap being driven (0 = before 1st crossing)
    uint32_t last_cross_ms = 0;
} sf_lap;

// Do path segment P0->P1 and S/F segment A->B intersect? (planar, lon*cos lat)
static bool segCrossT(float p0Lat, float p0Lon, float p1Lat, float p1Lon,
                      float aLat, float aLon, float bLat, float bLon) {
    const double k = cos(p0Lat * 0.017453292519943295);
    const double ax=p0Lon*k, ay=p0Lat, bx=p1Lon*k, by=p1Lat;
    const double cx=aLon*k, cy=aLat, dx=bLon*k, dy=bLat;
    auto cr = [](double px,double py,double qx,double qy,double rx,double ry){
        return (qx-px)*(ry-py)-(qy-py)*(rx-px); };
    const double d1=cr(cx,cy,dx,dy,ax,ay), d2=cr(cx,cy,dx,dy,bx,by);
    const double d3=cr(ax,ay,bx,by,cx,cy), d4=cr(ax,ay,bx,by,dx,dy);
    return ((d1>0)!=(d2>0)) && ((d3>0)!=(d4>0));
}

static void resetTeensyLap() {
    sf_lap.have_prev = false;
    sf_lap.lap = 0;
    sf_lap.last_cross_ms = 0;
}

// Update the lap counter from a fresh fix. First crossing -> lap 1; each later
// crossing -> lap++. 15 s minimum-lap guard against S/F double-triggers.
static void updateTeensyLap(uint8_t fix, float lat, float lon) {
    if (!sf_lap.has_line || fix < 2) return;
    if (sf_lap.have_prev) {
        if (segCrossT(sf_lap.prevLat, sf_lap.prevLon, lat, lon,
                      sf_lap.aLat, sf_lap.aLon, sf_lap.bLat, sf_lap.bLon)
            && (sf_lap.last_cross_ms == 0 || millis() - sf_lap.last_cross_ms >= 15000)) {
            sf_lap.lap++;
            sf_lap.last_cross_ms = millis();
        }
    }
    sf_lap.prevLat = lat; sf_lap.prevLon = lon; sf_lap.have_prev = true;
}

// Set when the dash sends UPLOAD,CANCEL. Volatile across reboot — deliberately
// in-RAM only so a reboot is the only way to re-enable uploads.
static volatile bool uploads_disabled = false;
// Set true while httpPost() is actively pushing a file; checked from
// pumpDashCommands -> handleDashCommand so a CANCEL during the loop aborts
// the current connection on the next chunk boundary.
static volatile bool upload_in_progress = false;
static volatile bool upload_cancel_pending = false;

// Live "stream to cloud" was removed (not ready). Cloud recording now ALWAYS
// uploads After Race: the session file is written to /queue/ and the dash
// drives the upload (Q,LIST/Q,GET/Q,DEL) / queue drain after the session ends.
// live_status_last_ok is retained only so the CLD line keeps its <live_ok>
// field shape (now always 0 — there is no live POST to report on).
static bool     live_status_last_ok = false;
static uint32_t queue_depth         = 0;       // updated by scanQueue()

// Test data generator. When test_mode_active is true, emitToDash() and the
// SD writer substitute synthetic, deterministic-but-plausible values for GPS,
// engine, and IMU instead of reading the real hardware. The dash toggles
// this via TESTSTART/TESTSTOP and observes the same UPLOAD,*/cloud lifecycle
// it sees during real sessions.
static bool     test_mode_active   = false;
static uint32_t test_mode_start_ms = 0;

// Manual drain request. Set true by the dash UPLOAD button so cloudTick
// stops waiting for its 10 s interval and starts draining the queue
// immediately. Cleared automatically when the queue empties or when a
// drain attempt fails (to avoid hammering a dead server).
static volatile bool drain_queue_now = false;

// Most recent upload failure reason, set by the wupForwardFile / httpPost
// paths and surfaced to the dash via UPLOAD,DONE,FAIL,<reason>. Empty when
// the upload succeeded or wasn't attempted.
static char last_upload_err[96] = "";

// True when the active path is 'WiFi via dash': dash owns the WiFi link and we
// forward session files to it over UART for cloud upload. Mirrors g_cfg.inet.
static bool wifiInetActive() { return g_cfg.inet == 1; }
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
                               float gx, float gy, float gz, int lap);
static void handleCfgLine(const String& line);   // cloud section below
static bool sdReady();                            // SD status helper, defined below
static void detectSD();                           // SD detect, defined below
static void emitSdStatus();                       // SD wire emit, defined below
static void scanQueue();                          // /queue/ walker, defined below
static void emitCloudStatus();                    // CLD wire emit, defined below
static void listSdContents();                     // SDLS diagnostic: dump /sessions,/queue,/cansniff
static void handleQList();                         // dash-initiated upload: list queue
static void handleQGet(const char* basename);     // dash-initiated upload: stream file
static void handleQDel(const char* basename);     // dash-initiated upload: delete file
static void openCanSniff();                       // CAN sniffer: open /cansniff/ file
static void closeCanSniff();                      // CAN sniffer: flush + close
static void cansniffLog(uint32_t id, bool ext, uint8_t len, const uint8_t* buf);
static void emitCanSniffStatus();                 // CANSNIFF wire emit to dash
// CAN sniffer state — declared early so handleDashCommand() (above pumpCAN)
// can toggle it. Defined/used by the CAN + SD sections below.
static bool     cansniff_active = false;
static uint32_t cansniff_frames = 0;

// TimeLib sync provider — reads the Teensy 4.1 built-in RTC.
static time_t getTeensyTime() { return Teensy3Clock.get(); }

static void runFirmwareUpdate(Stream& io, const char* tag) {
    uint32_t buffer_addr = 0, buffer_size = 0;
    if (firmware_buffer_init(&buffer_addr, &buffer_size) == 0) {
        io.println(F("FW,ERR,buffer_init_failed"));
        return;
    }
    Serial.printf("[%s fwupdate] buffer @ 0x%08lX, size=%lu\n",
                  tag, (unsigned long)buffer_addr, (unsigned long)buffer_size);
    io.printf("FW,READY,%lu\n", (unsigned long)buffer_size);
    // Per-line ACK protocol. The input/output/ack streams may all be the
    // same Stream (USB Serial test mode), or input+ack may be DASH_SERIAL and
    // diagnostics USB Serial (normal dash-driven OTA).
    update_firmware_acked(&io, &Serial, &io, buffer_addr, buffer_size);
    // If we get here, update_firmware_acked failed/returned before reboot.
    firmware_buffer_free(buffer_addr, buffer_size);
    io.println(F("FW,ERR,update_failed"));
}

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
                if (test_mode_active) {
                    test_mode_active = false;
                    DASH_SERIAL.println(F("TEST,0"));
                }
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
    } else if (line.startsWith("CANSNIFF,")) {
        // Dash CAN-sniffer toggle. CANSNIFF,1 opens a capture file and logs
        // every raw CAN frame; CANSNIFF,0 closes it.
        const bool want = (line.substring(9).toInt() != 0);
        if (want && !cansniff_active)      openCanSniff();
        else if (!want && cansniff_active) closeCanSniff();
        else                                emitCanSniffStatus();
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
    } else if (line == "TESTSTART") {
        // Begin a synthetic-data session. Track defaults to 'TEST' if the dash
        // didn't send a TRACK line first. We open a real /sessions/ file using
        // the same code path as a normal REC,1 so the closeSession() upload
        // pipeline (Ethernet HTTP or WiFi-via-WUP) is exercised end-to-end.
        if (!recording_active) {
            if (!sdReady()) {
                Serial.println(F("[teensy] TESTSTART rejected: SD not ready"));
                // Tell the dash to flip the button back so the user knows it
                // didn't actually start. The dash already shows the real SD
                // status on the TOOLS page next to Format SD card.
                DASH_SERIAL.println(F("TEST,0"));
                return;
            }
            test_mode_active     = true;
            test_mode_start_ms   = millis();
            if (current_track[0] == '\0' ||
                strcmp(current_track, "UNKNOWN") == 0) {
                strncpy(current_track, "TEST", sizeof(current_track) - 1);
                current_track[sizeof(current_track) - 1] = '\0';
            }
            recording_active   = true;
            session_start_ms   = millis();
            session_start_unix = (uint32_t)::now();
            Serial.printf("[teensy] TEST START \u2014 track=\"%s\"\n", current_track);
            openSession();
            DASH_SERIAL.println(F("TEST,1"));
        }
    } else if (line == "TESTSTOP") {
        // Stop synthetic session and run the same upload pipeline as REC,0.
        if (recording_active && test_mode_active) {
            recording_active   = false;
            closeSession();
            test_mode_active   = false;
            Serial.printf("[teensy] TEST STOP \u2014 duration=%lums\n",
                          (unsigned long)(millis() - test_mode_start_ms));
            DASH_SERIAL.println(F("TEST,0"));
        }
    } else if (line.startsWith("DBG,")) {
        // Dash diagnostic relay. Dash doesn't have its own USB serial that's
        // easy to monitor (UART0 / CH340 is the bridge to us), so it routes
        // DBG,<text> lines through and we forward to our USB CDC console. Use
        // `pio device monitor` on /dev/ttyACM0 to see dash-side state.
        Serial.printf("[dash-dbg] %s\n", line.substring(4).c_str());
    } else if (line.startsWith("DTEMP,")) {
        // Dash reports its ESP32-S3 die temp (°C) so we can log both MCUs' temps
        // together in the health line / .dbg log (heat diagnostics).
        dash_temp_c = line.substring(6).toFloat();
    } else if (line == "Q,LIST") {
        handleQList();
    } else if (line.startsWith("Q,GET,")) {
        handleQGet(line.substring(6).c_str());
    } else if (line.startsWith("Q,DEL,")) {
        handleQDel(line.substring(6).c_str());
    } else if (line == "QUEUE,DRAIN") {
        // Dash UPLOAD button: kick the queue walker immediately instead of
        // waiting for the next 10 s cloudTick. Multiple presses are idempotent.
        drain_queue_now = true;
        Serial.println(F("[cloud] QUEUE,DRAIN requested by dash"));
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
        // Re-announce the reset cause too — the boot-time RST emit is easy for
        // a slower-booting dash to miss (both power up together; the dash takes
        // seconds longer through WiFi init). VER? is sent on STATUS-page open.
        DASH_SERIAL.printf("RST,teensy,%s\n", teensy_reset_reason);
    } else if (line == "FWUPDATE") {
        // Dash kicking off a Teensy OTA over Serial3. We're about to hand the
        // UART to the FlasherX hex-receive loop, which is BLOCKING until EOF
        // or error. No telemetry/commands flow during that window.
        runFirmwareUpdate(DASH_SERIAL, "dash-uart");
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

static void handleUsbCommand(const String& line) {
    if (line == "VER?") {
        Serial.printf("VER,teensy,%s\n", FIRMWARE_VERSION);
    } else if (line == "SDRE") {
        // Manual SD re-detection. Useful from `pio device monitor` to test
        // recovery without rebooting or physically replugging the card.
        Serial.println(F("[sd] manual re-detect requested via USB"));
        detectSD();
        emitSdStatus();
        if (sdReady()) {
            scanQueue();
            emitCloudStatus();
        }
    } else if (line == "SDLS") {
        listSdContents();   // defined after sdFat in the SD section
    } else if (line == "USBFWUPDATE") {
        // Developer/test path: push local firmware.hex directly over Teensy's
        // native USB CDC port. This bypasses GitHub + CrowPanel WiFi + UART0
        // forwarding, making FlasherX tests fast and deterministic.
        runFirmwareUpdate(Serial, "usb");
    } else if (line.length() > 0) {
        Serial.printf("[usb] unknown cmd: %s\n", line.c_str());
    }
}

static void pumpUsbCommands() {
    static String cmdBuf;
    while (Serial.available()) {
        const char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') { handleUsbCommand(cmdBuf); cmdBuf = ""; }
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

// GPS stale auto-recovery watchdog. The root causes of mid-session GPS freeze
// are gone (live per-sample cloud POST removed; Ethernet.maintain() no longer
// runs in loop) and a 32 KB UART RX ring rides through SD-sync hiccups — so
// STALE should not occur during a session. This is a belt-and-suspenders net for
// if it ever does:
//   LIGHT  (>2.5 s stale): flush the RX ring so the UBX parser drops any
//          corrupted/backlogged bytes and resyncs on the next LIVE frame (the
//          reported position jumps to NOW instead of replaying stale fixes).
//          Fully non-blocking; rate-limited to once / 2 s.
//   HEAVY  (>10 s stale): the module itself may have glitched — re-run begin()
//          and re-assert UBX/autoPVT. Blocking (~handshake) but bounded, last
//          resort only; rate-limited to once / 10 s (the 32 KB ring covers the
//          one-time stall).
constexpr uint32_t GPS_STALE_RECOVER_MS    = 2500;
constexpr uint32_t GPS_RECOVER_INTERVAL_MS = 2000;
constexpr uint32_t GPS_REINIT_MS           = 10000;
constexpr uint32_t GPS_REINIT_INTERVAL_MS  = 10000;
static uint32_t gnss_last_recover_ms = 0;
static uint32_t gnss_last_reinit_ms  = 0;
static uint32_t gnss_recover_count   = 0;   // LIGHT flush/resync events
static uint32_t gnss_reinit_count    = 0;   // HEAVY re-begin() events

// ---- On-SD debug instrumentation (see dbg* below). These live up here (no
// SdFat dependency) so loop()/watchdog/writeSessionSample can feed them; the
// 1 Hz health line + file live in the SD section. dbg_loop_max_us catches loop
// stalls (the suspected GPS-stale cause), dbg_sdwr_max_us catches SD latency
// spikes, dbg_fresh_1s is the real PVT rate.
static uint32_t dbg_loop_max_us = 0;
static uint32_t dbg_sdwr_max_us = 0;
static uint32_t dbg_fresh_1s    = 0;

static uint32_t gps_baud_now = 0;   // baud we actually connected the module at
static uint8_t  gps_nav_hz   = NAV_RATE_HZ;   // live nav rate (dash: CFG,gpshz,<n>)
// begin() blocks up to maxWait waiting for the module. Keep it SHORT off the
// boot path (watchdog re-begin / live baud scan) so a failed attempt can't
// freeze the 25 Hz loop for seconds (that blocking IS what causes GPS-stale).
static constexpr uint16_t GPS_BEGIN_WAIT_FAST = 600;

// MODULE-RESET FORENSICS (v0.1.102). UBX-NAV-STATUS rides the same auto stream
// as PVT and carries msss = milliseconds since MODULE startup/reset. If msss
// ever goes BACKWARDS, the u-blox rebooted — decisive, courtroom-grade proof
// of a power glitch (vs UART/parser trouble). Logged in the .dbg health line
// as msss/mrst. The racing-only stales all show avail=0 (module electrically
// silent) — this counter settles whether those are module reboots.
static volatile uint32_t gps_msss          = 0;
static volatile uint32_t gps_module_resets = 0;
static void navStatusCB(UBX_NAV_STATUS_data_t *d) {
    const uint32_t m = d->msss;
    if (gps_msss > 3000 && m + 1000 < gps_msss) {   // clock went backwards = reboot
        gps_module_resets++;
        Serial.printf("[gps] MODULE RESET detected: msss %lu -> %lu (reset #%lu)\n",
                      (unsigned long)gps_msss, (unsigned long)m,
                      (unsigned long)gps_module_resets);
    }
    gps_msss = m;
}

// Change the u-blox nav (PVT) rate on demand. Lower Hz = far less UART traffic
// (10 Hz PVT is ~26% of 38400 vs ~65% at 25 Hz) = more headroom against stalls.
static void applyGpsHz(uint8_t hz) {
    if (hz < 1)  hz = 1;
    if (hz > 25) hz = 25;
    gps_nav_hz = hz;
    if (gnss_lib_ok) {
        myGNSS.setNavigationFrequency(hz);
        myGNSS.saveConfiguration();      // persist so a reset restores this rate + UBX
        gnss_last_fresh_ms = millis();   // grace so the watchdog doesn't trip on the reconfig
    }
    Serial.printf("[gps] nav rate -> %u Hz\n", (unsigned)hz);
}
static bool tryConnectGNSS(uint32_t baud, uint16_t maxWait = 1100) {
    GPS_SERIAL.begin(baud);
    delay(50);
    if (myGNSS.begin(GPS_SERIAL, maxWait)) { gps_baud_now = baud; return true; }
    return false;
}

// Status code matching the wire format documented at the top of the file.
static uint8_t gpsStatus() {
    const uint32_t age = millis() - gnss_last_fresh_ms;
    if (gnss_lib_ok)        return (age < 1500) ? 2 /*OK*/   : 3 /*STALE*/;
    if (gnss_raw_bytes > 0) return (age < 1500) ? 1 /*RAW*/  : 0 /*OFF*/;
    return 0; // OFF — never seen a byte
}

// Called every loop(); self-rate-limited. Recovery strategy documented on the
// GPS_STALE_* constants above. No-op until the link is up and has produced at
// least one PVT, so it never fires during cold-start acquisition.
static void gpsStaleWatchdog() {
    // DEAD-AT-BOOT RECOVERY (review fix): if the boot-time connect failed,
    // gnss_lib_ok is false and — before this — NOTHING ever retried: GPS stayed
    // OFF for the entire session. The two bogus-2019-epoch sessions at Thompson
    // (no time source = GPS dead from power-on) prove this happened on track.
    // A module that was cranky at power-on (brownout sag during startup, hot
    // restart, etc.) is now picked up as soon as it recovers: retry every 30 s
    // (~0.9 s bounded stall), with a wider baud scan every 6th try (covers a
    // module whose SAVED baud isn't 38400/9600 — see the boot-scan note).
    if (!gnss_lib_ok) {
        static uint32_t last_late_try_ms = 0;
        static uint8_t  late_tries = 0;
        const uint32_t nowl = millis();
        if (nowl - last_late_try_ms < 30000 || nowl < 15000) return;
        last_late_try_ms = nowl;
        bool ok = tryConnectGNSS(GPS_BAUD_PRIMARY, 400);
        if (!ok && (++late_tries % 6) == 0) {
            const uint32_t scan[] = { 9600, 230400, 115200, 460800 };
            for (uint32_t b : scan) if (tryConnectGNSS(b, 400)) { ok = true; break; }
        }
        if (ok) {
            myGNSS.setUART1Output(COM_TYPE_UBX, 250);
            myGNSS.setNavigationFrequency(gps_nav_hz, 250);
            myGNSS.setAutoPVT(true, (uint16_t)250);
            myGNSS.setAutoNAVSTATUScallbackPtr(&navStatusCB, 250);
            myGNSS.saveConfiguration(250);
            gnss_lib_ok = true;
            Serial.printf("[gps] LATE connect @ %lu baud (dead at boot, recovered)\n",
                          (unsigned long)gps_baud_now);
        }
        return;
    }
    if (gnss_last_fresh_ms == 0) return;
    const uint32_t now = millis();
    const uint32_t age = now - gnss_last_fresh_ms;
    if (age < GPS_STALE_RECOVER_MS) return;            // healthy — nothing to do

    // LIGHT: flush the RX ring (non-blocking) so the parser resyncs on a live frame.
    if (now - gnss_last_recover_ms >= GPS_RECOVER_INTERVAL_MS) {
        gnss_last_recover_ms = now;
        gnss_recover_count++;
        uint32_t drained = 0;
        while (GPS_SERIAL.available()) { (void)GPS_SERIAL.read(); drained++; }
        Serial.printf("[gps] STALE %lums -> flush+resync #%lu (dropped %lu bytes)%s\n",
                      (unsigned long)age, (unsigned long)gnss_recover_count,
                      (unsigned long)drained, recording_active ? " [REC]" : "");
    }

    // HEAVY: module may have glitched — re-establish the link. Blocking but bounded.
    if (age >= GPS_REINIT_MS && now - gnss_last_reinit_ms >= GPS_REINIT_INTERVAL_MS) {
        gnss_last_reinit_ms = now;
        gnss_reinit_count++;
        // COST CONTROL (0.1.91): the old "scan all 5 bauds + re-config" every
        // heavy attempt froze the loop up to ~9 s per recovery (the 0.1.88 debug
        // logs). A module that just glitched/reset almost always comes back at
        // the SAME baud (BBR preserved), so:
        //   - try ONLY the current baud (600 ms) + reassert config with SHORT
        //     (250 ms) maxWaits  => normal recovery ~1.3 s, not 9 s.
        //   - only after several consecutive failures (likely a real baud change
        //     from a full power loss) do the expensive 5-baud scan, and at most
        //     once in a while. This bounds the per-attempt loop stall.
        static uint8_t heavy_fail_streak = 0;
        // tryConnectGNSS re-inits Serial2 too, so this also clears a UART framing
        // glitch, not just a module reset. NOTE: SparkFun begin() polls TWICE
        // internally, so effective block ~= 2x maxWait. Keep maxWait SMALL (400)
        // because a genuinely DEAD module (the 0.1.91 logs: it goes fully silent
        // once hot and can't be revived in SW) would otherwise freeze the loop
        // ~9 s per attempt for nothing — that froze RPM/logging too.
        bool reok = tryConnectGNSS(gps_baud_now ? gps_baud_now : GPS_BAUD_PRIMARY, 400);
        if (!reok && heavy_fail_streak >= 6) {
            // Rare path: a full reset MAY have changed the baud. Only the two
            // u-blox defaults, short waits, so a dead module can't cost ~9 s.
            const uint32_t scan[] = { 38400, 9600 };
            for (uint32_t b : scan) {
                if (b == gps_baud_now) continue;
                if (tryConnectGNSS(b, 400)) { reok = true; break; }
            }
            heavy_fail_streak = 0;
        }
        if (reok) {
            heavy_fail_streak = 0;
            myGNSS.setUART1Output(COM_TYPE_UBX, 250);
            myGNSS.setNavigationFrequency(gps_nav_hz, 250);
            myGNSS.setAutoPVT(true, (uint16_t)250);
            myGNSS.setAutoNAVSTATUScallbackPtr(&navStatusCB, 250);   // keep reset forensics alive
            gnss_last_fresh_ms = millis();   // grace window so we don't re-trip instantly
            Serial.printf("[gps] re-begin OK @ %lu baud\n", (unsigned long)gps_baud_now);
        } else {
            heavy_fail_streak++;
            Serial.printf("[gps] re-begin FAILED (module silent) streak=%u\n",
                          (unsigned)heavy_fail_streak);
        }
    }
}

// Change the GPS UART baud on demand (from the dash CFG,gpsbaud,<n>). Tells the
// module to switch UART1, re-handshakes on the Teensy side, and if that fails
// SCANS known bauds so a bad choice can never brick the link (the user can just
// pick another rate). Reports GPSBAUD,<actual_baud>,<ok> back to the dash so the
// settings page shows OK / NO DATA immediately.
static void applyGpsBaud(uint32_t target) {
    Serial.printf("[gps] applyGpsBaud -> %lu (from %lu)\n",
                  (unsigned long)target, (unsigned long)gps_baud_now);
    bool ok = false;
    // One deliberate switch attempt at the requested baud.
    if (gnss_lib_ok) {
        myGNSS.setSerialRate(target);
        delay(120);
        GPS_SERIAL.begin(target);
        delay(60);
        ok = myGNSS.begin(GPS_SERIAL, GPS_BEGIN_WAIT_FAST);
        if (ok) gps_baud_now = target;
    }
    // If the switch didn't take, DON'T keep hammering the target (that's what
    // freaks the module out). Recover to whatever baud the module is still on,
    // with SHORT waits so the whole scan is bounded (~3 s worst case, not ~16 s).
    if (!ok) {
        const uint32_t scan[] = { 230400, 115200, 38400, 9600, 460800 };
        for (uint32_t b : scan) { if (tryConnectGNSS(b, GPS_BEGIN_WAIT_FAST)) { ok = true; break; } }
    }
    if (ok) {
        myGNSS.setUART1Output(COM_TYPE_UBX);
        myGNSS.setNavigationFrequency(gps_nav_hz);
        myGNSS.setAutoPVT(true);
        myGNSS.setAutoNAVSTATUScallbackPtr(&navStatusCB, 250);
        myGNSS.saveConfiguration();      // persist new baud+config so a reset restores it (stale fix)
        gnss_lib_ok = true;
        gnss_last_fresh_ms = millis();   // grace so the watchdog doesn't instantly trip
    } else {
        gnss_lib_ok = false;
    }
    DASH_SERIAL.printf("GPSBAUD,%lu,%d\n", (unsigned long)gps_baud_now, (int)ok);
    Serial.printf("[gps] applyGpsBaud done: baud=%lu ok=%d\n",
                  (unsigned long)gps_baud_now, (int)ok);
}

// ---------------------------------------------------------------------------
// Tach state — FreqMeasureMulti on pin 9. Used in Direct sensor mode only.
// Interrupt-driven; we drain the FIFO in loop().
// ---------------------------------------------------------------------------
static FreqMeasureMulti tach;          // FlexPWM2_2_B input capture on pin 9

// Noisy opto-tach conditioning. The raw signal has spurious edges (ringing,
// EMI, opto not pulling fully low) that show up as very short pulse periods =
// implausibly high instantaneous RPM. The old "average every period in the
// window" math let a SINGLE glitch tank the average period and spike RPM all
// over the place. We now run a two-stage filter on a PER-PULSE basis:
//   1. Glitch gate  — drop any pulse whose instantaneous RPM is outside a
//                      plausible band (kills the high-frequency noise spikes).
//   2. Median-of-5  — kills isolated outliers that slip past the gate.
//   3. EMA          — smooths the median so the displayed value doesn't twitch.
// Tune RPM_EMA_ALPHA for responsiveness vs. smoothness (lower = smoother).
static constexpr uint16_t RPM_MAX_PLAUSIBLE = 12000;   // reject pulses implying > this RPM (noise)
static constexpr float    RPM_EMA_ALPHA     = 1.0f;    // 0..1; 1.0 = EMA off (snappiest), lower = smoother/laggier
static constexpr uint8_t  RPM_MEDIAN_N      = 3;        // median window over recent good pulses (odd; >=3 kills single spikes)

static double   rpm_inst_ring[RPM_MEDIAN_N] = {0};      // recent good instantaneous RPM
static uint8_t  rpm_ring_head     = 0;
static uint8_t  rpm_ring_fill     = 0;
static float    rpm_ema           = 0.0f;
static uint32_t rpm_last_pulse_ms = 0;
static uint32_t rpm_spike_rej_ms  = 0;   // first-rejection time of the current spike streak

static double rpmRingMedian() {
    if (rpm_ring_fill == 0) return 0.0;
    double tmp[RPM_MEDIAN_N];
    for (uint8_t i = 0; i < rpm_ring_fill; i++) tmp[i] = rpm_inst_ring[i];
    // insertion sort (tiny N)
    for (uint8_t i = 1; i < rpm_ring_fill; i++) {
        double v = tmp[i]; int8_t j = i - 1;
        while (j >= 0 && tmp[j] > v) { tmp[j + 1] = tmp[j]; j--; }
        tmp[j + 1] = v;
    }
    return tmp[rpm_ring_fill / 2];
}

static void pumpTach() {
    while (tach.available()) {
        const double period = tach.read();
        if (period <= 0) continue;
        const double freq_hz = FreqMeasureMulti::countToFrequency(period);
        const double ppr = (g_cfg.rpm_ppr_x10 > 0) ? (g_cfg.rpm_ppr_x10 / 10.0)
                                                    : (double)RPM_PULSES_PER_REV;
        const double inst = (freq_hz * 60.0) / ppr;
        // Stage 1: glitch gate — drop noise spikes / nonsense periods outright.
        if (inst <= 0.0 || inst > (double)RPM_MAX_PLAUSIBLE) continue;
        // Stage 1.5: SLEW/SPIKE GATE (dash "RPM spike filter", CFG,rpmspk).
        // The absolute gate + median only kill SINGLE outliers above 12 k; a
        // noise BURST (several spurious edges) or a spike at a plausible value
        // (e.g. 8 k while the engine's at 3 k) sailed through and slammed the
        // bar. Physics: an engine can't change speed faster than ~a few 1000
        // RPM/s, but noise "jumps" instantly — so reject any pulse implying a
        // change from the current filtered value faster than the level's max
        // slew. Escape hatches so a REAL level shift can never be locked out:
        // (a) the allowance grows with time since the last ACCEPTED pulse, and
        // (b) after CONFIRM ms of continuous rejection the filter resets and
        // accepts the new level (a genuine jump lands within ~0.2–0.3 s).
        if (g_cfg.rpm_spike > 0 && rpm_ema > 400.0f) {
            static constexpr float    SLEW_RPM_S[4] = { 0.f, 20000.f, 10000.f, 5000.f };
            static constexpr uint16_t CONFIRM_MS[4] = { 0, 120, 200, 300 };
            const uint8_t  sp    = (g_cfg.rpm_spike > 3) ? 3 : g_cfg.rpm_spike;
            const uint32_t nowms = millis();
            float allow = SLEW_RPM_S[sp] * (float)(nowms - rpm_last_pulse_ms) * 0.001f;
            if (allow < 250.0f) allow = 250.0f;   // floor: normal pulse-to-pulse jitter
            if (fabs(inst - (double)rpm_ema) > (double)allow) {
                if (rpm_spike_rej_ms == 0) rpm_spike_rej_ms = nowms;
                if (nowms - rpm_spike_rej_ms < CONFIRM_MS[sp]) continue;   // reject pulse
                // Sustained — it's real. Reset the filter so it re-seeds at the
                // new level immediately instead of slewing there.
                rpm_ring_head = 0; rpm_ring_fill = 0; rpm_ema = 0.0f;
                rpm_spike_rej_ms = 0;
            } else {
                rpm_spike_rej_ms = 0;
            }
        }
        // Stage 2: feed the median ring.
        rpm_inst_ring[rpm_ring_head] = inst;
        rpm_ring_head = (rpm_ring_head + 1) % RPM_MEDIAN_N;
        if (rpm_ring_fill < RPM_MEDIAN_N) rpm_ring_fill++;
        // Stage 3: smoothing, trimmed by the dash's RPM-smoothing slider
        // (g_cfg.rpm_smooth, -10..+10) — GENTLE / scaled-down so each step is a
        // small nudge, not a drastic jump (the old 0.09/step down to alpha 0.10
        // was way too harsh; going negative also did a hard median->raw flip):
        //    0  = median + EMA off  (crisp baseline — the sweet spot)
        //   >0  = median + LIGHT EMA, ~4.5%/step, floored at alpha 0.5 (moderate
        //         even at +10, never molasses)
        //   <0  = blend the raw latest sample INTO the median, 10%/step, so it
        //         gets snappier gradually instead of flipping straight to raw.
        const int    sm     = g_cfg.rpm_smooth;
        const double median = rpmRingMedian();
        double       med;
        if (sm < 0) { const double f = (double)(-sm) / 10.0; med = inst * f + median * (1.0 - f); }
        else        { med = median; }
        float        alpha  = (sm <= 0) ? RPM_EMA_ALPHA : (RPM_EMA_ALPHA - sm * 0.045f);
        if (alpha < 0.5f) alpha = 0.5f;
        if (rpm_ema <= 0.0f) rpm_ema = (float)med;
        else                 rpm_ema += alpha * ((float)med - rpm_ema);
        rpm_last_pulse_ms = millis();
    }
}

static uint16_t computeRpmAndReset() {
    if (millis() - rpm_last_pulse_ms > RPM_TIMEOUT_MS) {
        // Engine stopped / signal lost — reset the filter so it ramps cleanly
        // from zero on the next pulse rather than EMA-decaying from a stale value.
        rpm_ring_head = 0; rpm_ring_fill = 0; rpm_ema = 0.0f; rpm_spike_rej_ms = 0;
        return 0;
    }
    if (rpm_ema < 0.0f)         return 0;
    if (rpm_ema > 65535.0f)     return 65535;
    return (uint16_t)(rpm_ema + 0.5f);
}

// ---------------------------------------------------------------------------
// MS3Pro CAN bus — FlexCAN_T4 on CAN1 (TX=22, RX=23).
//
// MS3Pro "Simplified Dash Broadcasting": frames at CAN_BASE_ID (0x5E8/1512)
// + 0..4, 8 bytes each, big-endian. Layout per the official spec (and the
// captured 0x5E8 frame confirms it). We parse the frames carrying what the
// dash shows:
//
//   0x5E8  bytes 0-1  map_x10    (int16,  kPa × 10)
//          bytes 2-3  rpm        (uint16, 1 RPM/bit)
//          bytes 4-5  clt_f_x10  (int16,  °F  × 10  — TunerStudio units assumed °F)
//          bytes 6-7  tps_x10    (int16,  %   × 10)
//   0x5E9  bytes 4-5  iat_f_x10  (int16,  °F  × 10, field "mat")
//   0x5EA  byte  0    afrtgt_x10 (uint8,  AFR × 10  — target, not stored)
//          byte  1    afr_x10    (uint8,  AFR × 10, field "AFR1", e.g. 147 = 14.7)
//   0x5EB  bytes 0-1  bat_x10    (int16,  V   × 10)
//
// Note: afr/afrtgt are SINGLE bytes (0-255 = 0.0-25.5 AFR), the rest are 16-bit.
// All fields default to -1 until a valid frame arrives. If no frames arrive
// for CAN_STALE_MS the struct resets to -1 so the dash shows '---'.
// ---------------------------------------------------------------------------
static FlexCAN_T4<CAN1, RX_SIZE_256, TX_SIZE_16> Can1;

static struct CanEcu {
    uint16_t rpm        = 0;
    int16_t  clt_f_x10  = -1;
    int16_t  map_x10    = -1;
    int16_t  tps_x10    = -1;
    int16_t  afr_x10    = -1;
    int16_t  iat_f_x10  = -1;
    int16_t  bat_x10    = -1;
    uint32_t last_ms    = 0;   // millis() of most recent valid frame (any ID)
} can_ecu;

// CAN RX diagnostics — independent of whether any frame ID matched our parser.
// can_rx_total counts EVERY frame seen on the bus since boot; the seen-ID ring
// records the distinct IDs observed in the last report window. This is what
// tells us, over USB serial, which link in the chain is broken:
//   total stays 0          -> nothing on the bus (wiring / transceiver / broadcast off)
//   total climbs, no 0x5E8 -> wrong broadcast mode/base (e.g. Advanced 0x5F0, not
//                             Simplified Dash 0x5E8) — reconfigure TunerStudio
//   total climbs, 0x5E8 ok -> data is arriving + parsed; check dash sensor_type
static struct CanDiag {
    uint32_t rx_total      = 0;    // every frame ever read (any ID)
    uint32_t rx_window     = 0;    // frames since last 1 Hz report
    uint32_t dup_window    = 0;    // frames byte-identical to the previous one
    uint32_t ids_seen[8]   = {0};  // distinct IDs in this window (ring, dropped if >8)
    uint8_t  ids_count     = 0;
    uint8_t  base_hits     = 0;    // frames matching CAN_BASE_ID+0..3 this window
    uint32_t last_report_ms = 0;
    uint8_t  last_buf[8]   = {0};  // payload of previous frame (for dup detection)
    uint8_t  last_len      = 0xFF;
    uint32_t last_id       = 0xFFFFFFFF;
} can_diag;

static void canDiagNote(uint32_t id, uint8_t len, const uint8_t* buf) {
    can_diag.rx_total++;
    can_diag.rx_window++;
    // Duplicate detection: a healthy bus carries CHANGING data; a saturated
    // ACK-error retransmit storm repeats one frozen frame. If ~100% of frames
    // are byte-identical to the previous one at ~3000+ fps, it's a storm.
    if (id == can_diag.last_id && len == can_diag.last_len &&
        memcmp(buf, can_diag.last_buf, len > 8 ? 8 : len) == 0) {
        can_diag.dup_window++;
    }
    can_diag.last_id = id; can_diag.last_len = len;
    memcpy(can_diag.last_buf, buf, len > 8 ? 8 : len);
    for (uint8_t i = 0; i < can_diag.ids_count; i++)
        if (can_diag.ids_seen[i] == id) return;     // already recorded
    if (can_diag.ids_count < 8)
        can_diag.ids_seen[can_diag.ids_count++] = id;
}

// Call at ~1 Hz from emitToDash(). Prints a one-line CAN health summary to the
// USB serial monitor and resets the window counters.

// CAN (re)init sequence, factored out so the TX self-test can recover the
// controller from a bus-off without rebooting.
static void canBegin() {
    Can1.begin();
    Can1.setBaudRate(CAN_BAUD);   // normal mode -> FlexCAN auto-ACKs
    Can1.setMaxMB(16);
    Can1.enableFIFO();
}

// One-shot TX/ACK self-test state. Proves whether the Teensy can actually put
// a frame on the bus and get it ACKed (i.e. is the transmit/transceiver path
// alive). 0=waiting, 1=frame sent (awaiting evaluation next report), 2=done.
static uint8_t  can_tx_test_state  = 0;
static uint8_t  can_tx_test_result = 0;   // 0=untested, 1=PASS, 2=FAIL

static void canDiagReport() {
    const uint32_t now = millis();
    if (now - can_diag.last_report_ms < 1000) return;
    can_diag.last_report_ms = now;
    char ids[64]; int n = 0; ids[0] = 0;
    for (uint8_t i = 0; i < can_diag.ids_count && n < (int)sizeof(ids) - 8; i++)
        n += snprintf(ids + n, sizeof(ids) - n, "%s0x%lX",
                      i ? "," : "", (unsigned long)can_diag.ids_seen[i]);
    // NOTE: v0.1.47 added an ACTIVE TX heartbeat here to test the ACK path. That
    // was a mistake — on a bus where the node can't get an ACK, repeated failed
    // transmits drive TX_ERR to 256 -> BUS-OFF, which kills RX too ("now we have
    // nothing"). REMOVED. Diagnostics are now strictly PASSIVE: we only listen,
    // never transmit, so we can never push the node off the bus ourselves.
    //
    // Pull FlexCAN's error/bus state. events() populates the ESR1 capture buffer
    // without draining the FIFO (FIFO interrupt isn't enabled), so it's safe to
    // call alongside our read()-polling in pumpCAN(). A receiver that gets frames
    // with no ACK still registers ACK_ERR passively — no transmit needed.
    Can1.events();
    CAN_error_t err;
    const bool have_err = Can1.error(err, false);

    // TX/ACK self-test REMOVED (v0.1.55). v0.1.54 transmitted a probe frame to
    // test the ACK path; on this bus the transmit can't complete, so the node
    // bus-offed and reception died ("now I'm getting nothing"). That outcome is
    // itself the proof: a working TX path would have sent one frame, gotten
    // ACKed, and kept running. Since transmitting kills the bus, the Teensy's
    // transmit/transceiver path is dead. There is NO firmware fix for that, so
    // we go back to STRICTLY PASSIVE listening and never transmit again — that
    // restores reception of whatever the MS3 puts on the wire.
    (void)can_tx_test_state; (void)can_tx_test_result;

    const uint32_t dpct = can_diag.rx_window ? (can_diag.dup_window * 100UL / can_diag.rx_window) : 0;
    Serial.printf("CANDIAG frames/s=%lu dup=%lu%% total=%lu base_hits=%u ids=[%s] "
                  "state=%s ACK_ERR=%d CRC_ERR=%d FRM=%d STF=%d TXerr=%u RXerr=%u flt=%s\n",
                  (unsigned long)can_diag.rx_window, (unsigned long)dpct,
                  (unsigned long)can_diag.rx_total, can_diag.base_hits, ids,
                  have_err ? (char*)err.state : "?",
                  have_err ? err.ACK_ERR : 0, have_err ? err.CRC_ERR : 0,
                  have_err ? err.FRM_ERR : 0, have_err ? err.STF_ERR : 0,
                  have_err ? err.TX_ERR_COUNTER : 0, have_err ? err.RX_ERR_COUNTER : 0,
                  have_err ? (char*)err.FLT_CONF : "?");
    // Also surface it on the dash link so it can be shown without a USB cable.
    // CANDIAG,<frames/s>,<total>,<base_hits>,<dup%>,<ACK_ERR>,<TXerr>,<RXerr>,<txtest>
    DASH_SERIAL.printf("CANDIAG,%lu,%lu,%u,%lu,%d,%u,%u,%u\n",
                       (unsigned long)can_diag.rx_window,
                       (unsigned long)can_diag.rx_total, can_diag.base_hits,
                       (unsigned long)dpct, have_err ? err.ACK_ERR : 0,
                       have_err ? err.TX_ERR_COUNTER : 0,
                       have_err ? err.RX_ERR_COUNTER : 0,
                       can_tx_test_result);
    can_diag.rx_window = 0;
    can_diag.dup_window = 0;
    can_diag.ids_count = 0;
    can_diag.base_hits = 0;
}

// CAN sniffer: when cansniff_active (declared near the top), EVERY frame on
// the bus (any ID) is logged to /cansniff/cansniff_<unix>.csv on the SD card
// so the real MS3Pro broadcast layout can be analysed offline.
static void pumpCAN() {
    CAN_message_t msg;
    const uint32_t now = millis();
    while (Can1.read(msg)) {
        // Diagnostics: count EVERY frame + record its ID/payload, regardless of match.
        canDiagNote(msg.id, msg.len, msg.buf);
        if (msg.id >= CAN_BASE_ID && msg.id <= CAN_BASE_ID + 3) can_diag.base_hits++;
        // Sniffer: capture the raw frame (any ID) before our targeted parse.
        if (cansniff_active) {
            cansniffLog(msg.id, msg.flags.extended, msg.len, msg.buf);
        }
        switch (msg.id) {
            case CAN_BASE_ID + 0:   // 0x5E8: map, rpm, clt, tps
                can_ecu.map_x10    = (int16_t)(((uint16_t)msg.buf[0] << 8) | msg.buf[1]);
                can_ecu.rpm        =          (((uint16_t)msg.buf[2] << 8) | msg.buf[3]);
                can_ecu.clt_f_x10  = (int16_t)(((uint16_t)msg.buf[4] << 8) | msg.buf[5]);
                can_ecu.tps_x10    = (int16_t)(((uint16_t)msg.buf[6] << 8) | msg.buf[7]);
                can_ecu.last_ms    = now;
                break;
            case CAN_BASE_ID + 1:   // 0x5E9: pw1, pw2, mat(IAT), adv_deg
                can_ecu.iat_f_x10  = (int16_t)(((uint16_t)msg.buf[4] << 8) | msg.buf[5]);
                can_ecu.last_ms    = now;
                break;
            case CAN_BASE_ID + 2:   // 0x5EA: afrtgt1, AFR1, EGOcor1, egt1, pwseq1
                // afrtgt1 (byte 0) and AFR1 (byte 1) are single bytes = AFR * 10.
                can_ecu.afr_x10    = (int16_t)msg.buf[1];   // AFR1 (cyl#1), e.g. 147 = 14.7
                can_ecu.last_ms    = now;
                break;
            case CAN_BASE_ID + 3:   // 0x5EB: batt, sensors1, sensors2, knk_rtd
                can_ecu.bat_x10    = (int16_t)(((uint16_t)msg.buf[0] << 8) | msg.buf[1]);
                can_ecu.last_ms    = now;
                break;
            default:
                break;
        }
    }
    // Staleness guard — if the MS3Pro goes silent, reset all fields to -1
    // so the dash shows '---' instead of frozen last-known values.
    if (can_ecu.last_ms != 0 && now - can_ecu.last_ms > CAN_STALE_MS) {
        can_ecu = CanEcu{};
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
static float imu_temp_c = -999.0f;   // MPU-6050 die temp (enclosure ambient proxy)

static struct {
    float   ax_sum = 0, ay_sum = 0, az_sum = 0;
    float   gx_sum = 0, gy_sum = 0, gz_sum = 0;
    uint32_t n     = 0;
    // Last averaged output (used by emitToDash):
    float   ax = 0, ay = 0, az = 0;
    float   gx = 0, gy = 0, gz = 0;
} imu;

// --- Boot-time auto-calibration (see CLAUDE.md "IMU auto-calibration") -------
// Applied to every read: gyro bias is SUBTRACTED, accel is SCALE-normalized so
// |a| == 1 g. Offsets are recomputed every boot (gyro bias drifts with
// temperature) but only COMMITTED when the device is provably still during the
// cal window; otherwise the last-good EEPROM offsets are retained.
static struct {
    float gx_off = 0, gy_off = 0, gz_off = 0;   // gyro bias, deg/s (subtracted)
    float a_scale = 1.0f;                         // accel scale so |a|=1g
    bool  valid   = false;                        // loaded or freshly calibrated?
} imuCal;

// Stillness gate: max peak-to-peak over the cal window to accept a fresh cal.
// At rest we measured ~0.2 dps / ~0.02 g jitter, so these are ~10x headroom.
constexpr float GYRO_STILL_DPS = 2.0f;
constexpr float ACCEL_STILL_G  = 0.10f;

// EEPROM persistence of last-good offsets (Teensy flash-emulated EEPROM @ addr 0;
// nothing else on the Teensy uses EEPROM — see CLAUDE.md).
struct ImuCalStore { uint16_t magic; float gx_off, gy_off, gz_off, a_scale; };
constexpr uint16_t IMUCAL_MAGIC   = 0xCA15;
constexpr int      IMUCAL_EE_ADDR = 0;

static void loadImuCal() {
    ImuCalStore s;
    EEPROM.get(IMUCAL_EE_ADDR, s);
    if (s.magic == IMUCAL_MAGIC && s.a_scale > 0.5f && s.a_scale < 2.0f) {
        imuCal.gx_off = s.gx_off; imuCal.gy_off = s.gy_off; imuCal.gz_off = s.gz_off;
        imuCal.a_scale = s.a_scale; imuCal.valid = true;
        Serial.printf("IMU cal loaded (EEPROM): bias[%.2f %.2f %.2f] dps, scale %.4f\n",
                      imuCal.gx_off, imuCal.gy_off, imuCal.gz_off, imuCal.a_scale);
    }
}

static void saveImuCal() {
    ImuCalStore s{ IMUCAL_MAGIC, imuCal.gx_off, imuCal.gy_off, imuCal.gz_off, imuCal.a_scale };
    EEPROM.put(IMUCAL_EE_ADDR, s);   // .put == update: only writes changed bytes
}

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

// Read one 14-byte burst from the MPU-6050 into engineering units (RAW, no
// calibration applied). All 14 bytes are buffered first so the int16 assembly
// has a well-defined byte order (the old inline (read()<<8)|read() relied on
// unspecified C++ operand evaluation order). Returns false on I2C failure.
static bool readImuRaw(float a[3], float g[3]) {
    Wire.beginTransmission(MPU6050_ADDR);
    Wire.write(0x3B);   // ACCEL_XOUT_H — burst start
    Wire.endTransmission(false);
    if (Wire.requestFrom(MPU6050_ADDR, (uint8_t)14, (uint8_t)true) < 14) return false;
    uint8_t b[14];
    for (int i = 0; i < 14; i++) b[i] = Wire.read();
    a[0] = (int16_t)((b[0]  << 8) | b[1])  * (1.0f / 16384.0f);
    a[1] = (int16_t)((b[2]  << 8) | b[3])  * (1.0f / 16384.0f);
    a[2] = (int16_t)((b[4]  << 8) | b[5])  * (1.0f / 16384.0f);
    // b[6..7] = die temperature. Now DECODED (heat diagnostics): the MPU-6050
    // sits in the same enclosure, so it's a decent ambient-temp proxy.
    imu_temp_c = (int16_t)((b[6] << 8) | b[7]) / 340.0f + 36.53f;
    g[0] = (int16_t)((b[8]  << 8) | b[9])  * (1.0f / 131.0f);
    g[1] = (int16_t)((b[10] << 8) | b[11]) * (1.0f / 131.0f);
    g[2] = (int16_t)((b[12] << 8) | b[13]) * (1.0f / 131.0f);
    return true;
}

// Boot-time calibration. Loads last-good offsets from EEPROM first (the
// fallback), then samples ~0.5 s at rest. If the device is moving during the
// window (peak-to-peak over threshold) the fresh cal is REJECTED and the loaded
// offsets stand — this is what stops a calibration taken while driving / idling
// from corrupting the whole session. Otherwise: gyro bias = mean (true rest
// value is 0); accel scale = 1/|a| so gravity reads exactly 1 g regardless of
// how the unit is oriented at boot (orientation-independent — no assumption the
// car is level). Committed offsets are written back to EEPROM.
static void calibrateIMU() {
    if (!imu_present) return;
    loadImuCal();                         // fallback if the gate rejects this cal
    delay(50);                            // let the gyro PLL settle after wake

    float a[3], g[3];
    for (int i = 0; i < 10; i++) { readImuRaw(a, g); delay(2); }   // discard warm-up

    constexpr int N = 200;
    double as[3] = {0,0,0}, gs[3] = {0,0,0};
    float  amin[3], amax[3], gmin[3], gmax[3];
    for (int k = 0; k < 3; k++) { amin[k]=1e9f; amax[k]=-1e9f; gmin[k]=1e9f; gmax[k]=-1e9f; }
    int got = 0;
    for (int i = 0; i < N; i++) {
        if (!readImuRaw(a, g)) { delay(2); continue; }
        for (int k = 0; k < 3; k++) {
            as[k] += a[k]; gs[k] += g[k];
            if (a[k] < amin[k]) amin[k] = a[k];  if (a[k] > amax[k]) amax[k] = a[k];
            if (g[k] < gmin[k]) gmin[k] = g[k];  if (g[k] > gmax[k]) gmax[k] = g[k];
        }
        got++;
        delay(2);
    }
    if (got < N / 2) { Serial.println(F("IMU cal: too few reads, keeping prior offsets")); return; }

    const float inv = 1.0f / got;
    float am[3], gm[3];
    for (int k = 0; k < 3; k++) { am[k] = as[k] * inv; gm[k] = gs[k] * inv; }

    float gp2p = 0, ap2p = 0;
    for (int k = 0; k < 3; k++) {
        float gd = gmax[k] - gmin[k]; if (gd > gp2p) gp2p = gd;
        float ad = amax[k] - amin[k]; if (ad > ap2p) ap2p = ad;
    }
    if (gp2p > GYRO_STILL_DPS || ap2p > ACCEL_STILL_G) {
        Serial.printf("IMU cal ABORTED — device moving (gyro p2p=%.2f dps, accel p2p=%.3f g); using %s offsets\n",
                      gp2p, ap2p, imuCal.valid ? "stored" : "zero");
        return;
    }

    imuCal.gx_off = gm[0]; imuCal.gy_off = gm[1]; imuCal.gz_off = gm[2];
    const float mag = sqrtf(am[0]*am[0] + am[1]*am[1] + am[2]*am[2]);
    if (mag > 0.5f && mag < 1.5f) imuCal.a_scale = 1.0f / mag;   // else keep prior scale
    imuCal.valid = true;
    saveImuCal();
    Serial.printf("IMU calibrated: gyro bias[%.2f %.2f %.2f] dps, |a|=%.3f g -> scale %.4f\n",
                  imuCal.gx_off, imuCal.gy_off, imuCal.gz_off, mag, imuCal.a_scale);
}

// Read one burst and accumulate CALIBRATED samples into the averager.
// Returns silently if the device is absent or the burst is incomplete.
static void readIMU() {
    if (!imu_present) return;
    static uint32_t lastReadMs = 0;
    const uint32_t now = millis();
    if (now - lastReadMs < 4) return;   // cap at ~250 Hz
    lastReadMs = now;

    float a[3], g[3];
    if (!readImuRaw(a, g)) return;
    imu.ax_sum += a[0] * imuCal.a_scale;
    imu.ay_sum += a[1] * imuCal.a_scale;
    imu.az_sum += a[2] * imuCal.a_scale;
    imu.gx_sum += g[0] - imuCal.gx_off;
    imu.gy_sum += g[1] - imuCal.gy_off;
    imu.gz_sum += g[2] - imuCal.gz_off;
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

// Forward-decl-compatible accessor so callers defined ABOVE this section
// don't need to see the SdCardStatus enum / sd_card_status global directly.
// Declared at the top of the file with the rest of the forward decls.
static bool sdReady() { return sd_card_status == SD_CARD_READY; }

// Last SdFat error code from the most recent detectSD() failure path. Set on
// every NONE result so we can include it in the SD,* wire message AND log to
// USB serial; reset to 0 when a card mounts cleanly. SdFat error codes are
// documented in SdFat/src/common/SysCall.h (e.g. 0x14 = card init timeout).
static uint8_t sd_last_err  = 0;
static uint8_t sd_last_data = 0;

static void detectSD() {
    sd_total_mb = sd_free_mb = 0;
    sd_last_err = sd_last_data = 0;

    // Aggressive SDIO bring-up. The user's card + Teensy 4.1 slot combo can
    // require up to ~1 s of retries after a cold boot or unclean reset to
    // mount. 8 attempts * 100 ms gives the card plenty of time to settle.
    bool mounted = false;
    for (int attempt = 0; attempt < 8 && !mounted; ++attempt) {
        if (sdFat.begin(SdioConfig(FIFO_SDIO))) {
            mounted = true;
            break;
        }
        delay(100);
    }

    if (mounted) {
        sd_card_status = SD_CARD_READY;
        sd_total_mb = (uint32_t)(sdFat.card()->sectorCount() / 2048ULL);
        const uint64_t freeSects = (uint64_t)sdFat.vol()->freeClusterCount()
                                 * sdFat.vol()->sectorsPerCluster();
        sd_free_mb = (uint32_t)(freeSects / 2048ULL);
        return;
    }

    // Snapshot the error so we can report it to the dash + USB serial.
    sd_last_err  = sdFat.sdErrorCode();
    sd_last_data = sdFat.sdErrorData();

    // Mount failed. Probe the raw card to distinguish 'no card present' from
    // 'card present but FS unmountable'. Same retry budget for symmetry.
    SdioCard rawCard;
    for (int attempt = 0; attempt < 4; ++attempt) {
        if (rawCard.begin(SdioConfig(FIFO_SDIO))) {
            sd_card_status = SD_CARD_NEEDS_FMT;
            sd_total_mb = (uint32_t)(rawCard.sectorCount() / 2048ULL);
            sd_last_err = sd_last_data = 0;   // FS issue, not card issue
            return;
        }
        delay(100);
    }
    sd_card_status = SD_CARD_NONE;
    Serial.printf("[sd] detect FAILED  err=0x%02X data=0x%02X\n",
                  sd_last_err, sd_last_data);
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
            DASH_SERIAL.printf("SD,NONE,%02X%02X\n", sd_last_err, sd_last_data);
            break;
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

// Diagnostic: list SD directories with file sizes (triggered by USB "SDLS").
static void listSdContents() {
    if (!sdReady()) { Serial.println(F("[sdls] SD not ready")); return; }
    const char* dirs[] = { "/sessions", "/queue", "/cansniff" };
    for (const char* d : dirs) {
        File32 dir = sdFat.open(d);
        if (!dir || !dir.isDir()) { Serial.printf("[sdls] %s: (none)\n", d); continue; }
        Serial.printf("[sdls] %s:\n", d);
        File32 f;
        int n = 0;
        while ((f = dir.openNextFile())) {
            char nm[80]; f.getName(nm, sizeof(nm));
            Serial.printf("[sdls]    %-44s %10llu bytes\n", nm,
                          (unsigned long long)f.fileSize());
            f.close(); n++;
        }
        if (n == 0) Serial.println(F("[sdls]    (empty)"));
        dir.close();
    }
    Serial.println(F("[sdls] done"));
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
// CAN sniffer — dumps every raw CAN frame to /cansniff/cansniff_<unix>.csv
// so the actual MS3Pro broadcast layout can be reverse-engineered offline.
//
// CSV columns: t_ms,id,ext,dlc,d0,d1,d2,d3,d4,d5,d6,d7  (data bytes in hex)
//   t_ms : millis() since sniff start (monotonic, per-capture)
//   id   : CAN identifier in hex (e.g. 0x05F0)
//   ext  : 1 if 29-bit extended ID, 0 if 11-bit standard
//   dlc  : data length (0-8)
// Flushed every ~1 s so a power loss costs at most ~1 s of frames.
// ---------------------------------------------------------------------------
static File32   cansniff_file;
static bool     cansniff_file_open    = false;
static uint32_t cansniff_start_ms     = 0;
static uint32_t cansniff_last_flush_ms = 0;
static char     cansniff_path[80]     = "";

static void emitCanSniffStatus() {
    const char* fname = cansniff_path[0] ? cansniff_path : "";
    DASH_SERIAL.printf("CANSNIFF,%u,%s,%lu\n",
                       cansniff_active ? 1 : 0, fname,
                       (unsigned long)cansniff_frames);
    Serial.printf("[cansniff] %s file=%s frames=%lu\n",
                  cansniff_active ? "ACTIVE" : "STOPPED",
                  fname, (unsigned long)cansniff_frames);
}

static void openCanSniff() {
    cansniff_file_open     = false;
    cansniff_frames        = 0;
    cansniff_start_ms      = millis();
    cansniff_last_flush_ms = millis();
    cansniff_path[0]       = '\0';

    if (sd_card_status != SD_CARD_READY) {
        Serial.println(F("[cansniff] SD not ready — cannot start"));
        cansniff_active = false;
        emitCanSniffStatus();
        return;
    }
    if (!sdFat.exists("/cansniff") && !sdFat.mkdir("/cansniff")) {
        Serial.println(F("[cansniff] mkdir /cansniff failed"));
        cansniff_active = false;
        emitCanSniffStatus();
        return;
    }

    const uint32_t unix = (uint32_t)::now();
    if (unix > 1000000000UL) {
        snprintf(cansniff_path, sizeof(cansniff_path),
                 "/cansniff/cansniff_%lu.csv", (unsigned long)unix);
    } else {
        snprintf(cansniff_path, sizeof(cansniff_path),
                 "/cansniff/cansniff_nortc_%lu.csv", (unsigned long)millis());
    }
    if (!cansniff_file.open(cansniff_path, O_WRITE | O_CREAT | O_TRUNC)) {
        Serial.printf("[cansniff] open %s FAILED\n", cansniff_path);
        cansniff_path[0] = '\0';
        cansniff_active  = false;
        emitCanSniffStatus();
        return;
    }
    cansniff_file.print(F("t_ms,id,ext,dlc,d0,d1,d2,d3,d4,d5,d6,d7\n"));
    cansniff_file_open = true;
    cansniff_active    = true;
    Serial.printf("[cansniff] opened %s\n", cansniff_path);
    emitCanSniffStatus();
}

static void closeCanSniff() {
    cansniff_active = false;
    if (cansniff_file_open) {
        cansniff_file.sync();
        cansniff_file.close();
        cansniff_file_open = false;
    }
    emitCanSniffStatus();
    // Refresh free-MB so the dash's SD readout updates after a capture.
    if (sd_card_status == SD_CARD_READY) {
        const uint64_t freeSects = (uint64_t)sdFat.vol()->freeClusterCount()
                                 * sdFat.vol()->sectorsPerCluster();
        sd_free_mb = (uint32_t)(freeSects / 2048ULL);
    }
}

static void cansniffLog(uint32_t id, bool ext, uint8_t len, const uint8_t* buf) {
    if (!cansniff_file_open) return;
    if (len > 8) len = 8;
    char line[80];
    int n = snprintf(line, sizeof(line),
                     "%lu,0x%03lX,%u,%u",
                     (unsigned long)(millis() - cansniff_start_ms),
                     (unsigned long)id, ext ? 1 : 0, len);
    for (uint8_t i = 0; i < 8; ++i) {
        if (i < len) n += snprintf(line + n, sizeof(line) - n, ",%02X", buf[i]);
        else         n += snprintf(line + n, sizeof(line) - n, ",");
    }
    n += snprintf(line + n, sizeof(line) - n, "\n");
    cansniff_file.write((const uint8_t*)line, (size_t)n);
    cansniff_frames++;

    if (millis() - cansniff_last_flush_ms >= 1000) {
        cansniff_last_flush_ms = millis();
        cansniff_file.sync();
        emitCanSniffStatus();   // 1 Hz heartbeat with live frame count
    }
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
static uint32_t session_samples       = 0;   // (declared before the dbg block, which reads it)
static uint32_t session_last_flush_ms = 0;

// ---------------------------------------------------------------------------
// On-SD debug log. A companion "<session>.dbg.ndjson" written alongside each
// recording: a 1 Hz health line (loop stall ms, SD write ms, real PVT rate,
// PVT age, GPS raw bytes, recover/re-begin counts, sample count) so a
// GPS-stale session can be diagnosed offline. It sits in the SAME dir as the
// session (=> /queue/ when cloud recording), so Q,LIST uploads it too; the dash
// tags it X-File-Kind: debug and the server files it under debug/<user>/.
// Writes are cached; sync only every 5 s so debug logging can't itself add the
// SD latency we're trying to measure.
// ---------------------------------------------------------------------------
static File32   dbg_file;
static bool     dbg_open = false;
static char     dbg_path[152];
static uint32_t dbg_last_flush_ms  = 0;
static uint32_t dbg_last_health_ms = 0;
static uint32_t dbg_prev_recover   = 0;
static uint32_t dbg_prev_reinit    = 0;

static void dbgOpen(const char* sessionPath) {
    dbg_open = false; dbg_path[0] = '\0';
    if (!sessionPath || !sessionPath[0]) return;
    strncpy(dbg_path, sessionPath, sizeof(dbg_path) - 1);
    dbg_path[sizeof(dbg_path) - 1] = '\0';
    char* dot = strstr(dbg_path, ".ndjson");
    if (dot) strcpy(dot, ".dbg.ndjson");
    else strncat(dbg_path, ".dbg.ndjson", sizeof(dbg_path) - strlen(dbg_path) - 1);
    if (!dbg_file.open(dbg_path, O_WRITE | O_CREAT | O_TRUNC)) {
        Serial.printf("[dbg] open %s FAILED\n", dbg_path);
        return;
    }
    dbg_open = true;
    dbg_last_flush_ms = millis(); dbg_last_health_ms = millis();
    dbg_prev_recover = gnss_recover_count; dbg_prev_reinit = gnss_reinit_count;
    dbg_loop_max_us = dbg_sdwr_max_us = dbg_fresh_1s = 0;
    char hdr[200];
    int n = snprintf(hdr, sizeof(hdr),
        "{\"ev\":\"open\",\"unix\":%lu,\"fw\":\"%s\",\"track\":\"%s\",\"rec_cl\":%d,\"inet\":%u,\"reset\":\"%s\"}\n",
        (unsigned long)session_start_unix, FIRMWARE_VERSION, current_track,
        (int)g_cfg.rec_cl, (unsigned)g_cfg.inet, teensy_reset_reason);
    if (n > 0) { dbg_file.write((const uint8_t*)hdr, n); dbg_file.sync(); }
}

// Call every loop(); self-rate-limits to 1 Hz. Emits one health line and
// resets the per-window maxima.
static void dbgHealth() {
    if (!dbg_open) return;
    const uint32_t now = millis();
    if (now - dbg_last_health_ms < 1000) return;
    dbg_last_health_ms = now;
    const uint32_t rec_d = gnss_recover_count - dbg_prev_recover;
    const uint32_t rei_d = gnss_reinit_count  - dbg_prev_reinit;
    dbg_prev_recover = gnss_recover_count; dbg_prev_reinit = gnss_reinit_count;
    const uint32_t pvt_age = (gnss_last_fresh_ms == 0) ? 999999u : (now - gnss_last_fresh_ms);
    const int avail = GPS_SERIAL.available();   // GPS UART backlog RIGHT NOW
    const float t_die = tempmonGetTemp();       // Teensy i.MX RT1062 die temp (°C)
    const float batt  = (can_ecu.bat_x10 >= 0) ? can_ecu.bat_x10 / 10.0f : -1.0f;
    char line[360];
    int n = snprintf(line, sizeof(line),
        "{\"t\":%lu,\"ev\":\"h\",\"loop_ms\":%lu,\"sdwr_ms\":%lu,\"fresh\":%lu,\"avail\":%d,"
        "\"pvt_age\":%lu,\"flush\":%lu,\"rebegin\":%lu,\"samp\":%lu,\"status\":%u,\"lib_ok\":%u,"
        "\"t_die\":%.1f,\"t_mpu\":%.1f,\"t_esp\":%.1f,\"batt\":%.1f,\"msss\":%lu,\"mrst\":%lu}\n",
        (unsigned long)((now - session_start_ms) / 1000),
        (unsigned long)(dbg_loop_max_us / 1000), (unsigned long)(dbg_sdwr_max_us / 1000),
        (unsigned long)dbg_fresh_1s, avail, (unsigned long)pvt_age,
        (unsigned long)rec_d, (unsigned long)rei_d, (unsigned long)session_samples,
        (unsigned)gpsStatus(), (unsigned)(gnss_lib_ok ? 1 : 0),
        t_die, imu_temp_c, dash_temp_c, batt,
        (unsigned long)gps_msss, (unsigned long)gps_module_resets);
    dbg_loop_max_us = 0; dbg_sdwr_max_us = 0; dbg_fresh_1s = 0;
    if (n > 0) {
        dbg_file.write((const uint8_t*)line, n);
        if (now - dbg_last_flush_ms >= 5000) { dbg_file.sync(); dbg_last_flush_ms = now; }
    }
}

static void dbgClose() {
    if (dbg_open) {
        const char* end = "{\"ev\":\"close\"}\n";
        dbg_file.write((const uint8_t*)end, strlen(end));
        dbg_file.sync();
        dbg_file.close();
    }
    dbg_open = false;
}

// Always-on 1 Hz device-health emit (temps + battery), INDEPENDENT of recording
// or the debug-log setting, so we can catch a thermal shutdown / brownout that
// kills the UART link even when nothing is being logged. Sends the dash a
// HLTH,<t_die_x10>,<t_mpu_x10>,<t_esp_x10>,<batt_x10> line (x10 °C / V; -9999 or
// -1 = n/a) and prints a line to USB serial. t_die = Teensy die temp, t_mpu =
// MPU-6050 (enclosure ambient proxy), t_esp = dash ESP32 (from its DTEMP line),
// batt = MS3 CAN battery voltage.
static uint32_t health_last_ms = 0;
static void healthTick() {
    const uint32_t now = millis();
    if (now - health_last_ms < 1000) return;
    health_last_ms = now;
    const float   t_die     = tempmonGetTemp();
    const int16_t t_die_x10 = (int16_t)lroundf(t_die * 10.0f);
    const int16_t t_mpu_x10 = (imu_temp_c  > -100.0f) ? (int16_t)lroundf(imu_temp_c  * 10.0f) : -9999;
    const int16_t t_esp_x10 = (dash_temp_c > -100.0f) ? (int16_t)lroundf(dash_temp_c * 10.0f) : -9999;
    const int16_t batt_x10  = can_ecu.bat_x10;   // -1 if no CAN
    DASH_SERIAL.printf("HLTH,%d,%d,%d,%d\n", t_die_x10, t_mpu_x10, t_esp_x10, batt_x10);
    Serial.printf("[health] die=%.1fC mpu=%.1fC esp=%.1fC batt_x10=%d%s\n",
                  t_die, imu_temp_c, dash_temp_c, (int)batt_x10,
                  (t_die > 85.0f) ? "  <-- TEENSY HOT" : "");
}
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
    resetTeensyLap();    // fresh lap counter for this session (line-crossing)

    if (sd_card_status != SD_CARD_READY) {
        Serial.println(F("[sd] openSession: card not READY — skipping"));
        emitSessionStatus(false);
        return;
    }

    // When cloud recording is enabled, write the live session STRAIGHT INTO
    // /queue/ (not /sessions/). This is the kill-switch insurance: if we get
    // hot-pitted and the master cut kills all power mid-session, closeSession()
    // never runs to move the file — but because it's already living in /queue/,
    // the boot-time scanQueue() picks it up and the dash can upload it later.
    // We flush every ~1 s, so at worst the final <1 s of samples is lost; the
    // rest of the file is intact, valid NDJSON. SD-only sessions (no cloud) go
    // to /sessions/ as before — they're never auto-uploaded.
    const char* dir = g_cfg.rec_cl ? "/queue" : "/sessions";
    if (!sdFat.exists(dir)) {
        if (!sdFat.mkdir(dir)) {
            Serial.printf("[sd] mkdir %s failed\n", dir);
            emitSessionStatus(false);
            return;
        }
    }

    char trackSafe[32];
    sanitizeName(current_track, trackSafe, sizeof(trackSafe));

    if (session_start_unix > 0) {
        snprintf(session_path, sizeof(session_path),
                 "%s/session_%lu_%s.ndjson",
                 dir, (unsigned long)session_start_unix, trackSafe);
    } else {
        snprintf(session_path, sizeof(session_path),
                 "%s/session_nortc_%lu_%s.ndjson",
                 dir, (unsigned long)session_start_ms, trackSafe);
    }

    if (!session_file.open(session_path, O_WRITE | O_CREAT | O_TRUNC)) {
        Serial.printf("[sd] open %s FAILED\n", session_path);
        session_path[0] = '\0';
        emitSessionStatus(false);
        return;
    }
    session_file_open = true;
    Serial.printf("[sd] opened %s\n", session_path);
    if (g_cfg.debug_enabled)
        dbgOpen(session_path);   // companion .dbg.ndjson health log (dash CFG,dbg_on)
    else
        Serial.println("[dbg] debug logging OFF (CFG,dbg_on=0) — no .dbg file");
    emitSessionStatus(true);
}

// Forward decl — cloud helpers live below this block but closeSession() uses them.
static constexpr int HTTP_STATUS_CANCELLED = -1;
static int  httpPost(const char* path, const uint8_t* body, size_t body_len, File32* file_body);
static int  cloudUploadFile(const char* path, size_t body_len, File32* f);
static bool moveToQueue(const char* src_path);
static void scanQueue();
static void emitCloudStatus();
static void emitUploadStart(const char* filename, uint32_t total);
static void emitUploadProg(uint32_t done);
static void emitUploadDone(const char* status, const char* reason = nullptr);

static void closeSession() {
    if (session_file_open) {
        session_file.sync();
        session_file.close();
        session_file_open = false;
    }
    dbgClose();
    emitSessionStatus(false);

    // Decide whether to upload now, queue for later, or do nothing.
    // (Order matters: the path we use depends on whether the file made it to SD.)
    const bool have_file = (session_path[0] != '\0' && sdFat.exists(session_path));
    Serial.printf("[closeSession] path=%s have_file=%d rec_cl=%d inet=%u host=%s port=%u\n",
                  session_path, (int)have_file, (int)g_cfg.rec_cl,
                  (unsigned)g_cfg.inet,
                  g_cfg.host[0] ? g_cfg.host : "<unset>", (unsigned)g_cfg.port);
    if (have_file && g_cfg.rec_cl) {
        // The session is normally already in /queue/ (openSession writes there
        // directly when cloud recording is on — see the kill-switch note). The
        // only time it isn't is if rec_cl got toggled ON mid-session, in which
        // case the file is still in /sessions/ and we move it now. closeSession
        // never auto-uploads (v0.1.34+ protocol); the dash drives uploads via
        // Q,LIST / Q,GET / Q,DEL when its UPLOAD button is tapped.
        if (strncmp(session_path, "/queue/", 7) != 0) {
            moveToQueue(session_path);
            // Move the companion debug log alongside it so it uploads too.
            if (dbg_path[0] && sdFat.exists(dbg_path) && strncmp(dbg_path, "/queue/", 7) != 0)
                moveToQueue(dbg_path);
        }
        scanQueue();
        emitCloudStatus();
        Serial.printf("[closeSession] file queued: %s\n", session_path);
    }

    // Reset session-scoped state.
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
                               float gx, float gy, float gz, int lap) {
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

    // Lap number (only when an S/F line is known, so the server can trust it).
    if (lap >= 0) n += snprintf(buf+n, sizeof(buf)-n, "\"lap\":%d,", lap);

    if (oil_x10 < 0)  n += snprintf(buf+n, sizeof(buf)-n, "\"oil_psi\":null,");
    else              n += snprintf(buf+n, sizeof(buf)-n, "\"oil_psi\":%.1f,",  oil_x10 * 0.1f);
    if (cool_x10 < 0) n += snprintf(buf+n, sizeof(buf)-n, "\"coolant_f\":null,");
    else              n += snprintf(buf+n, sizeof(buf)-n, "\"coolant_f\":%.1f,", cool_x10 * 0.1f);

    n += snprintf(buf+n, sizeof(buf)-n,
        "\"ax\":%.2f,\"ay\":%.2f,\"az\":%.2f,\"gx\":%.1f,\"gy\":%.1f,\"gz\":%.1f}\n",
        ax, ay, az, gx, gy, gz);
    if (n < 0 || n >= (int)sizeof(buf)) return;

    const uint32_t _wr0 = micros();
    const int written = session_file.write((const uint8_t*)buf, (size_t)n);
    if (written != n) {
        Serial.printf("[sd] short write (%d/%d) — closing session\n", written, n);
        session_file.close();
        session_file_open = false;
        emitSessionStatus(false);
        return;
    }
    session_samples++;

    // Periodic flush so power-loss costs <=1 s, and 1 Hz status heartbeat to dash.
    if (millis() - session_last_flush_ms >= 1000) {
        session_last_flush_ms = millis();
        session_file.sync();
        emitSessionStatus(true);   // sends SD,REC,1,<file>,<samples>
    }
    // Track worst-case SD write+sync latency this second (debug log). This is
    // the prime suspect for loop stalls -> GPS UART overflow -> STALE.
    const uint32_t _wrd = micros() - _wr0;
    if (_wrd > dbg_sdwr_max_us) dbg_sdwr_max_us = _wrd;
}

// ---------------------------------------------------------------------------
// Cloud upload (HTTP only — HTTPS + FTP deferred; see CLAUDE.md).
//
// Settings arrive from the dash as CFG,<key>,<val> lines (see handleCfgLine).
// Cloud upload is ALWAYS After Race (live "stream to cloud" was removed):
//   1. During a session (rec_cl=1): the file is written straight into /queue/
//      (kill-switch insurance). Nothing is POSTed mid-session.
//   2. On close: if it isn't already in /queue/, move it there; scan queue.
//   3. Upload: dash-driven (Q,LIST/Q,GET/Q,DEL), or the queue walker drains
//      /queue/ oldest-first to /upload when the dash requests a drain.
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
// (g_cfg, queue_depth declared up above near recording_active so
// closeSession() can reference them.)
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
    // Live "stream to cloud" was removed (not ready). Ignore whatever value the
    // dash sends and pin AfterRace so no per-sample POST ever blocks the loop
    // mid-session (that loop stall was starving the GPS UART -> GPS STALE).
    else if (key == "cl_strm")  { /* live "stream to cloud" removed — ignored; always After Race */ }
    else if (key == "cl_email") { strncpy(g_cfg.email,   val.c_str(), sizeof(g_cfg.email)-1);   g_cfg.email[sizeof(g_cfg.email)-1]=0; }
    else if (key == "cl_key")   { strncpy(g_cfg.api_key, val.c_str(), sizeof(g_cfg.api_key)-1); g_cfg.api_key[sizeof(g_cfg.api_key)-1]=0; }
    else if (key == "rec_sd")   { g_cfg.rec_sd = (val.toInt() != 0); }
    else if (key == "rec_cl")   { g_cfg.rec_cl = (val.toInt() != 0); }
    else if (key == "dbg_on")   { g_cfg.debug_enabled = (val.toInt() != 0);
        Serial.printf("[cfg] debug logging = %s\n", g_cfg.debug_enabled ? "ON" : "OFF"); }
    else if (key == "gpsbaud") {
        const uint32_t b = (uint32_t)val.toInt();
        if (b >= 4800 && b <= 921600 && b != gps_baud_now) applyGpsBaud(b);
        else if (b == gps_baud_now) DASH_SERIAL.printf("GPSBAUD,%lu,%d\n",
                                    (unsigned long)gps_baud_now, (int)gnss_lib_ok);
    }
    else if (key == "gpshz") {
        const uint8_t hz = (uint8_t)val.toInt();
        if (hz != gps_nav_hz) applyGpsHz(hz);
    }
    else if (key == "sf") {
        // Active track's start/finish LINE: val = aLat,aLon,bLat,bLon.
        // (0,0,0,0) => no line known (dash falls back / not at a track).
        float v[4] = {0,0,0,0}; int idx = 0; int start = 0;
        for (int i = 0; i <= (int)val.length() && idx < 4; i++) {
            if (i == (int)val.length() || val[i] == ',') {
                v[idx++] = val.substring(start, i).toFloat(); start = i + 1;
            }
        }
        sf_lap.aLat=v[0]; sf_lap.aLon=v[1]; sf_lap.bLat=v[2]; sf_lap.bLon=v[3];
        sf_lap.has_line = (v[2] != 0.0f || v[3] != 0.0f);
        sf_lap.have_prev = false;
        Serial.printf("[cfg] S/F line %s\n", sf_lap.has_line ? "set" : "cleared");
    }
    else if (key == "inet") {
        g_cfg.inet = (uint8_t)val.toInt();
    } else if (key == "srctyp") {
        // Sensor source: 0=Direct (opto tach + ADC sensors), 1=MegaSquirt (CAN),
        // 2=Bluetooth (dash-side BLE OBD-II for slow readings). 2 behaves like
        // Direct on the Teensy (RPM from the opto tach; the dash overrides
        // coolant with the OBD value) unless CAN happens to be live.
        g_cfg.sensor_type = (uint8_t)val.toInt();
        Serial.printf("[cfg] sensor_type = %s\n",
                      g_cfg.sensor_type == 0 ? "Direct" :
                      g_cfg.sensor_type == 1 ? "MegaSquirt" : "Bluetooth");
    }
    else if (key == "rpmppr") {
        // Tach pulses/rev x10 (the Direct-mode RPM divider). 0 is invalid —
        // fall back to the compile-time default so we never divide by zero.
        g_cfg.rpm_ppr_x10 = (uint16_t)val.toInt();
        if (g_cfg.rpm_ppr_x10 == 0) g_cfg.rpm_ppr_x10 = 20;
        Serial.printf("[cfg] tach pulses/rev = %.1f\n", g_cfg.rpm_ppr_x10 / 10.0);
    }
    else if (key == "rpmsm") {
        // RPM display smoothing trim from the dash slider (-10..+10).
        int v = val.toInt();
        if (v < -10) v = -10;
        if (v >  10) v =  10;
        if ((int8_t)v != g_cfg.rpm_smooth) {   // print only on change (periodic re-send is quiet)
            g_cfg.rpm_smooth = (int8_t)v;
            Serial.printf("[cfg] rpm smoothing = %d\n", (int)g_cfg.rpm_smooth);
        }
    }
    else if (key == "rpmspk") {
        uint8_t v = (uint8_t)val.toInt();
        if (v > 3) v = 3;
        if (v != g_cfg.rpm_spike) {
            g_cfg.rpm_spike = v;
            Serial.printf("[cfg] rpm spike filter = %u\n", (unsigned)v);
        }
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
static void emitUploadDone(const char* status, const char* reason) {
    if (reason && reason[0]) {
        DASH_SERIAL.printf("UPLOAD,DONE,%s,%s\n", status, reason);
    } else {
        DASH_SERIAL.printf("UPLOAD,DONE,%s\n", status);
    }
}

// Single POST primitive. body may be a contiguous buffer (live stream) or a
// file streamed in fixed-size chunks (after-race / queue). For the file path
// the caller passes body=nullptr + body_len=total + file=open File32. Returns
// HTTP status code, HTTP_STATUS_CANCELLED on user cancel, or 0 on connect/
// transport failure.
static int httpPost(const char* path, const uint8_t* body, size_t body_len,
                    File32* file_body) {
    auto fail = [](const char* why) -> int {
        snprintf(last_upload_err, sizeof(last_upload_err), "%s", why);
        return 0;
    };
    if (uploads_disabled)                                   return fail("uploads disabled");
    if (!eth_hw_present)                                    return fail("no W5500 detected");
    if (Ethernet.linkStatus() != LinkON)                    return fail("Ethernet link down");
    if (g_cfg.host[0] == '\0' || g_cfg.port == 0)           return fail("cloud host/port unset");
    if (g_cfg.proto != 0) {
        return fail("Teensy supports only HTTP (use WiFi for HTTPS)");
    }

    EthernetClient c;
    c.setConnectionTimeout(800);
    if (!c.connect(g_cfg.host, g_cfg.port)) {
        Serial.printf("[cloud] connect %s:%u failed\n", g_cfg.host, g_cfg.port);
        return fail("TCP connect failed");
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

// --- WiFi-via-dash forwarder --------------------------------------------
// When g_cfg.inet == 1 (WiFi), the dash owns the WiFi link. We can't HTTP
// from the Teensy in that case. Instead we forward session files to the dash
// over UART using a tiny line-oriented protocol; the dash performs the
// actual HTTPS POST and reports back. NDJSON files are naturally line-based
// so each sample (~250 B) becomes one WUP,L,<line> message.
//
//   Teensy -> Dash:
//     WUP,START,<basename>,<size>,<session_id>,<track>
//     WUP,L,<one ndjson line, no leading/trailing whitespace>
//     ... (one per sample)
//     WUP,END,<line_count>,<total_bytes>
//     WUP,CANCEL                            (on user cancel)
//
//   Dash -> Teensy:
//     WUP,READY                             (dash buffered metadata, send data)
//     WUP,NACK,<reason>                     (dash declined: no wifi/no PSRAM/...)
//     U                                     (per-line ACK after each WUP,L)
//     WUP,RESULT,OK,<http_status>            (cloud accepted -> delete file)
//     WUP,RESULT,FAIL,<reason_or_status>     (caller decides to queue)
//
// Returns HTTP-status-like code so callers reuse the existing branching:
//   2xx           upload succeeded, file should be deleted
//   HTTP_STATUS_CANCELLED  user cancelled
//   any other     transport/protocol/upload failure -> queue file
static bool wupReadLineTimeout(char* out, size_t outsize, uint32_t timeout_ms) {
    out[0] = '\0';
    size_t n = 0;
    const uint32_t end = millis() + timeout_ms;
    while ((int32_t)(end - millis()) > 0) {
        pumpDashCommands();   // service inbound cancels/CFG during the wait
        while (DASH_SERIAL.available()) {
            const char c = (char)DASH_SERIAL.read();
            if (c == '\r') continue;
            if (c == '\n') { out[n] = '\0'; return true; }
            if (n + 1 < outsize) out[n++] = c;
        }
        delay(1);
    }
    out[n] = '\0';
    return false;
}

static int wupForwardFile(const char* path, const uint8_t* /*unused*/,
                          size_t body_len, File32* file_body) {
    auto fail = [](const char* why) -> int {
        snprintf(last_upload_err, sizeof(last_upload_err), "%s", why);
        return 0;
    };
    if (uploads_disabled)                                   return fail("uploads disabled");
    if (!file_body)                                         return fail("no file handle");
    if (g_cfg.host[0] == '\0' || g_cfg.port == 0)           return fail("cloud host/port unset");

    const char* base = strrchr(path, '/');
    base = base ? base + 1 : path;

    // Drain any stale UART bytes so the WUP,READY handshake isn't fooled
    // by leftover telemetry from before we entered this function.
    while (DASH_SERIAL.available()) DASH_SERIAL.read();

    DASH_SERIAL.printf("WUP,START,%s,%lu,%lu,%s\n",
                       base, (unsigned long)body_len,
                       (unsigned long)session_start_unix, current_track);
    DASH_SERIAL.flush();

    char reply[160];
    // Be generous with the handshake window. The dash may be mid-frame redraw
    // when WUP,START arrives, and a fresh PSRAM alloc + WiFi/state checks can
    // also add latency. 10 s is still well below any reasonable user wait.
    if (!wupReadLineTimeout(reply, sizeof(reply), 10000)) {
        Serial.println(F("[wup] no WUP,READY — dash silent"));
        return fail("no WUP,READY (dash silent)");
    }
    if (strncmp(reply, "WUP,NACK", 8) == 0) {
        Serial.printf("[wup] dash NACK: %s\n", reply);
        // reply looks like 'WUP,NACK,<reason>'; surface the reason only.
        const char* r = strchr(reply + 8, ',');
        return fail(r ? r + 1 : reply);
    }
    if (strncmp(reply, "WUP,READY", 9) != 0) {
        Serial.printf("[wup] unexpected reply: %s\n", reply);
        return fail("bad WUP handshake reply");
    }

    // Stream the file as NDJSON lines. We trust each on-disk line is already
    // newline-terminated; we strip the trailing '\n' and wrap it as
    // 'WUP,L,<line>\n' so the dash can count bytes vs the original size.
    if (!file_body->seek(0)) return fail("SD seek failed");

    upload_in_progress    = true;
    upload_cancel_pending = false;
    emitUploadProg(0);

    char     line[320];
    size_t   line_n      = 0;
    uint32_t lines       = 0;
    uint32_t bytes       = 0;
    uint32_t last_prog_ms = 0;
    char     ack_buf[24];
    while (true) {
        const int c = file_body->read();
        if (c < 0 && line_n == 0) break;   // EOF
        if (c == '\r') continue;
        if (c == '\n' || c < 0) {
            // Send one record.
            line[line_n] = '\0';
            if (line_n == 0) continue;
            DASH_SERIAL.print(F("WUP,L,"));
            DASH_SERIAL.write((const uint8_t*)line, line_n);
            DASH_SERIAL.write('\n');
            DASH_SERIAL.flush();
            // Per-line ACK so the dash can pace us if its PSRAM append stalls.
            if (!wupReadLineTimeout(ack_buf, sizeof(ack_buf), 3000)) {
                Serial.printf("[wup] ACK timeout at line %lu\n", (unsigned long)lines);
                upload_in_progress = false;
                snprintf(last_upload_err, sizeof(last_upload_err),
                         "ACK timeout at line %lu", (unsigned long)lines);
                return 0;
            }
            if (ack_buf[0] != 'U') {
                Serial.printf("[wup] unexpected ACK: %s\n", ack_buf);
                upload_in_progress = false;
                snprintf(last_upload_err, sizeof(last_upload_err),
                         "bad ACK: %.40s", ack_buf);
                return 0;
            }
            lines++;
            bytes += line_n + 1;   // include the newline that was on disk
            line_n = 0;
            if (upload_cancel_pending) {
                DASH_SERIAL.println(F("WUP,CANCEL"));
                upload_in_progress = false;
                return HTTP_STATUS_CANCELLED;
            }
            if (millis() - last_prog_ms >= 250) {
                last_prog_ms = millis();
                emitUploadProg(bytes);
            }
            if (c < 0) break;
            continue;
        }
        if (line_n + 1 < sizeof(line)) line[line_n++] = (char)c;
        else {
            // Single NDJSON line bigger than expected — truncate is safer than
            // silently splitting JSON across two messages.
            line[sizeof(line) - 1] = '\0';
        }
    }
    emitUploadProg(bytes);

    DASH_SERIAL.printf("WUP,END,%lu,%lu\n", (unsigned long)lines, (unsigned long)bytes);
    DASH_SERIAL.flush();

    // Dash now does the HTTPS POST. This can take a while for a big file on
    // marginal WiFi, so a generous timeout.
    if (!wupReadLineTimeout(reply, sizeof(reply), 120000)) {
        Serial.println(F("[wup] no WUP,RESULT — dash silent during POST"));
        upload_in_progress = false;
        return fail("no POST result from dash");
    }
    upload_in_progress = false;
    Serial.printf("[wup] result: %s\n", reply);
    if (strncmp(reply, "WUP,RESULT,OK", 13) == 0) {
        const char* p = strchr(reply + 13, ',');
        const int  http_status = p ? atoi(p + 1) : 200;
        return http_status >= 200 ? http_status : 200;
    }
    // reply is 'WUP,RESULT,FAIL,<reason>' (or older 'WUP,RESULT,FAIL'). Pull
    // the reason out so the dash UPLOAD modal can show what actually broke.
    if (strncmp(reply, "WUP,RESULT,FAIL", 15) == 0) {
        const char* r = (reply[15] == ',') ? reply + 16 : "dash POST failed";
        snprintf(last_upload_err, sizeof(last_upload_err), "%s", r);
    } else {
        snprintf(last_upload_err, sizeof(last_upload_err),
                 "bad RESULT: %.40s", reply);
    }
    return 0;
}

// Pick the right upload mechanism based on inet mode. Ethernet uses the local
// httpPost(); WiFi-via-dash routes through wupForwardFile(). emitUploadStart
// has already been called by the session-end / queue-walker code paths so the
// dash modal is already up.
static int cloudUploadFile(const char* path, size_t body_len, File32* f) {
    last_upload_err[0] = '\0';   // reset before each attempt
    if (wifiInetActive()) {
        return wupForwardFile(path, nullptr, body_len, f);
    }
    return httpPost("/upload", nullptr, body_len, f);
}

// --- Cloud status emit ---------------------------------------------------
// CLD,<live_ok>,<queue_depth>. live_ok is always 0 now that live streaming is
// removed (uploads are After Race, dash-driven); the field is kept for the
// dash's existing CLD parser.
static void emitCloudStatus() {
    DASH_SERIAL.printf("CLD,%u,%lu\n",
                       (unsigned)(live_status_last_ok ? 1 : 0),
                       (unsigned long)queue_depth);
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

// ---------------------------------------------------------------------------
// Dash-initiated upload protocol (Q,LIST / Q,GET / Q,DEL).
//
// Replaces the Teensy-initiated WUP protocol. The dash drives the whole flow
// when its UPLOAD button is tapped: it asks for the list, fetches each file's
// content line-by-line, POSTs to the cloud, and tells us when to delete. We
// just respond as a passive file server with no policy of our own.
//
// Filename safety: callers may only reference files in /queue/. We sanitize
// to a single path component (no '/' or '..') to avoid escapes.
// ---------------------------------------------------------------------------
static bool qBasenameSafe(const char* name, char* out, size_t out_sz) {
    if (!name || !*name) return false;
    for (const char* p = name; *p; ++p) {
        if (*p == '/' || *p == '\\' || *p == ' ') return false;
    }
    if (strstr(name, "..") != nullptr) return false;
    if (snprintf(out, out_sz, "/queue/%s", name) >= (int)out_sz) return false;
    return true;
}

static void handleQList() {
    if (!sdReady()) {
        DASH_SERIAL.println(F("Q,END,nosd"));
        Serial.println(F("[Q] LIST refused: SD not ready"));
        return;
    }
    if (!sdFat.exists("/queue")) {
        DASH_SERIAL.println(F("Q,END"));
        return;
    }
    File32 dir;
    if (!dir.open("/queue", O_READ)) {
        DASH_SERIAL.println(F("Q,END,opendir_failed"));
        return;
    }
    File32 entry;
    int    count = 0;
    while (entry.openNext(&dir, O_READ)) {
        if (!entry.isDir()) {
            char name[80];
            entry.getName(name, sizeof(name));
            DASH_SERIAL.printf("Q,FILE,%s,%lu\n",
                               name, (unsigned long)entry.fileSize());
            count++;
        }
        entry.close();
    }
    dir.close();
    DASH_SERIAL.println(F("Q,END"));
    Serial.printf("[Q] LIST: emitted %d file(s)\n", count);
}

// Wait for the dash to acknowledge the previous Q,L line. Per-line ACKs are
// the only thing that prevents Teensy from out-running the dash's TCP write
// path during streaming uploads: if the dash's WiFi backpressures (sender
// buffer full, retransmit, etc.) the dash will simply not send Q,A until it
// catches up, and Teensy will park here instead of blasting more data into
// a buffer that's about to overflow.
//
// Returns true if Q,A arrived within the timeout, false on timeout. Other
// inbound lines (telemetry/cancel/etc) are intentionally drained and ignored
// here so a stray telemetry line doesn't get mis-parsed in the middle of a
// streaming session.
// Wait for a sequence-numbered ack 'Q,A,<seq>' from the dash. Returns the
// acked seq (>=0) or -1 on timeout. The dash acks the HIGHEST contiguous line
// seq it has applied, so any ack >= the line we just sent confirms delivery.
// Drain any pending ACK lines (non-blocking) and return the highest cumulative
// ack seen so far. Parse state is STATIC because with a windowed sender an ack
// line can straddle two calls — the old per-call locals silently dropped the
// partial and lost acks. Interleaved telemetry lines from the dash are ignored.
static long qPumpAcksOnce(long best) {
    static char   line[24];
    static size_t n = 0;
    while (DASH_SERIAL.available()) {
        const char c = (char)DASH_SERIAL.read();
        if (c == '\r') continue;
        if (c == '\n') {
            line[n] = '\0';
            n = 0;
            if (strncmp(line, "Q,A,", 4) == 0) {
                const long v = strtol(line + 4, nullptr, 10);
                if (v > best) best = v;
            } else if (strncmp(line, "Q,A", 3) == 0) {
                best = 0x7fffffffL;   // bare ack (ancient dash): everything acked
            }
            continue;   // ignore telemetry / stray lines
        }
        if (n + 1 < sizeof(line)) line[n++] = c;
        else n = 0;
    }
    return best;
}

static void handleQGet(const char* basename) {
    if (!sdReady()) {
        DASH_SERIAL.println(F("Q,ERR,nosd"));
        return;
    }
    char path[96];
    if (!qBasenameSafe(basename, path, sizeof(path))) {
        DASH_SERIAL.println(F("Q,ERR,bad_name"));
        return;
    }
    File32 f;
    if (!f.open(path, O_READ)) {
        Serial.printf("[Q] GET %s: open failed\n", path);
        DASH_SERIAL.println(F("Q,ERR,open_failed"));
        return;
    }
    const uint32_t sz = f.fileSize();
    DASH_SERIAL.printf("Q,DATA,%s,%lu\n", basename, (unsigned long)sz);
    DASH_SERIAL.flush();
    // SLIDING-WINDOW ARQ (v0.1.102; go-back-N, cumulative ACKs). The old
    // stop-and-wait sent ONE line then blocked for its ack — every ~220-byte
    // line paid a full round trip including the dash's UI-loop latency
    // (5-30 ms while it draws), so a 20k-line session took many minutes while
    // the wire could do it in under a minute. Now we keep up to QGET_WIN lines
    // in flight; the dash already ACKs the highest CONTIGUOUS seq it applied
    // (a gap simply re-acks the last good seq), so on an ack stall we
    // retransmit everything unacked (go-back-N) and the dash dedups by seq.
    // Window sized so max in-flight (~5 KB) stays well under the dash's 32 KB
    // UART RX ring even while it blocks flushing a TCP chunk (backpressure:
    // no acks -> window fills -> we wait).
    constexpr uint32_t QGET_WIN = 16;
    static char     wtext[QGET_WIN][320];   // retransmit ring (static: ~5 KB, off the stack)
    static uint16_t wlen[QGET_WIN];
    char     line[320];
    size_t   line_n = 0;
    uint32_t seq = 0;
    long     last_acked = 0;
    uint32_t retransmits = 0;
    bool     ack_fail = false;

    auto sendSeq = [&](uint32_t s2) {
        DASH_SERIAL.printf("Q,L,%lu,", (unsigned long)s2);
        DASH_SERIAL.write((const uint8_t*)wtext[s2 % QGET_WIN], wlen[s2 % QGET_WIN]);
        DASH_SERIAL.write('\n');
    };
    // Block until the cumulative ack advances. On a 2 s stall, go-back-N
    // retransmit of everything in flight; 8 stalls in a row = link dead.
    auto waitAckProgress = [&]() -> bool {
        for (int attempt = 0; attempt < 8; ++attempt) {
            const long   before = last_acked;
            const uint32_t t0   = millis();
            while (millis() - t0 < 2000) {
                last_acked = qPumpAcksOnce(last_acked);
                if (last_acked > before) return true;
                delay(1);
            }
            retransmits += (uint32_t)(seq - (uint32_t)last_acked);
            for (uint32_t s2 = (uint32_t)last_acked + 1; s2 <= seq; ++s2) sendSeq(s2);
        }
        return false;
    };

    while (!ack_fail) {
        const int c = f.read();
        if (c < 0 && line_n == 0) break;          // EOF, no partial
        if (c == '\r') continue;
        if (c == '\n' || c < 0) {
            line[line_n] = '\0';
            if (line_n > 0) {
                // Window full? Wait for acks (with go-back-N on stall).
                while ((uint32_t)(seq - (uint32_t)last_acked) >= QGET_WIN) {
                    if (!waitAckProgress()) { ack_fail = true; break; }
                }
                if (ack_fail) break;
                seq++;
                memcpy(wtext[seq % QGET_WIN], line, line_n);
                wlen[seq % QGET_WIN] = (uint16_t)line_n;
                sendSeq(seq);
                last_acked = qPumpAcksOnce(last_acked);   // opportunistic, non-blocking
            }
            line_n = 0;
            if (c < 0) break;
            continue;
        }
        if (line_n + 1 < sizeof(line)) line[line_n++] = (char)c;
        else line[sizeof(line) - 1] = '\0';
    }
    // Drain the tail of the window.
    while (!ack_fail && last_acked < (long)seq) {
        if (!waitAckProgress()) ack_fail = true;
    }
    if (ack_fail)
        Serial.printf("[Q] GET %s: ACK stall at seq %lu (acked %ld)\n",
                      basename, (unsigned long)seq, last_acked);
    f.close();
    if (ack_fail) {
        DASH_SERIAL.println(F("Q,ERR,ack_timeout"));
        return;
    }
    DASH_SERIAL.printf("Q,EOF,%lu\n", (unsigned long)seq);
    DASH_SERIAL.flush();
    Serial.printf("[Q] GET %s: streamed %lu lines, %lu bytes (%lu retransmits)\n",
                  basename, (unsigned long)seq, (unsigned long)sz,
                  (unsigned long)retransmits);
}

static void handleQDel(const char* basename) {
    if (!sdReady()) {
        DASH_SERIAL.println(F("Q,DEL,FAIL,nosd"));
        return;
    }
    char path[96];
    if (!qBasenameSafe(basename, path, sizeof(path))) {
        DASH_SERIAL.println(F("Q,DEL,FAIL,bad_name"));
        return;
    }
    if (!sdFat.exists(path)) {
        DASH_SERIAL.println(F("Q,DEL,OK"));   // already gone is fine
        return;
    }
    if (sdFat.remove(path)) {
        DASH_SERIAL.println(F("Q,DEL,OK"));
        scanQueue();
        emitCloudStatus();
        Serial.printf("[Q] DEL %s: removed\n", path);
    } else {
        DASH_SERIAL.println(F("Q,DEL,FAIL,remove_failed"));
        Serial.printf("[Q] DEL %s: remove() failed\n", path);
    }
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
    const int status = cloudUploadFile(chosen_path, sz, &f);
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
    char reason[96];
    if (last_upload_err[0])
        snprintf(reason, sizeof(reason), "%s", last_upload_err);
    else if (status > 0)
        snprintf(reason, sizeof(reason), "http %d", status);
    else
        snprintf(reason, sizeof(reason), "no route");
    emitUploadDone("FAIL", reason);
    Serial.printf("[queue] %s POST failed (status=%d reason=%s) — leaving for next pass\n",
                  chosen_path, status, reason);
    return false;
}

static void cloudTick() {
    if (uploads_disabled) return;   // cancel-latched until reboot
    if (recording_active) {
        // No cloud work while recording — the session is written to SD (and to
        // /queue/ when cloud recording is on) and uploaded After Race. Keeping
        // the loop free of blocking POSTs here is also what keeps the GPS UART
        // serviced fast enough to stay locked during a session.
        return;
    }
    // Manual-only drain. Auto-drain has been disabled: the queue walker
    // only runs when the dash UPLOAD button sets drain_queue_now = true.
    // Ethernet path also requires a real link before we let it try.
    if (!drain_queue_now) return;
    if (!wifiInetActive() &&
        (!eth_hw_present || Ethernet.linkStatus() != LinkON)) {
        drain_queue_now = false;
        return;
    }
    if (queue_depth == 0) {
        drain_queue_now = false;
        return;
    }

    const bool ok = drainOneQueued();
    if (ok) {
        scanQueue();
        emitCloudStatus();
        // Leave drain_queue_now set: the next cloudTick will immediately
        // start the next file, and the next, until queue_depth hits 0.
    } else {
        // Failed upload — stop draining so we don't hammer a dead server.
        // User can tap UPLOAD again to retry once the cause is fixed.
        drain_queue_now = false;
    }
}



void setup() {
    captureResetReason();   // read SRC_SRSR FIRST, before anything can reset/clear it
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

    // SAME fix for the GPS UART (Serial2). Its default RX ring is only tens of
    // bytes — SMALLER than a single ~100-byte UBX-NAV-PVT frame at 25 Hz. The
    // GPS stream is parsed by POLLING getPVT() in loop(); RPM is NOT (CAN +
    // FreqMeasureMulti are interrupt/FIFO-driven), which is why during a race
    // RPM stays solid while GPS "freezes at the last position": any loop stall
    // longer than ~25 ms overflows the tiny ring and corrupts the autoPVT
    // stream. The big stalls come from the blocking cloud HTTP POST while
    // recording (up to ~800 ms connect + ~1500 ms response ≈ 2.3 s) and, to a
    // lesser degree, SD sync as the session file grows — which is exactly why
    // it survives "half a lap or two" and then stays stale: once loops run
    // chronically slow the 64-byte ring can never re-sync. 32 KB buffers ~8.5 s
    // at 38400 baud, so the parser rides through a multi-second stall and
    // recovers instead of permanently freezing. MUST be set before begin().
    // NOTE: addMemoryForRead() lives on the concrete HardwareSerialIMXRT type,
    // not the HardwareSerial& alias — call Serial2 directly (as the dash line
    // calls Serial3 directly above). GPS_SERIAL is an alias for Serial2.
    static uint8_t gpsRxBuf[32768];
    Serial2.addMemoryForRead(gpsRxBuf, sizeof(gpsRxBuf));

    setSyncProvider(getTeensyTime);

    // Wait briefly for USB serial; don't block forever if no host is attached.
    unsigned long start = millis();
    while (!Serial && millis() - start < 1500) { /* spin */ }
    Serial.printf("racecar-35 dash teensy boot, firmware v%s\n", FIRMWARE_VERSION);
    Serial.printf("[boot] Teensy last reset: %s\n", teensy_reset_reason);
    DASH_SERIAL.printf("RST,teensy,%s\n", teensy_reset_reason);   // dash shows it on STATUS
    // FlasherX check_flash_id() will search the staged OTA image for this
    // literal (FLASH_ID == "fw_teensy41" on Teensy 4.1, defined in FlashTxx.h).
    // We print it here so the linker keeps the string in our image too — the
    // staged image must contain the same literal for the check to pass.
    Serial.println(F("FLASH_ID:" FLASH_ID));
    // Tell the dash what firmware we're running so it can show it in settings
    // and compare against GitHub's manifest.json when "Check for updates" runs.
    DASH_SERIAL.printf("VER,teensy,%s\n", FIRMWARE_VERSION);

    // Tach input on pin 9 via FreqMeasureMulti (FlexPWM2_2_B input capture).
    // Active in Direct sensor mode; CAN takes over in MegaSquirt mode.
    // Both are always initialised so switching modes at runtime is seamless.
    // NOTE: do NOT switch back to the plain FreqMeasure lib — on T4.x it is
    // hard-wired to pin 22 (= CAN1 TX) and can never read pin 9.
    if (tach.begin(RPM_TACH_PIN)) {
        Serial.println(F("FreqMeasureMulti on pin 9: armed (Direct RPM source)"));
    } else {
        Serial.println(F("FreqMeasureMulti on pin 9: BEGIN FAILED (pin not capture-capable?)"));
    }

    // MS3Pro CAN bus on CAN1 (TX=pin 22, RX=pin 23 via SN65HVD230 transceiver).
    //
    // NORMAL mode (the default, NOT listen-only). This is correct for a 2-node
    // bus: the SN65HVD230 transceiver has no protocol intelligence and cannot
    // ACK — the Teensy's FlexCAN controller drives the mandatory ACK bit
    // automatically, in hardware, for every valid frame it receives. The MS3
    // needs that ACK or it retransmits forever (the "storm"). v0.1.49's
    // listen-only build SUPPRESSED that ACK, which is why the bus broke worse.
    //
    // Why this can't bus-off: bus-off only happens when a node's TRANSMIT error
    // counter hits 256, and that only accrues from frames the node ORIGINATES.
    // We never originate a transmit here (the v0.1.47 TX test is gone), and
    // auto-ACK is a receive activity (caps at error-passive, keeps receiving).
    // So: proper ACK to the MS3 + structurally immune to bus-off.
    canBegin();
    Serial.printf("CAN1 ready at %lu bps NORMAL/auto-ACK (MS3Pro base ID 0x%03lX)\n",
                  (unsigned long)CAN_BAUD, (unsigned long)CAN_BASE_ID);

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
    // Boot-time auto-calibration (gyro bias + accel scale), gated by a stillness
    // check; falls back to EEPROM offsets if the car is moving at boot.
    if (imu_present) calibrateIMU();

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

    // Connect at the KNOWN-GOOD default baud (38400), then 9600 factory rate.
    // We do NOT auto-raise the baud here: a failed silent switch used to leave
    // the Teensy at a baud the module isn't on -> GPS permanently dead -> the
    // stale watchdog re-begins every 10 s, blocking the loop and stuttering RPM.
    // Higher baud is now an EXPLICIT, recoverable choice from the GPS settings
    // page (CFG,gpsbaud -> applyGpsBaud, which verifies + scans back on failure).
    // ⚠️ BAUD LANDMINE FIX (review): saveConfiguration() (0.1.93) persists a
    // dash-selected baud (e.g. 230400) to the MODULE's flash — so on the next
    // power-up the module boots at that baud, and a boot scan of only 38400 +
    // 9600 would NEVER connect => GPS permanently dead every boot. The boot
    // scan must include every rate the GPS settings page offers.
    if (tryConnectGNSS(GPS_BAUD_PRIMARY) || tryConnectGNSS(GPS_BAUD_FALLBACK)
        || tryConnectGNSS(230400, GPS_BEGIN_WAIT_FAST)
        || tryConnectGNSS(115200, GPS_BEGIN_WAIT_FAST)
        || tryConnectGNSS(460800, GPS_BEGIN_WAIT_FAST)) {
        Serial.printf("u-blox connected @ %lu baud\n", (unsigned long)gps_baud_now);
        myGNSS.setUART1Output(COM_TYPE_UBX);     // we don't need NMEA on this link
        myGNSS.setNavigationFrequency(gps_nav_hz);
        myGNSS.setAutoPVT(true);
        // Module-reset forensics: auto NAV-STATUS (every 5th nav solution = 5 Hz
        // at 25 Hz nav rate — ~120 B/s, minimal UART cost) with msss callback.
        myGNSS.setAutoNAVSTATUScallbackPtr(&navStatusCB);
        myGNSS.setAutoNAVSTATUSrate(5);
        // Persist this config (UBX out + nav rate + auto-PVT + baud) to the
        // module's flash + BBR. THE STALE FIX: the debug logs show the module
        // going silent for ~10s repeatedly (a reset, most likely a brownout) and
        // coming back in its DEFAULT NMEA mode, so the Teensy sees no UBX PVT
        // until the stale watchdog re-asserts UBX. With the config saved, a reset
        // reboots STRAIGHT into UBX auto-PVT and resumes almost immediately — no
        // ~10s silent gap. (Doesn't fix the underlying reset, but kills the stale.)
        if (myGNSS.saveConfiguration())
            Serial.println(F("[gps] config saved to flash/BBR (reset-resilient)"));
        else
            Serial.println(F("[gps] saveConfiguration FAILED"));
        gnss_lib_ok = true;
    } else {
        Serial.println(F("u-blox NOT detected on Serial2 (tried 38400/9600/230400/115200/460800; will keep retrying every 30 s)"));
        // Leave Serial2 open at 9600 so we can still observe raw bytes flowing
        // from the module (most u-blox modules default-output NMEA at 9600).
        // The dash will report status=RAW or OFF depending on what we see.
        GPS_SERIAL.begin(GPS_BAUD_FALLBACK);
        gnss_lib_ok = false;
    }
}

// Deterministic-but-plausible synthetic data generator. Used when test_mode_
// active is true so we can exercise the full SD-write + cloud-upload pipeline
// without real sensors. Track is a smooth circle near a known coordinate so
// any map view of the uploaded session shows recognizable motion.
// ---------------------------------------------------------------------------
// Test mode synthetic track — NJMP Thunderbolt (Millville, NJ).
//
// We follow a hand-tuned polyline that roughly matches Thunderbolt's 2.25 mi
// / 14-turn layout. Each waypoint has a target speed and signed lateral G;
// position, speed, and ay are linearly interpolated between adjacent points
// so the dash shows smooth-ish acceleration into a corner and back out, not a
// staircase.
//
// Geometry is approximate. The user knows the real track and can refine the
// numbers (just adjust lat/lon/mph/lat_g in NJMP_THUNDERBOLT[] and the lap
// will animate the new shape on the next session). Heading is computed from
// the segment direction so the dash compass tracks reality.
//
//   lat_g convention: positive = right-hand turn (driver feels pulled left).
//                     negative = left-hand turn  (driver feels pulled right).
//   ax convention:    positive = accelerating, negative = braking.
// ---------------------------------------------------------------------------
struct TrackPoint {
    float lat;
    float lon;
    float mph;
    float lat_g;
};

static const TrackPoint NJMP_THUNDERBOLT[] = {
    { 39.36500f, -75.08400f, 130.0f,  0.00f },   // S/F line (south end of front straight)
    { 39.36700f, -75.08400f, 140.0f,  0.00f },   // mid front straight
    { 39.36930f, -75.08400f, 150.0f,  0.00f },   // end of front straight
    { 39.37000f, -75.08350f,  55.0f, -1.40f },   // T1: hard brake into right-hander
    { 39.37020f, -75.08230f,  72.0f, -0.80f },   // T1 exit
    { 39.36970f, -75.08120f,  85.0f, -0.50f },   // T2 right
    { 39.36900f, -75.08050f,  78.0f,  0.70f },   // T3 esses (left)
    { 39.36850f, -75.07980f,  82.0f, -0.80f },   // T4 right
    { 39.36810f, -75.07900f,  92.0f, -0.45f },   // T5 fast right
    { 39.36800f, -75.07800f, 108.0f, -0.20f },   // T5 exit / fast mid-section
    { 39.36770f, -75.07720f,  95.0f, -0.55f },   // T6 right
    { 39.36720f, -75.07690f,  68.0f,  1.10f },   // T7 tight left
    { 39.36650f, -75.07720f,  78.0f,  0.95f },   // T8 left
    { 39.36580f, -75.07800f,  88.0f,  0.65f },   // T9 carousel (long left)
    { 39.36510f, -75.07900f, 108.0f,  0.35f },   // T10 exit onto back straight
    { 39.36400f, -75.08050f, 118.0f,  0.00f },   // back straight mid
    { 39.36380f, -75.08200f,  68.0f,  1.20f },   // T13 chicane left
    { 39.36420f, -75.08320f,  82.0f, -1.00f },   // T14 right onto S/F
};
static constexpr int NJMP_N = sizeof(NJMP_THUNDERBOLT) / sizeof(NJMP_THUNDERBOLT[0]);

static float trackWrap360(float deg) {
    while (deg <  0.0f)   deg += 360.0f;
    while (deg >= 360.0f) deg -= 360.0f;
    return deg;
}

static float trackBearingDeg(float lat1, float lon1, float lat2, float lon2) {
    // Flat-earth bearing. NJMP's footprint is ~3.6 km — within flat-earth
    // accuracy limits and way cheaper than full great-circle math on Teensy.
    const float lat_rad = lat1 * 0.0174533f;
    const float dlat_m  = (lat2 - lat1) * 111319.0f;
    const float dlon_m  = (lon2 - lon1) * cosf(lat_rad) * 111319.0f;
    if (dlat_m == 0.0f && dlon_m == 0.0f) return 0.0f;
    return trackWrap360(atan2f(dlon_m, dlat_m) * 57.29578f);
}

static float trackDistanceM(float lat1, float lon1, float lat2, float lon2) {
    const float lat_rad = lat1 * 0.0174533f;
    const float dlat_m  = (lat2 - lat1) * 111319.0f;
    const float dlon_m  = (lon2 - lon1) * cosf(lat_rad) * 111319.0f;
    return sqrtf(dlat_m * dlat_m + dlon_m * dlon_m);
}

static void generateTestSample(uint8_t& fix, uint8_t& sats,
                               float& lat_deg, float& lon_deg,
                               float& mph, float& hdg_deg,
                               uint8_t& status,
                               uint16_t& rpm,
                               int16_t& oil_psi_x10, int16_t& cool_f_x10,
                               float& ax, float& ay, float& az,
                               float& gx, float& gy, float& gz) {
    // Persistent state across calls. Reset on every new test_mode session
    // (detected by test_mode_start_ms changing).
    static int      seg            = 0;
    static float    prog           = 0.0f;
    static uint32_t last_call_ms   = 0;
    static uint32_t last_start_ms  = 0;
    static float    prev_speed_mph = 0.0f;

    const uint32_t now_ms = millis();
    if (test_mode_start_ms != last_start_ms) {
        seg            = 0;
        prog           = 0.0f;
        last_call_ms   = now_ms;
        last_start_ms  = test_mode_start_ms;
        prev_speed_mph = NJMP_THUNDERBOLT[0].mph;
    }
    float dt_s = (now_ms - last_call_ms) * 0.001f;
    if (dt_s > 0.5f) dt_s = 0.5f;   // clamp on first call / after big stalls
    last_call_ms = now_ms;

    const TrackPoint& p0 = NJMP_THUNDERBOLT[seg];
    const TrackPoint& p1 = NJMP_THUNDERBOLT[(seg + 1) % NJMP_N];

    // Interpolate position, speed, lateral G along the current segment.
    lat_deg = p0.lat   + (p1.lat   - p0.lat)   * prog;
    lon_deg = p0.lon   + (p1.lon   - p0.lon)   * prog;
    mph     = p0.mph   + (p1.mph   - p0.mph)   * prog;
    ay      = p0.lat_g + (p1.lat_g - p0.lat_g) * prog;

    // Heading: bearing of the current segment.
    hdg_deg = trackBearingDeg(p0.lat, p0.lon, p1.lat, p1.lon);

    // GPS state for downstream parsers.
    fix    = 3;
    sats   = 12;
    status = 2;   // OK

    // Engine. RPM scales with speed in a mid-range gear; cap at 8 k.
    int rpm_calc = 1500 + (int)(mph * 35.0f);
    if (rpm_calc > 8000) rpm_calc = 8000;
    rpm = (uint16_t)rpm_calc;
    // Healthy oil and coolant numbers, slowly drifting.
    oil_psi_x10 = (int16_t)(450 + 30 * sinf(now_ms * 0.0001f));
    cool_f_x10  = (int16_t)(1900 + 15 * sinf(now_ms * 0.00003f));

    // IMU. ax derived from speed delta this tick. Clamped to plausible values
    // so a 100 mph -> 60 mph braking zone doesn't read 6 g.
    if (dt_s > 0.001f) {
        const float dv_mps = (mph - prev_speed_mph) * 0.44704f;
        ax = (dv_mps / dt_s) / 9.81f;
        if (ax >  1.5f) ax =  1.5f;
        if (ax < -2.0f) ax = -2.0f;
    } else {
        ax = 0.0f;
    }
    prev_speed_mph = mph;
    az = 1.0f;

    // Gyro: yaw rate from cornering (consistent with ay + speed), pitch/roll
    // small and proportional to longitudinal/lateral accel so the IMU traces
    // look organic on the dash.
    const float v_ms = mph * 0.44704f;
    gz = (v_ms > 1.0f) ? (ay * 9.81f / v_ms) * 57.29578f : 0.0f;   // deg/s
    gx = ay * 4.0f;
    gy = ax * 4.0f;

    // Advance progress along the polyline based on distance covered this tick.
    const float seg_len_m = trackDistanceM(p0.lat, p0.lon, p1.lat, p1.lon);
    if (seg_len_m > 0.1f) {
        const float dist_m = v_ms * dt_s;
        prog += dist_m / seg_len_m;
    } else {
        prog = 1.0f;
    }
    while (prog >= 1.0f) {
        prog -= 1.0f;
        seg = (seg + 1) % NJMP_N;
    }
}

static void emitToDash() {
    // Only read the SparkFun lib's PVT cache after we've confirmed at least
    // ONE fresh PVT arrived (gnss_last_fresh_ms != 0). Calling getFixType()
    // etc. before that returns uninitialized heap memory — at boot we saw
    // fix=128 sats=121 speed=2 million as a result.
    const bool have_pvt = gnss_lib_ok && gnss_last_fresh_ms != 0;
    uint8_t fix     = have_pvt ? myGNSS.getFixType()       : 0;
    uint8_t sats    = have_pvt ? myGNSS.getSIV()           : 0;
    float   lat_deg = have_pvt ? myGNSS.getLatitude()    * 1e-7f      : 0.0f;
    float   lon_deg = have_pvt ? myGNSS.getLongitude()   * 1e-7f      : 0.0f;
    float   mph     = have_pvt ? myGNSS.getGroundSpeed() * 0.00223694f : 0.0f;
    float   hdg_deg = have_pvt ? myGNSS.getHeading()     * 1e-5f      : 0.0f;
    uint8_t status  = gpsStatus();

    // Engine data — source selected by g_cfg.sensor_type, with an AUTO override:
    //   0 = Direct:      RPM from opto tach (FreqMeasureMulti pin 9),
    //                    coolant from NTC thermistor (A3)
    //   1 = MegaSquirt:  RPM + coolant from MS3Pro CAN broadcast
    // Oil PSI is always from the direct ADC (A2) — MS3Pro has no oil input.
    //
    // AUTO-PREFER-CAN: if live MS3 CAN frames are arriving (0x5E8 fresh within
    // CAN_STALE_MS), use the CAN RPM/coolant for the ENG line EVEN in Direct
    // mode. This makes RPM/coolant "just work" the moment the transceiver sees
    // the bus, without the user having to flip Settings -> Sensor Type. If CAN
    // goes silent we fall straight back to the direct opto-tach / NTC sources.
    // We always drain both sources every cycle so the fallback is seamless.
    const bool can_live = (can_ecu.last_ms != 0)
                          && (millis() - can_ecu.last_ms <= CAN_STALE_MS);
    const bool use_can  = (g_cfg.sensor_type == 1) || can_live;
    const uint16_t directRpm  = computeRpmAndReset();   // always drain the tach FIFO
    const int16_t  directCool = readCoolantFx10();       // always keep EMA warm
    uint16_t       rpm         = use_can ? can_ecu.rpm       : directRpm;
    int16_t        oil_psi_x10 = readOilPsiX10();
    int16_t        cool_f_x10  = use_can ? can_ecu.clt_f_x10 : directCool;

    // IMU — flush averaged samples and emit even when absent (zeroes keep the
    // dash parser's field count stable and make it easy to detect a missing IMU).
    flushImu();
    float ax = imu.ax, ay = imu.ay, az = imu.az;
    float gx = imu.gx, gy = imu.gy, gz = imu.gz;

    // Test mode override — substitute synthetic data BEFORE any emit so the
    // dash UI, the SD write, and the cloud-upload pipeline all see the same
    // synthetic sample.
    if (test_mode_active) {
        generateTestSample(fix, sats, lat_deg, lon_deg, mph, hdg_deg, status,
                           rpm, oil_psi_x10, cool_f_x10, ax, ay, az, gx, gy, gz);
    }

    DASH_SERIAL.printf("GPS,%u,%u,%.6f,%.6f,%.1f,%.1f,%u\n",
                       fix, sats, lat_deg, lon_deg, mph, hdg_deg, status);
    Serial.printf("GPS,%u,%u,%.6f,%.6f,%.1f,%.1f,%u  (raw_bytes=%lu)\n",
                  fix, sats, lat_deg, lon_deg, mph, hdg_deg, status,
                  (unsigned long)gnss_raw_bytes);

    // ENG line: RPM + oil PSI + coolant — all sourced per sensor_type above.
    // The dash RPM bar always reads eng.rpm from this line.
    DASH_SERIAL.printf("ENG,%u,%d,%d\n", rpm, oil_psi_x10, cool_f_x10);
    Serial.printf("ENG,%u,%d,%d  [src=%s]\n", rpm, oil_psi_x10, cool_f_x10,
                  use_can ? (g_cfg.sensor_type == 1 ? "CAN" : "CAN(auto)") : "direct");
    // ECU line: full MS3Pro CAN dataset. Dash uses these when sensor_type==1
    // (MegaSquirt) for coolant temp, AFR, MAP, TPS, IAT, and battery.
    DASH_SERIAL.printf("ECU,%u,%d,%d,%d,%d,%d,%d\n",
                       rpm, can_ecu.clt_f_x10, can_ecu.map_x10,
                       can_ecu.tps_x10, can_ecu.afr_x10,
                       can_ecu.iat_f_x10, can_ecu.bat_x10);
    Serial.printf("ECU,%u,%d,%d,%d,%d,%d,%d\n",
                  rpm, can_ecu.clt_f_x10, can_ecu.map_x10,
                  can_ecu.tps_x10, can_ecu.afr_x10,
                  can_ecu.iat_f_x10, can_ecu.bat_x10);
    DASH_SERIAL.printf("IMU,%.2f,%.2f,%.2f,%.1f,%.1f,%.1f\n",
                       ax, ay, az, gx, gy, gz);
    Serial.printf("IMU,%.2f,%.2f,%.2f,%.1f,%.1f,%.1f\n",
                  ax, ay, az, gx, gy, gz);

    // Lap counter: precise start/finish LINE crossing on the GPS stream.
    updateTeensyLap(fix, lat_deg, lon_deg);

    // SD logging: append one NDJSON sample with all the fields we just emitted.
    if (recording_active && session_file_open) {
        writeSessionSample(fix, sats, lat_deg, lon_deg, mph, hdg_deg,
                           rpm, oil_psi_x10, cool_f_x10,
                           ax, ay, az, gx, gy, gz,
                           sf_lap.has_line ? sf_lap.lap : -1);
    }

    // Time of day from RTC — piggybacks on the 1 Hz GPS heartbeat so the dash
    // gets a fresh TIME line every emit without a separate periodic block in
    // loop() (which previously was causing UART stalls via Ethernet.maintain()).
    DASH_SERIAL.printf("TIME,%lu\n", (unsigned long)now());
}

void loop() {
    // Debug: worst loop() PERIOD in the current 1 s window. A long period == the
    // loop stalled (SD/other), the prime suspect for GPS UART overflow -> STALE.
    {
        static uint32_t last_loop_us = 0;
        const uint32_t nu = micros();
        if (last_loop_us) { const uint32_t d = nu - last_loop_us; if (d > dbg_loop_max_us) dbg_loop_max_us = d; }
        last_loop_us = nu;
    }

    // Heartbeat LED so we can see at a glance the Teensy is alive.
    static unsigned long lastBlink = 0;
    if (millis() - lastBlink >= 500) {
        lastBlink = millis();
        digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
    }

    // Drain commands from the dash (REC, TRACK, SDFORMAT, …) and developer
    // commands from native USB (VER?, USBFWUPDATE).
    pumpDashCommands();
    pumpUsbCommands();

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

    // Drain both RPM sources every loop; emitToDash() picks the active one.
    pumpTach();
    pumpCAN();
    canDiagReport();   // 1 Hz CAN health line to USB serial + dash (self-throttled)
    healthTick();      // 1 Hz temps + battery (heat/brownout diagnostics), always on

    // Periodic SD card recheck. SDIO occasionally misses a card on boot —
    // particularly if the card was inserted while the Teensy was already
    // powered. Poll every 3 s while we don't have a healthy mount, and
    // re-emit SD,* so the dash UI updates. Don't poke a working card.
    static uint32_t last_sd_poll_ms = 0;
    if ((sd_card_status == SD_CARD_NONE || sd_card_status == SD_CARD_ERROR) &&
        !recording_active &&
        millis() - last_sd_poll_ms >= 3000) {
        last_sd_poll_ms = millis();
        const SdCardStatus before = sd_card_status;
        detectSD();
        if (sd_card_status != before) {
            Serial.printf("[sd] poll: status %d -> %d\n",
                          (int)before, (int)sd_card_status);
            emitSdStatus();
            // Queue may have been there all along but unmountable; rescan now.
            if (sd_card_status == SD_CARD_READY) {
                scanQueue();
                emitCloudStatus();
            }
        }
    }

    // Periodic SD + cloud status heartbeat to the dash. Earlier code only
    // emitted SD,* on CHANGE, so if the dash missed an early SD,READY (UART
    // buffer race during boot, OR card came up after the initial emit) the
    // dash would stay stuck on 'no card' on its STATUS page even though the
    // Teensy was happily writing files to /queue/. Re-emitting every 5 s
    // costs ~30 bytes of UART traffic and guarantees the dash converges to
    // ground truth within seconds regardless of any lost transient.
    static uint32_t last_status_emit_ms = 0;
    if (millis() - last_status_emit_ms >= 5000) {
        last_status_emit_ms = millis();
        emitSdStatus();
        emitCloudStatus();
    }

    // Cloud upload tick — queue drainer (no-op while recording / nothing queued).
    cloudTick();

    // IMU accumulate — rate-limited internally to ~250 Hz.
    readIMU();

    // GPS health tracking — exactly one path is active at a time.
    bool freshThisCall = false;
    if (gnss_lib_ok) {
        if (myGNSS.getPVT(0)) {       // non-blocking: returns true ONLY on fresh PVT
            gnss_last_fresh_ms = millis();
            freshThisCall = true;
            dbg_fresh_1s++;           // real PVT rate for the debug health line
        }
        myGNSS.checkCallbacks();      // dispatch NAV-STATUS msss callback (reset forensics)
    } else {
        // Drain raw bytes the lib never claimed. Doesn't try to interpret —
        // we just want to know SOMETHING is on the GPS UART.
        while (GPS_SERIAL.available()) {
            (void)GPS_SERIAL.read();
            gnss_raw_bytes++;
            gnss_last_fresh_ms = millis();
        }
    }

    // Auto-recover if the PVT stream ever goes stale (esp. mid-recording).
    gpsStaleWatchdog();

    // On-SD debug health line (1 Hz, self-rate-limited; no-op when not recording).
    dbgHealth();

    // Emit cadence: on every fresh GPS PVT (== the nav rate, up to 25 Hz), BUT
    // never slower than a 25 Hz FLOOR regardless of GPS. RPM/ENG/IMU come from
    // the tach/CAN/IMU and are INDEPENDENT of GPS — the old 1 Hz fallback made
    // the dash RPM bar update only once/sec whenever GPS went stale (the
    // "RPM haywire" symptom). A steady 40 ms floor keeps RPM smooth even when
    // GPS is silent; when GPS is stale the (frozen) position just repeats,
    // which the dash already renders as STALE. ~25 Hz over the 921600 link is
    // ~6 KB/s = trivial.
    static unsigned long lastEmit = 0;
    const unsigned long emit_floor_ms = 40UL;   // 25 Hz floor, GPS-independent
    if (freshThisCall || millis() - lastEmit >= emit_floor_ms) {
        lastEmit = millis();
        emitToDash();
    }

    // 1 Hz GPS diagnostics for the dash GPS settings page:
    // GPSDIAG,<baud>,<lib_ok>,<pvt_age_ms>,<light_recover>,<heavy_rebegin>
    static uint32_t last_gpsdiag_ms = 0;
    if (millis() - last_gpsdiag_ms >= 1000) {
        last_gpsdiag_ms = millis();
        const uint32_t age = (gnss_last_fresh_ms == 0) ? 999999u
                             : (millis() - gnss_last_fresh_ms);
        DASH_SERIAL.printf("GPSDIAG,%lu,%u,%lu,%lu,%lu,%u\n",
                           (unsigned long)gps_baud_now, (unsigned)(gnss_lib_ok ? 1 : 0),
                           (unsigned long)age, (unsigned long)gnss_recover_count,
                           (unsigned long)gnss_reinit_count, (unsigned)gps_nav_hz);
    }
}
