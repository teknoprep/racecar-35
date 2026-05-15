// racecar-35 dash firmware — GPS + tach + IMU to dash bridge
//
// Reads u-blox GNSS over Serial2 (pin 7=RX2, pin 8=TX2), an opto-
// isolated tach pulse on pin 9 (FlexPWM input capture / FreqMeasure),
// and an MPU-6050 IMU over Wire (pin 18=SDA, pin 19=SCL),
// then forwards all three to the CrowPanel ESP32 dash over Serial3 (pin
// 14=TX3, pin 15=RX3 -> CrowPanel UART0).
//
// Wire format on Serial3, 115200 8N1, '\n'-terminated, three line types:
//
//   GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>,<gps_status>
//   ENG,<rpm>,<oil_psi_x10>,<coolant_f_x10>
//   IMU,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
//
// Example: GPS,3,12,40.123456,-74.123456,67.5,123.4,2
//          ENG,3450,650,2185
//          IMU,0.02,-0.98,0.12,1.3,-0.5,0.2
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
  constexpr uint32_t DASH_BAUD         = 115200;
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
// Recording is currently a state flag with debug logging; SD logging and
// cloud upload land when the W5500 module arrives.
// ---------------------------------------------------------------------------
static bool     recording_active = false;
static char     current_track[32] = "UNKNOWN";
static uint32_t session_start_ms = 0;
// Active timezone id sent by the dash (e.g. "ET", "PT", "UTC"). Used today
// only for logging; future SD-filename / cloud-metadata code can consult it.
// The Teensy's RTC and the wire-format TIME line are always UTC.
static char     current_tz[8]    = "UTC";

static void formatSDCard();   // defined below, after SD section

// TimeLib sync provider — reads the Teensy 4.1 built-in RTC.
static time_t getTeensyTime() { return Teensy3Clock.get(); }

static void handleDashCommand(const String& line) {
    if (line.startsWith("REC,")) {
        const int v = line.substring(4).toInt();
        const bool now = (v != 0);
        if (now != recording_active) {
            recording_active = now;
            if (now) {
                session_start_ms = millis();
                Serial.printf("[teensy] REC START — track=\"%s\", t=%lums\n",
                              current_track, (unsigned long)session_start_ms);
            } else {
                const uint32_t dur = millis() - session_start_ms;
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
    setSyncProvider(getTeensyTime);

    // Wait briefly for USB serial; don't block forever if no host is attached.
    unsigned long start = millis();
    while (!Serial && millis() - start < 1500) { /* spin */ }
    Serial.println(F("racecar-35 dash: boot"));

    // Tach input on pin 9 (FreqMeasure uses FlexPWM input capture on T4.x).
    // Must be called after Serial.begin to avoid weird interactions.
    FreqMeasure.begin();
    Serial.println(F("FreqMeasure on pin 9: armed"));

    // ADC for oil pressure (A2) and coolant temp (A3). 12-bit + 16-sample
    // hardware averaging gives a stable, noise-floor-free reading at 25 Hz.
    analogReadResolution(12);
    analogReadAveraging(16);
    Serial.println(F("Analog: oil PSI on A2, coolant degF on A3 (12-bit, x16 avg)"));

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
