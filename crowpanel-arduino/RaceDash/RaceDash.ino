// racecar-35 dash — CrowPanel ESP32-S3 7" V3.0
// Built via arduino-cli with esp32:esp32@2.0.14, OPI PSRAM, QIO 4MB.
//
// UART0 (Serial) listens for two line types from the Teensy 4.1, 115200 8N1:
//   GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>,<gps_status>
//   ENG,<rpm>[,<oil_psi_x10>,<coolant_f_x10>]   (3-field form, back-compat to 1)
//
// Two pages, swipeable:
//   PAGE_DASH      — RPM bar + huge speed + L (HDG/LAT/LON) / R (FIX/SATS/GPS)
//   PAGE_SETTINGS  — adjustable thresholds for the RPM bar, alert thresholds,
//                    blink rates and colors. Persisted to NVS via Preferences.
// Swipe right→left enters settings; left→right returns to dash.

#include <Wire.h>
#include <PCA9557.h>
#include <WiFi.h>          // ESP32-S3 built-in WiFi (NTP, OTA HTTPS, future cloud upload)
#include <WiFiClientSecure.h>   // HTTPS to GitHub for manifest + firmware download
#include <HTTPClient.h>         // wraps WiFiClientSecure with a simple GET/POST API
#include <Update.h>             // partition-swap OTA writer
#include <esp_log.h>       // for esp_log_level_set("wifi", ESP_LOG_NONE)

// Compile-time firmware version. Bump via the release process when shipping
// a new build (eventually automated by scripts/release.sh + GitHub Action).
// Settings page displays it; "Check for updates" compares to manifest.json
// from https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/.
#define FIRMWARE_VERSION "0.1.13"

#include <Preferences.h>
#include <time.h>
#include <driver/i2c.h>           // I2C_NUM_1 for Touch_GT911 config
#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <lgfx/v1/platforms/esp32s3/Panel_RGB.hpp>
#include <lgfx/v1/platforms/esp32s3/Bus_RGB.hpp>

// ---------------------------------------------------------------------------
// Forward type decls — Arduino IDE auto-injects function prototypes at the
// top of the translation unit, so any type used in a function signature must
// be visible BEFORE the first function. Real definitions follow below.
// ---------------------------------------------------------------------------
enum SettingId : uint8_t {
    // Internet block at the very top — picks whether all internet-bound
    // operations route via Teensy/W5500 (Ethernet) or CrowPanel/ESP32-S3 WiFi.
    // SSID/pass/status rows are hidden when Mode=Ethernet.
    ST_INET_MODE = 0,
    ST_WIFI_SSID, ST_WIFI_PASS, ST_WIFI_STATUS,
    ST_RPM_MIN, ST_RPM_MAX, ST_ALERTS,
    ST_A1_RPM, ST_A1_COL, ST_A1_HZ,
    ST_AM_RPM, ST_AM_COL, ST_AM_HZ,
    // Coolant temp warn-color + oil PSI warn-color. Each has a master
    // show toggle, a threshold (°F or PSI), and the warning palette colour.
    ST_SHOW_TEMP, ST_TEMP_WARN_F, ST_TEMP_WARN_COL,
    ST_SHOW_PSI,  ST_PSI_WARN_PSI, ST_PSI_WARN_COL,
    // Sensor data source (Direct / MegaSquirt) + AFR display (MS3 mode only).
    // AFR has both a "too rich" (low) and "too lean" (high) warn threshold;
    // either fires the same colour.
    ST_SENSOR_TYPE,
    ST_SHOW_AFR, ST_AFR_WARN_LO, ST_AFR_WARN_HI, ST_AFR_WARN_COL,
    ST_REC_SD, ST_REC_CLOUD,
    ST_CL_HOST, ST_CL_PORT, ST_CL_PROTO, ST_CL_STREAM,
    ST_CL_AUTH_USER, ST_CL_AUTH_PASS,
    ST_AUTO_TRACK,
    ST_TIMEZONE,    // ENUM: cycle through TIMEZONES[]
    ST_SET_TIME,    // action: open time-set page
    ST_COUNT,
    // Tool-page actions — NOT in the scrollable settings list. They live on
    // PAGE_TOOLS (swipe right from STATUS). Their SettingId values are still
    // useful as keyboard / tap dispatch keys (e.g. ST_OTA_CHECK is what
    // handleSettingsTap looked for; now it's tools/handleToolsTap).
    ST_SD_FORMAT,
    ST_OTA_CHECK,
};
struct NumBounds { uint16_t lo, hi, step; };

// WiFi state machine values (full definition lives next to its tick function
// further down). Forward-declared here because the Arduino IDE / arduino-cli
// auto-injects function prototypes at the top of the TU — anything used in
// a function signature must be visible before the first function.
enum WifiState : uint8_t { WS_OFF = 0, WS_CONNECTING, WS_CONNECTED, WS_FAILED };

// Single key descriptor for both numeric and text keyboards.
struct KbKey {
    int16_t  x, y, w, h;
    const char* label;
    // action: visible char (0..0x7F) is inserted as that char into editBuf.
    // Special codes: 0x01='\b' backspace, 0x02='C' clear, 0x03='D' done,
    //                0x04='X' cancel, 0x05=' ' space.
    char action;
};

#define TFT_BL 2

PCA9557 Out;
Preferences prefs;

// ---------------------------------------------------------------------------
// LovyanGFX driver — V3.0 panel timings + GT911 capacitive touch on I2C 0x14.
// ---------------------------------------------------------------------------
class LGFX : public lgfx::LGFX_Device {
public:
    lgfx::Bus_RGB     _bus_instance;
    lgfx::Panel_RGB   _panel_instance;
    lgfx::Light_PWM   _light_instance;
    lgfx::Touch_GT911 _touch_instance;

    LGFX(void) {
        {
            auto cfg = _panel_instance.config();
            cfg.memory_width  = 800; cfg.memory_height = 480;
            cfg.panel_width   = 800; cfg.panel_height  = 480;
            cfg.offset_x = 0;        cfg.offset_y = 0;
            _panel_instance.config(cfg);
        }
        {
            auto cfg = _bus_instance.config();
            cfg.panel = &_panel_instance;
            cfg.pin_d0  = GPIO_NUM_15; cfg.pin_d1  = GPIO_NUM_7;  cfg.pin_d2  = GPIO_NUM_6;
            cfg.pin_d3  = GPIO_NUM_5;  cfg.pin_d4  = GPIO_NUM_4;
            cfg.pin_d5  = GPIO_NUM_9;  cfg.pin_d6  = GPIO_NUM_46; cfg.pin_d7  = GPIO_NUM_3;
            cfg.pin_d8  = GPIO_NUM_8;  cfg.pin_d9  = GPIO_NUM_16; cfg.pin_d10 = GPIO_NUM_1;
            cfg.pin_d11 = GPIO_NUM_14; cfg.pin_d12 = GPIO_NUM_21; cfg.pin_d13 = GPIO_NUM_47;
            cfg.pin_d14 = GPIO_NUM_48; cfg.pin_d15 = GPIO_NUM_45;
            cfg.pin_henable = GPIO_NUM_41;
            cfg.pin_vsync   = GPIO_NUM_40;
            cfg.pin_hsync   = GPIO_NUM_39;
            cfg.pin_pclk    = GPIO_NUM_0;
            cfg.freq_write  = 15000000;
            cfg.hsync_polarity    = 0;
            cfg.hsync_front_porch = 40;
            cfg.hsync_pulse_width = 48;
            cfg.hsync_back_porch  = 40;
            cfg.vsync_polarity    = 0;
            cfg.vsync_front_porch = 1;
            cfg.vsync_pulse_width = 31;
            cfg.vsync_back_porch  = 13;
            cfg.pclk_active_neg = 1;
            cfg.de_idle_high    = 0;
            cfg.pclk_idle_high  = 0;
            _bus_instance.config(cfg);
            _panel_instance.setBus(&_bus_instance);
        }
        {
            auto cfg = _light_instance.config();
            cfg.pin_bl = GPIO_NUM_2;
            _light_instance.config(cfg);
            _panel_instance.light(&_light_instance);
        }
        {
            auto cfg = _touch_instance.config();
            cfg.x_min      = 0;   cfg.x_max = 799;
            cfg.y_min      = 0;   cfg.y_max = 479;
            cfg.pin_int    = -1;
            cfg.pin_rst    = -1;
            cfg.bus_shared = true;
            cfg.offset_rotation = 0;
            cfg.i2c_port   = I2C_NUM_1;
            cfg.pin_sda    = GPIO_NUM_19;
            cfg.pin_scl    = GPIO_NUM_20;
            cfg.freq       = 400000;
            cfg.i2c_addr   = 0x14;
            _touch_instance.config(cfg);
            _panel_instance.setTouch(&_touch_instance);
        }
        setPanel(&_panel_instance);
    }
};

LGFX tft;

// ---------------------------------------------------------------------------
// User-pickable color palette for alert colors.
// ---------------------------------------------------------------------------
constexpr int N_PALETTE = 8;
const uint16_t PALETTE[N_PALETTE] = {
    TFT_RED, TFT_ORANGE, TFT_YELLOW, TFT_GREEN,
    TFT_CYAN, TFT_BLUE, TFT_MAGENTA, TFT_WHITE
};
const char* const PALETTE_NAMES[N_PALETTE] = {
    "RED", "ORANGE", "YELLOW", "GREEN",
    "CYAN", "BLUE", "MAGENTA", "WHITE"
};

// ---------------------------------------------------------------------------
// State.
// ---------------------------------------------------------------------------
struct GpsState {
    uint8_t  fix      = 0;
    uint8_t  sats     = 0;
    float    lat_deg  = 0.0f;
    float    lon_deg  = 0.0f;
    float    mph      = 0.0f;
    float    hdg_deg  = 0.0f;
    uint8_t  status   = 0;        // 0=OFF 1=RAW 2=OK 3=STALE
    uint32_t last_ms  = 0;
};
static GpsState g;

struct EngState {
    uint16_t rpm           = 0;
    // x10 fixed-point: 0..30000 PSI*10, -1 on sensor fault. Stored as int16.
    int16_t  oil_psi_x10   = -1;
    int16_t  coolant_f_x10 = -1;
    uint32_t last_ms       = 0;
};
static EngState eng;

// MegaSquirt-via-CAN data, forwarded by the Teensy when the CAN transceiver is
// wired up (planned — see Teensy main.cpp). All fields are x10 fixed-point
// except RPM. -1 indicates "not received" / fault. The dash listens for ECU
// lines only when settings.sensor_type == 1 (MegaSquirt).
struct EcuState {
    uint16_t rpm           = 0;
    int16_t  coolant_f_x10 = -1;
    int16_t  map_x10       = -1;     // manifold absolute pressure, kPa × 10
    int16_t  tps_x10       = -1;     // throttle position, % × 10
    int16_t  afr_x10       = -1;     // air/fuel ratio × 10 (e.g. 145 = 14.5)
    int16_t  iat_f_x10     = -1;     // intake air temp, °F × 10
    int16_t  bat_x10       = -1;     // battery voltage, V × 10
    uint32_t last_ms       = 0;
};
static EcuState ecu;

struct ImuState {
    float    ax = 0, ay = 0, az = 0;   // g  (±2g range)
    float    gx = 0, gy = 0, gz = 0;   // deg/s (±250 range)
    uint32_t last_ms = 0;
};
static ImuState imu;

static char     active_ip[24]    = "NOT CONNECTED";  // updated by ETH, line from Teensy
static uint32_t rec_start_ms     = 0;                // millis() when recording last started

// SD card state — updated from SD,<status>[,<total_mb>[,<free_mb>]] lines.
// 0=NONE  1=NEEDS_FORMAT  2=READY  3=ERROR  4=FORMATTING
static uint8_t  sd_card_status   = 0;
static uint32_t sd_total_mb      = 0;
static uint32_t sd_free_mb       = 0;

// Active session state — updated from SD,REC,<0|1>,<file>,<samples> lines.
// The dash uses this to show a REC indicator + a live sample count.
static bool     sd_session_active  = false;
static char     sd_session_file[80] = "";
static uint32_t sd_session_samples = 0;

// ---------------------------------------------------------------------------
// OTA state — driven by tapping "Check for updates" in settings.
// Source of truth lives in firmware/manifest.json on this repo's main branch:
//   https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/manifest.json
//
// State machine drives PAGE_OTA modal. otaTick() in loop() advances states
// that perform network work (checking + downloading) without blocking the
// touch handler. Cancel is implemented by setting ota_cancel_requested;
// the tick checks it between chunks and bails.
// ---------------------------------------------------------------------------
enum OtaState : uint8_t {
    OTA_S_IDLE = 0,
    OTA_S_CHECKING,             // GET manifest.json
    OTA_S_UPTODATE,             // version match — nothing to do
    OTA_S_AVAILABLE,            // update found; modal waits for user Confirm/Cancel
    OTA_S_TEENSY_DOWNLOADING,   // download teensy.hex + stream over UART to Teensy
    OTA_S_TEENSY_WAITING,       // hex flushed; wait/poll until Teensy reports target version
    OTA_S_DOWNLOADING,          // streaming CrowPanel firmware bytes into Update.write()
    OTA_S_APPLYING,             // Update.end() in progress (finalising flash partition)
    OTA_S_REBOOT,               // success; about to ESP.restart()
    OTA_S_FAILED,               // any error; modal shows reason + Close
};
static OtaState ota_state              = OTA_S_IDLE;
static char     ota_latest_version[16] = "";
static char     ota_url[200]           = "";
static char     ota_err_msg[80]        = "";
static uint32_t ota_total_bytes        = 0;
static uint32_t ota_done_bytes         = 0;
static uint8_t  ota_return_page        = 0;     // page to restore on Close
static bool     ota_modal_dirty        = false;
static bool     ota_cancel_requested   = false;
// Teensy-side update state (parallel manifest entry)
static char     ota_teensy_version[16] = "";
static char     ota_teensy_url[200]    = "";
static uint32_t ota_teensy_size        = 0;
static bool     ota_need_teensy        = false;
static bool     ota_need_crowpanel     = false;

// Upload modal state — driven by UPLOAD,START/PROG/DONE lines from Teensy.
// While upload_active, the dash forces PAGE_UPLOAD on top of whatever was
// showing, and ignores all input except the CANCEL button. CANCEL latches
// upload_locally_cancelled = true so the dash can show a small reminder that
// uploads are disabled until the system reboots.
static bool     upload_active            = false;
static char     upload_file[64]          = "";
static uint32_t upload_total             = 0;
static uint32_t upload_done              = 0;
static uint8_t  upload_return_page       = 0;        // PAGE_DASH; uint8_t because
                                                     // enum Page isn't visible up here
static bool     upload_locally_cancelled = false;   // cleared only on reboot
static bool     upload_modal_dirty       = false;
static uint32_t upload_last_draw_ms      = 0;
static char     upload_result_msg[32]    = "";       // brief banner after DONE

// Cloud state — updated from CLD,<live_ok>,<queue_depth> lines.
static bool     cloud_live_ok      = false;
static uint32_t cloud_queue_depth  = 0;

// Firmware version tracking. Dash version is compile-time (FIRMWARE_VERSION).
// Teensy version arrives via VER,teensy,<ver> on Teensy boot, and on demand
// when the dash sends "VER?\n" (dash boot + status page entry).
static char     teensy_fw_version[16] = "?";

// Two-tap arming for the SD format action in settings.
static bool     sd_format_armed  = false;
static uint32_t sd_format_arm_ms = 0;

// Settings page dirty flag — declared here (before the UART parsers) so
// parseSdLine() can set it on status change. The settings page checks it
// each draw cycle to decide whether to repaint.
static bool settingsDirty = true;

// RTC epoch received from the Teensy's TIME, line (0 = not yet received).
static uint32_t rtc_epoch = 0;

// Time-set page editing state — populated from rtc_epoch on entry.
static int  ts_year  = 2025, ts_month = 1, ts_day  = 1;
static int  ts_hour  = 0,    ts_min   = 0, ts_sec  = 0;
static bool ts_dirty = false;   // set by +/- taps; triggers value redraw

// ---------------------------------------------------------------------------
// Lap timer — GPS-proximity start/finish detection + distance integration.
// ---------------------------------------------------------------------------
struct LapTimer {
    bool     active          = false;
    int      track_idx       = -1;    // which TRACKS[] entry we're timing at
    uint32_t lap_start_ms    = 0;
    float    dist_miles      = 0.0f;  // odometer this lap (mph * dt)
    uint32_t prev_gps_ms     = 0;
    bool     left_start      = false; // true once car has moved away from S/F line
    bool     timing_started  = false; // true after first clean S/F crossing (guards partial first lap)

    uint32_t last_lap_ms     = 0;     // most recent completed lap time (0 = none)
    float    last_lap_dist   = 0.0f;  // distance of last completed lap (miles)
};
static LapTimer lapTimer;

constexpr float    LAP_RADIUS_KM = 0.075f;   // 75 m start/finish detection radius
constexpr uint32_t MIN_LAP_MS    = 15000;    // minimum lap time before a crossing counts

// Recording state — toggled by the START/STOP button on the dash. Sent to
// the Teensy as `REC,<0|1>\n` on UART0 TX. The Teensy doesn't read this
// yet (one-way bytes harmlessly accumulate in its Serial3 RX buffer);
// when the SD-card / cloud-streaming feature is implemented Teensy-side
// it just starts consuming the line.
static bool recording         = false;
static int  last_track_idx    = -1;        // TRACKS[] index of the last confirmed track (-1 = none saved)
static char active_track_name[52] = "";    // full display name (may include config, e.g. "Mid-Ohio Full Course")

// Button geometry on the dash page — both buttons sit left of the speed.
// START/STOP is smaller and higher than before; TRACK button lives below it.
namespace {
  constexpr int RECBTN_X = 30;
  constexpr int RECBTN_Y = 155;
  constexpr int RECBTN_W = 160;
  constexpr int RECBTN_H = 70;
  constexpr int TRKBTN_X = 30;    // always-visible shortcut to the track picker
  constexpr int TRKBTN_Y = 235;   // RECBTN_Y + RECBTN_H + 10
  constexpr int TRKBTN_W = 160;
  constexpr int TRKBTN_H = 60;
  // Speed sits on the right side of the screen (out of the way of the
  // START/STOP button on the left). Drop the decimal at >=100 mph so a
  // 3-digit number stays narrow enough to fit the 400-px bg pad cleanly
  // without clipping at the right edge.
  constexpr int SPEED_CX = 600;          // speed text middle-centre x
  constexpr int SPEED_PAD_W = 400;       // bg-fill pad width (spans 400..800)
}

struct Settings {
    // RPM bar + alert flash (existing)
    uint16_t rpm_min          = 1000;
    uint16_t rpm_max          = 8000;
    bool     alerts_enabled   = true;
    uint16_t alert1_rpm       = 6000;
    uint8_t  alert1_color_idx = 2;     // YELLOW
    uint8_t  alert1_hz        = 2;
    uint16_t alertmax_rpm     = 7500;
    uint8_t  alertmax_color_idx = 0;   // RED
    uint8_t  alertmax_hz      = 8;

    // Recording / streaming
    bool     record_sd        = false;
    bool     record_cloud     = false;
    char     cloud_host[64]   = "racecar.api.blueuc.com";  // default endpoint
    uint16_t cloud_port       = 80;
    uint8_t  cloud_protocol   = 0;     // 0=HTTP, 1=HTTPS, 2=FTP (only HTTP wired today)
    uint8_t  cloud_stream     = 0;     // 0=Live, 1=AfterRace
    // cloud_user repurposed as a free-text USER EMAIL tag for data ownership.
    // Not an auth credential — the Teensy forwards it as X-User-Email header.
    // Real auth (Google OAuth) lives in the cloud-side Docker image.
    char     cloud_auth_user[64] = "";   // user email (X-User-Email)
    char     cloud_auth_pass[96] = "";   // API key (X-API-Key); masked on display

    // Internet routing. 0=Ethernet (Teensy/W5500), 1=WiFi (CrowPanel ESP32-S3).
    // Phase 1: controls NTP path + (future) firmware update path. Cloud upload
    // routing still flows through Teensy/Ethernet regardless until Phase 3.
    uint8_t  internet_mode    = 0;       // default Ethernet
    char     wifi_ssid[33]    = "";      // 802.11 max 32 + NUL
    char     wifi_pass[64]    = "";      // WPA2 PSK max 63 + NUL

    // Engine sensor display + warn thresholds (drawn bottom-left on the dash).
    // show_*  : false hides that line entirely from the dash.
    // *_warn  : threshold past which the value flips to the warn colour.
    //           Coolant warns ABOVE the threshold (overheat), oil warns BELOW
    //           it (low pressure — engine-killing if you don't see it).
    bool     show_coolant       = true;
    uint16_t coolant_warn_f     = 220;     // °F — typical 220°F overheat threshold
    uint8_t  coolant_warn_col   = 0;       // RED
    bool     show_oil_psi       = true;
    uint16_t oil_warn_psi       = 20;      // PSI — typical 20 PSI low-oil floor
    uint8_t  oil_warn_col       = 0;       // RED

    // Sensor data source. Selects which UART line type the dash trusts for
    // engine telemetry that exists in both pipelines (coolant temp today; RPM
    // and others as MS3 CAN comes online):
    //   0 = Direct     — Teensy ADCs on A2/A3 (the wiring we built in §5b/5c)
    //   1 = MegaSquirt — MS3 CAN broadcast, forwarded by Teensy as ECU,...
    // Oil PSI stays direct regardless (MS3 typically doesn't have an oil
    // pressure input wired). AFR is only available in MegaSquirt mode.
    uint8_t  sensor_type        = 0;       // default Direct

    // AFR (Air/Fuel Ratio) — only meaningful in MegaSquirt sensor mode.
    // Two-sided warn band: too rich (< afr_warn_lo) and too lean (> afr_warn_hi)
    // both flip the value to the warn colour. Values are AFR × 10 (so 145 = 14.5).
    bool     show_afr           = true;
    uint16_t afr_warn_lo_x10    = 115;     // 11.5 AFR — below this is dangerously rich
    uint16_t afr_warn_hi_x10    = 160;     // 16.0 AFR — above this is dangerously lean
    uint8_t  afr_warn_col       = 0;       // RED

    // Track selection
    bool     auto_select_track = true; // when on, skip picker if a clear closest match exists

    // Time zone — index into TIMEZONES[] (defined below). Display only;
    // the Teensy's RTC + the wire-format TIME line are always UTC.
    uint8_t  timezone_idx     = 0;     // default UTC
};
static Settings s;

// Names for ENUM-style settings (cycle-on-tap).
const char* const PROTOCOL_NAMES[] = { "HTTP", "HTTPS", "FTP" };
constexpr int N_PROTOCOL = 3;
const char* const STREAM_NAMES[]   = { "Live Stream", "After Race" };
constexpr int N_STREAM   = 2;
const char* const SENSOR_TYPE_NAMES[] = { "Direct", "MegaSquirt" };
constexpr int N_SENSOR_TYPE = 2;
const char* const INET_MODE_NAMES[]   = { "Ethernet", "WiFi" };
constexpr int N_INET_MODE   = 2;

// ---------------------------------------------------------------------------
// Time zones with DST rules.
//
// The Teensy emits UTC always (TIME,<unix_epoch>). All DST/offset math is
// done here in the dash for display purposes. We send TZ,<id> to the Teensy
// at boot and on change so the trunk-side has the info available for future
// SD-filename / cloud-metadata use, but the Teensy doesn't currently apply
// the offset itself.
//
// DST rule fields:
//   stdOffsetHr   standard-time offset from UTC in hours (-5 = EST, +0 = GMT)
//   observesDst   true if DST applies (false for AZ, HI, UTC, etc.)
//   dstStart{Month,Week,Dow,Hour}   "Nth weekday of month, at HH local std time"
//   dstEnd{Month,Week,Dow,Hour}     same shape; week 1-4 = nth, 5 = last
// ---------------------------------------------------------------------------
struct TimeZone {
    const char* id;            // short id sent to Teensy ("UTC", "ET", "PT", ...)
    const char* name;          // long name shown in the settings ENUM cycle
    const char* abbrevStd;     // abbreviation when in standard time ("EST")
    const char* abbrevDst;     // abbreviation when in DST ("EDT"); == abbrevStd if no DST
    int8_t      stdOffsetHr;
    bool        observesDst;
    uint8_t     dstStartMonth;  // 1-12
    uint8_t     dstStartWeek;   // 1-4 (nth) or 5 (last)
    uint8_t     dstStartDow;    // 0=Sun .. 6=Sat
    uint8_t     dstStartHour;   // local std hour at which DST begins
    uint8_t     dstEndMonth;
    uint8_t     dstEndWeek;
    uint8_t     dstEndDow;
    uint8_t     dstEndHour;
};

// Common US zones + a few internationals. Add more by extending this array.
// US DST: 2nd Sunday of March → 1st Sunday of November (since 2007).
// EU DST: last Sunday of March → last Sunday of October.
static const TimeZone TIMEZONES[] = {
    // id      name                  std    dst    off  dst   sM sW sD sH eM eW eD eH
    { "UTC",   "UTC",                "UTC", "UTC",  0,  false, 0, 0, 0, 0, 0, 0, 0, 0 },
    { "ET",    "Eastern (US)",       "EST", "EDT", -5, true,   3, 2, 0, 2, 11, 1, 0, 2 },
    { "CT",    "Central (US)",       "CST", "CDT", -6, true,   3, 2, 0, 2, 11, 1, 0, 2 },
    { "MT",    "Mountain (US)",      "MST", "MDT", -7, true,   3, 2, 0, 2, 11, 1, 0, 2 },
    { "MT-AZ", "Arizona (no DST)",   "MST", "MST", -7, false,  0, 0, 0, 0, 0, 0, 0, 0 },
    { "PT",    "Pacific (US)",       "PST", "PDT", -8, true,   3, 2, 0, 2, 11, 1, 0, 2 },
    { "AKT",   "Alaska",             "AKST","AKDT",-9, true,   3, 2, 0, 2, 11, 1, 0, 2 },
    { "HT",    "Hawaii (no DST)",    "HST", "HST",-10, false,  0, 0, 0, 0, 0, 0, 0, 0 },
    { "GMT",   "UK (London)",        "GMT", "BST",  0, true,   3, 5, 0, 1, 10, 5, 0, 1 },
    { "CET",   "Europe (Paris)",     "CET", "CEST", 1, true,   3, 5, 0, 2, 10, 5, 0, 2 },
};
constexpr int N_TIMEZONES = sizeof(TIMEZONES) / sizeof(TIMEZONES[0]);

// Days in month (Gregorian). Leap years = divisible by 4 except centuries
// not divisible by 400.
static int daysInMonth(int y, int m) {
    static const int dom[] = {0,31,28,31,30,31,30,31,31,30,31,30,31};
    const bool leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
    return (m == 2 && leap) ? 29 : dom[m];
}

// Day-of-week for a Gregorian date. Zeller's congruence, normalised to
// 0=Sunday, 1=Monday ... 6=Saturday.
static int dayOfWeek(int y, int m, int d) {
    if (m < 3) { y--; m += 12; }
    const int K = y % 100;
    const int J = y / 100;
    const int h = (d + (13 * (m + 1)) / 5 + K + K / 4 + J / 4 + 5 * J) % 7;
    // Zeller h: 0=Sat, 1=Sun, 2=Mon, ..., 6=Fri → shift to 0=Sun
    return (h + 6) % 7;
}

// Day-of-month for the Nth occurrence of a given weekday in a given month.
// nth: 1..4 = literal, 5 = "last".
static int nthWeekdayOfMonth(int year, int month, int targetDow, int nth) {
    const int firstDow         = dayOfWeek(year, month, 1);
    const int firstOccurrence  = ((targetDow - firstDow + 7) % 7) + 1;
    if (nth >= 5) {
        // Last occurrence: walk forward by 7 until next would overshoot the month.
        const int dim = daysInMonth(year, month);
        int d = firstOccurrence;
        while (d + 7 <= dim) d += 7;
        return d;
    }
    return firstOccurrence + 7 * (nth - 1);
}

// Is DST currently in effect for the given UTC epoch under the given zone?
// We compare against local *standard* time so the rule's "2 AM local" maps
// cleanly to a single instant per year (we don't try to resolve the
// 2:00–3:00 AM "spring-forward" gap or 1:00–2:00 AM "fall-back" overlap;
// those edge cases are sub-second and not worth the complexity here).
static bool isDstActive(time_t utc, const TimeZone& tz) {
    if (!tz.observesDst) return false;

    const time_t   localStd = utc + (time_t)tz.stdOffsetHr * 3600;
    struct tm*     tmv = gmtime(&localStd);
    const int year  = tmv->tm_year + 1900;
    const int month = tmv->tm_mon + 1;
    const int day   = tmv->tm_mday;
    const int hour  = tmv->tm_hour;

    const int dstStartDay = nthWeekdayOfMonth(year, tz.dstStartMonth, tz.dstStartDow, tz.dstStartWeek);
    const int dstEndDay   = nthWeekdayOfMonth(year, tz.dstEndMonth,   tz.dstEndDow,   tz.dstEndWeek);

    // Outside the months containing the transitions: easy.
    if (month > tz.dstStartMonth && month < tz.dstEndMonth) return true;
    if (month < tz.dstStartMonth || month > tz.dstEndMonth) return false;

    // Same month as start (or end): compare day, then hour.
    if (month == tz.dstStartMonth) {
        if (day != dstStartDay) return day > dstStartDay;
        return hour >= tz.dstStartHour;
    }
    // month == dstEndMonth
    if (day != dstEndDay) return day < dstEndDay;
    return hour < tz.dstEndHour;
}

// UTC offset in seconds (negative for west of UTC), accounting for DST.
static int32_t tzOffsetSeconds(time_t utc, const TimeZone& tz) {
    int32_t off = (int32_t)tz.stdOffsetHr * 3600;
    if (isDstActive(utc, tz)) off += 3600;
    return off;
}

// UTC → local epoch (seconds). Result can be passed to gmtime() to format.
static time_t utcToLocal(time_t utc, const TimeZone& tz) {
    return utc + tzOffsetSeconds(utc, tz);
}

// Local → UTC. Used when the user hand-enters a time via the time-set page.
// Computes the offset by approximating DST status from a near-UTC estimate;
// only ambiguous within the spring/fall transition hour, which we accept.
static time_t localToUtc(time_t local, const TimeZone& tz) {
    int32_t off = (int32_t)tz.stdOffsetHr * 3600;
    const time_t utcApprox = local - off;
    if (isDstActive(utcApprox, tz)) off += 3600;
    return local - off;
}

// Returns the abbreviation appropriate for the given UTC moment ("EST"/"EDT").
static const char* tzAbbrevFor(time_t utc, const TimeZone& tz) {
    return isDstActive(utc, tz) ? tz.abbrevDst : tz.abbrevStd;
}

// Build a UTC unix epoch from broken-down UTC fields. (Used by time-set SAVE.)
static uint32_t makeEpochUtc(int y, int mo, int d, int h, int mi, int s) {
    static const int dom[] = {0,31,28,31,30,31,30,31,31,30,31,30,31};
    uint32_t days = 0;
    for (int yr = 1970; yr < y; yr++) {
        days += 365 + (((yr % 4 == 0 && yr % 100 != 0) || yr % 400 == 0) ? 1 : 0);
    }
    for (int m = 1; m < mo; m++) {
        days += dom[m];
        if (m == 2 && ((y % 4 == 0 && y % 100 != 0) || y % 400 == 0)) days++;
    }
    days += d - 1;
    return (uint32_t)days * 86400UL + (uint32_t)h * 3600UL + (uint32_t)mi * 60UL + (uint32_t)s;
}

// ---------------------------------------------------------------------------
// Track database — pre-seeded list of common US road-course tracks. Stored
// alphabetically (the picker re-orders on the fly when GPS is available).
// Add tracks by extending this array OR (TODO) editing via the settings page.
// ---------------------------------------------------------------------------
struct TrackConfig { const char* name; };

struct TrackInfo {
    const char* name;
    float       lat;                    // track centre (used for "are you at this track?")
    float       lon;
    float       radius_km;
    float       sf_lat;                 // start/finish line (used for lap detection)
    float       sf_lon;                 // NOTE: all S/F coords are approximate — verify on-site
    const TrackConfig* configs;         // nullptr = single layout, no config sub-picker
    uint8_t            n_configs;       // 0 = single layout
};
// Config arrays — tracks with multiple layouts that share the same S/F line.
// Layouts with genuinely different S/F locations get separate TRACKS[] entries.
static const TrackConfig MID_OHIO_CFGS[] = { {"Full + Chicane"}, {"Full Course"}, {"Short Course"} };
static const TrackConfig SONOMA_CFGS[]   = { {"Full Course"}, {"Short Course"} };
static const TrackConfig WGL_CFGS[]      = { {"Grand Prix"}, {"Short Course"} };
static const TrackConfig VIR_CFGS[]      = { {"Full Course"}, {"Grand Course"}, {"North Course"} };

static const TrackInfo TRACKS[] = {
    // name                     centre lat/lon           radius  S/F lat/lon               configs       n
    { "Barber",        33.5328f,  -86.6181f, 2.5f,  33.5327f,  -86.6155f, nullptr,         0 },
    { "CMP Full",      34.4884f,  -80.5941f, 2.0f,  34.4870f,  -80.5955f, nullptr,         0 },
    { "CMP East",      34.4870f,  -80.5880f, 1.2f,  34.4840f,  -80.5880f, nullptr,         0 },
    { "CMP West",      34.4884f,  -80.6000f, 1.0f,  34.4900f,  -80.6000f, nullptr,         0 },
    { "COTA",          30.1328f,  -97.6411f, 3.0f,  30.1341f,  -97.6413f, nullptr,         0 },
    { "Daytona",       29.1853f,  -81.0697f, 3.0f,  29.1868f,  -81.0700f, nullptr,         0 },
    { "Laguna Seca",   36.5847f, -121.7494f, 2.5f,  36.5843f, -121.7530f, nullptr,         0 },
    { "Lime Rock",     41.9263f,  -73.3856f, 2.0f,  41.9271f,  -73.3821f, nullptr,         0 },
    { "Mid-Ohio",      40.6889f,  -82.6356f, 2.5f,  40.6895f,  -82.6375f, MID_OHIO_CFGS,   3 },
    { "Nelson Ledges", 41.3892f,  -81.0852f, 1.5f,  41.3899f,  -81.0858f, nullptr,         0 },
    { "NHMS",          43.3628f,  -71.4630f, 2.0f,  43.3600f,  -71.4640f, nullptr,         0 },
    { "NJMP Thunderbolt", 39.4053f, -75.0789f, 2.0f, 39.4079f, -75.0793f, nullptr,         0 },
    { "NJMP Lightning",   39.3998f, -75.0735f, 1.5f, 39.4008f, -75.0728f, nullptr,         0 },
    { "Pocono",        41.0561f,  -75.5128f, 3.5f,  41.0550f,  -75.5115f, nullptr,         0 },
    { "Road America",  43.7986f,  -87.9956f, 3.0f,  43.7963f,  -87.9944f, nullptr,         0 },
    { "Road Atlanta",  34.1469f,  -83.8189f, 2.5f,  34.1518f,  -83.8197f, nullptr,         0 },
    { "Sebring",       27.4570f,  -81.3568f, 3.5f,  27.4502f,  -81.3537f, nullptr,         0 },
    { "Sonoma",        38.1614f, -122.4544f, 2.5f,  38.1615f, -122.4547f, SONOMA_CFGS,     2 },
    { "Summit Pt",           39.2415f, -77.9779f, 1.5f, 39.2415f, -77.9779f, nullptr,      0 },
    { "Summit Pt Jefferson", 39.2370f, -77.9700f, 1.2f, 39.2370f, -77.9700f, nullptr,      0 },
    { "Summit Pt Shenandoah",39.2450f, -77.9650f, 1.5f, 39.2450f, -77.9650f, nullptr,      0 },
    { "VIR",           36.5611f,  -79.2103f, 2.5f,  36.5689f,  -79.2067f, VIR_CFGS,        3 },
    { "VIR South",     36.5620f,  -79.2100f, 1.2f,  36.5620f,  -79.2100f, nullptr,         0 },
    { "VIR Patriot",   36.5660f,  -79.2120f, 1.0f,  36.5660f,  -79.2120f, nullptr,         0 },
    { "Watkins Glen",  42.3417f,  -76.9272f, 2.5f,  42.3369f,  -76.9272f, WGL_CFGS,        2 },
};
constexpr int N_TRACKS = sizeof(TRACKS) / sizeof(TRACKS[0]);

// Great-circle distance, kilometres. Standard haversine.
static float trackDistanceKm(float lat1, float lon1, float lat2, float lon2) {
    const float DEG2RAD = (float)M_PI / 180.0f;
    const float dLat = (lat2 - lat1) * DEG2RAD;
    const float dLon = (lon2 - lon1) * DEG2RAD;
    const float a = sinf(dLat * 0.5f) * sinf(dLat * 0.5f)
                  + cosf(lat1 * DEG2RAD) * cosf(lat2 * DEG2RAD)
                  * sinf(dLon * 0.5f) * sinf(dLon * 0.5f);
    return 6371.0f * 2.0f * atan2f(sqrtf(a), sqrtf(1.0f - a));
}

// Returns index of closest track within its radius, OR -1 if no GPS / no
// track in range. Used both for "auto select" and to highlight in the picker.
static int closestTrackIdx() {
    if (g.fix < 2) return -1;            // no usable fix
    int   best   = -1;
    float bestKm = 1e9f;
    for (int i = 0; i < N_TRACKS; ++i) {
        const float km = trackDistanceKm(g.lat_deg, g.lon_deg,
                                         TRACKS[i].lat, TRACKS[i].lon);
        if (km <= TRACKS[i].radius_km && km < bestKm) {
            bestKm = km; best = i;
        }
    }
    return best;
}

// ---------------------------------------------------------------------------
// Track picker state. Defined here (not next to its drawing code) so
// handleTouch() can read tp.scrollY / tp.dirty for drag-scroll without
// type-ordering dance.
// ---------------------------------------------------------------------------
struct TrackPickerState {
    uint8_t order[N_TRACKS + 1];   // indices into TRACKS[], +1 for the synthetic UNKNOWN slot
    uint8_t count;                 // total entries displayed
    int8_t  selected;              // index within `order`, -1 = nothing selected
    int     scrollY;
    bool    dirty;
    bool    for_recording = false; // true = confirm starts REC,1; false = track-select only
};
static TrackPickerState tp;

// Config picker — shown after track selection when the track has multiple
// layouts sharing the same S/F line. Tap-only (no scroll; max 3 configs).
struct ConfigPickerState {
    int   track_idx    = -1;
    int8_t selected    =  0;
    bool   dirty       = true;
    bool   from_auto   = false;  // true = opened via GPS auto-select on START; CANCEL → dash
    bool   for_recording = false; // true = confirm starts REC,1; false = track-select only
};
static ConfigPickerState cp;

constexpr uint8_t TP_UNKNOWN_IDX = 0xFE;   // sentinel meaning "synthetic UNKNOWN entry"
constexpr int TP_HDR_H        = 60;
constexpr int TP_FTR_H        = 70;
constexpr int TP_BODY_TOP     = TP_HDR_H;
constexpr int TP_BODY_BOTTOM  = 480 - TP_FTR_H;
constexpr int TP_BODY_HEIGHT  = TP_BODY_BOTTOM - TP_BODY_TOP;
constexpr int TP_ROW_DY       = 40;
constexpr int TP_ROW_HEIGHT   = 36;

static void clampPickerScroll();              // forward — body lives further down

static void loadSettings() {
    prefs.begin("dash", true);
    s.rpm_min            = prefs.getUShort("rpm_min",  s.rpm_min);
    s.rpm_max            = prefs.getUShort("rpm_max",  s.rpm_max);
    s.alerts_enabled     = prefs.getBool  ("alerts",   s.alerts_enabled);
    s.alert1_rpm         = prefs.getUShort("a1_rpm",   s.alert1_rpm);
    s.alert1_color_idx   = prefs.getUChar ("a1_col",   s.alert1_color_idx);
    s.alert1_hz          = prefs.getUChar ("a1_hz",    s.alert1_hz);
    s.alertmax_rpm       = prefs.getUShort("am_rpm",   s.alertmax_rpm);
    s.alertmax_color_idx = prefs.getUChar ("am_col",   s.alertmax_color_idx);
    s.alertmax_hz        = prefs.getUChar ("am_hz",    s.alertmax_hz);
    s.record_sd          = prefs.getBool  ("rec_sd",   s.record_sd);
    s.record_cloud       = prefs.getBool  ("rec_cl",   s.record_cloud);
    prefs.getString      ("cl_host",  s.cloud_host, sizeof(s.cloud_host));
    s.cloud_port         = prefs.getUShort("cl_port",  s.cloud_port);
    s.cloud_protocol     = prefs.getUChar ("cl_proto", s.cloud_protocol);
    s.cloud_stream       = prefs.getUChar ("cl_strm",  s.cloud_stream);
    // Renamed in NVS: cl_user -> cl_email, cl_pass -> cl_key. Old key reads
    // are kept as fallback for one release so existing dashes don't lose
    // their settings on upgrade. Drop after a deploy or two.
    if (prefs.isKey("cl_email")) prefs.getString("cl_email", s.cloud_auth_user, sizeof(s.cloud_auth_user));
    else                          prefs.getString("cl_user",  s.cloud_auth_user, sizeof(s.cloud_auth_user));
    if (prefs.isKey("cl_key"))    prefs.getString("cl_key",   s.cloud_auth_pass, sizeof(s.cloud_auth_pass));
    else                          prefs.getString("cl_pass",  s.cloud_auth_pass, sizeof(s.cloud_auth_pass));
    // Long API keys (>47 chars) saved under the old smaller buffer would be
    // truncated. NVS only stores what was put, so if the value present is
    // shorter than the new buffer that's fine; nothing extra to do.
    s.auto_select_track  = prefs.getBool  ("auto_trk", s.auto_select_track);
    s.timezone_idx       = prefs.getUChar ("tz",       s.timezone_idx);
    s.internet_mode      = prefs.getUChar ("inet",     s.internet_mode);
    prefs.getString      ("wssid",    s.wifi_ssid, sizeof(s.wifi_ssid));
    prefs.getString      ("wpass",    s.wifi_pass, sizeof(s.wifi_pass));
    s.show_coolant       = prefs.getBool  ("s_temp",   s.show_coolant);
    s.coolant_warn_f     = prefs.getUShort("t_warn",   s.coolant_warn_f);
    s.coolant_warn_col   = prefs.getUChar ("t_col",    s.coolant_warn_col);
    s.show_oil_psi       = prefs.getBool  ("s_psi",    s.show_oil_psi);
    s.oil_warn_psi       = prefs.getUShort("p_warn",   s.oil_warn_psi);
    s.oil_warn_col       = prefs.getUChar ("p_col",    s.oil_warn_col);
    s.sensor_type        = prefs.getUChar ("srctyp",   s.sensor_type);
    s.show_afr           = prefs.getBool  ("s_afr",    s.show_afr);
    s.afr_warn_lo_x10    = prefs.getUShort("afr_lo",   s.afr_warn_lo_x10);
    s.afr_warn_hi_x10    = prefs.getUShort("afr_hi",   s.afr_warn_hi_x10);
    s.afr_warn_col       = prefs.getUChar ("afr_col",  s.afr_warn_col);
    if (s.timezone_idx >= N_TIMEZONES) s.timezone_idx = 0;   // sanitise stale NVS
    {
        char ltrk[64] = "";
        prefs.getString("last_trk",   ltrk,               sizeof(ltrk));
        prefs.getString("last_trk_d", active_track_name,  sizeof(active_track_name));
        last_track_idx = -1;
        for (int i = 0; i < N_TRACKS; ++i) {
            if (strcmp(TRACKS[i].name, ltrk) == 0) { last_track_idx = i; break; }
        }
        // If no display name was saved yet, fall back to the base track name.
        if (active_track_name[0] == '\0' && last_track_idx >= 0)
            strncpy(active_track_name, TRACKS[last_track_idx].name, sizeof(active_track_name) - 1);
    }
    prefs.end();
}

static void saveSettings() {
    prefs.begin("dash", false);
    prefs.putUShort("rpm_min",  s.rpm_min);
    prefs.putUShort("rpm_max",  s.rpm_max);
    prefs.putBool  ("alerts",   s.alerts_enabled);
    prefs.putUShort("a1_rpm",   s.alert1_rpm);
    prefs.putUChar ("a1_col",   s.alert1_color_idx);
    prefs.putUChar ("a1_hz",    s.alert1_hz);
    prefs.putUShort("am_rpm",   s.alertmax_rpm);
    prefs.putUChar ("am_col",   s.alertmax_color_idx);
    prefs.putUChar ("am_hz",    s.alertmax_hz);
    prefs.putBool  ("rec_sd",   s.record_sd);
    prefs.putBool  ("rec_cl",   s.record_cloud);
    prefs.putString("cl_host",  s.cloud_host);
    prefs.putUShort("cl_port",  s.cloud_port);
    prefs.putUChar ("cl_proto", s.cloud_protocol);
    prefs.putUChar ("cl_strm",  s.cloud_stream);
    prefs.putString("cl_email", s.cloud_auth_user);   // user email
    prefs.putString("cl_key",   s.cloud_auth_pass);   // API key
    // Clean up the renamed legacy keys so they don't shadow the new ones on
    // next boot's loadSettings() (the load path prefers cl_email/cl_key if
    // present, but stale cl_user/cl_pass values are confusing in debug dumps).
    if (prefs.isKey("cl_user")) prefs.remove("cl_user");
    if (prefs.isKey("cl_pass")) prefs.remove("cl_pass");
    prefs.putBool  ("auto_trk", s.auto_select_track);
    // (sendCfgToTeensy() is called at end of this function so any save also
    // re-syncs the cloud config to the Teensy.)
    prefs.putUChar ("tz",       s.timezone_idx);
    prefs.putUChar ("inet",     s.internet_mode);
    prefs.putString("wssid",    s.wifi_ssid);
    prefs.putString("wpass",    s.wifi_pass);
    prefs.putBool  ("s_temp",   s.show_coolant);
    prefs.putUShort("t_warn",   s.coolant_warn_f);
    prefs.putUChar ("t_col",    s.coolant_warn_col);
    prefs.putBool  ("s_psi",    s.show_oil_psi);
    prefs.putUShort("p_warn",   s.oil_warn_psi);
    prefs.putUChar ("p_col",    s.oil_warn_col);
    prefs.putUChar ("srctyp",   s.sensor_type);
    prefs.putBool  ("s_afr",    s.show_afr);
    prefs.putUShort("afr_lo",   s.afr_warn_lo_x10);
    prefs.putUShort("afr_hi",   s.afr_warn_hi_x10);
    prefs.putUChar ("afr_col",  s.afr_warn_col);
    prefs.end();
    sendCfgToTeensy();   // keep Teensy in sync after every settings save
}

// Push the runtime config the Teensy needs to do cloud uploads. Format:
//   CFG,<key>,<value>\n   (one line per setting; Teensy stores in g_cfg)
// Call after loadSettings() at boot and at the end of saveSettings(). The
// Teensy parser tolerates unknown keys, so we can grow this list freely.
static void sendCfgToTeensy() {
    Serial.printf("CFG,cl_host,%s\n",   s.cloud_host);
    Serial.printf("CFG,cl_port,%u\n",   (unsigned)s.cloud_port);
    Serial.printf("CFG,cl_proto,%u\n",  (unsigned)s.cloud_protocol);
    Serial.printf("CFG,cl_strm,%u\n",   (unsigned)s.cloud_stream);
    Serial.printf("CFG,cl_email,%s\n",  s.cloud_auth_user);
    Serial.printf("CFG,cl_key,%s\n",    s.cloud_auth_pass);
    Serial.printf("CFG,rec_sd,%d\n",    (int)s.record_sd);
    Serial.printf("CFG,rec_cl,%d\n",    (int)s.record_cloud);
    Serial.printf("CFG,inet,%u\n",      (unsigned)s.internet_mode);
}

static void saveLastTrack(int idx, const char* display_name = nullptr) {
    if (idx < 0 || idx >= N_TRACKS) return;
    last_track_idx = idx;
    const char* dn = display_name ? display_name : TRACKS[idx].name;
    strncpy(active_track_name, dn, sizeof(active_track_name) - 1);
    active_track_name[sizeof(active_track_name) - 1] = '\0';
    prefs.begin("dash", false);
    prefs.putString("last_trk",   TRACKS[idx].name);
    prefs.putString("last_trk_d", active_track_name);
    prefs.end();
}

// ---------------------------------------------------------------------------
// Page state machine.
// ---------------------------------------------------------------------------
enum Page : uint8_t {
    PAGE_DASH          = 0,
    PAGE_SETTINGS      = 1,
    PAGE_NUM_KB        = 2,
    PAGE_TEXT_KB       = 3,
    PAGE_TRACK_PICKER  = 4,
    PAGE_CONFIG_PICKER = 5,
    PAGE_STATUS        = 6,
    PAGE_TIME_SET      = 7,
    PAGE_WIFI_SCAN     = 8,
    PAGE_UPLOAD        = 9,   // full-screen modal during file upload; blocks all other input
    PAGE_OTA           = 10,  // full-screen modal during firmware update check / install
    PAGE_TOOLS         = 11,  // maintenance actions: Check for updates, Format SD
};
static Page    currentPage     = PAGE_DASH;
static bool    pageJustEntered = true;

// Keyboard popup state — shared between the numeric (port) and text (host)
// keyboards. editBuf is mutated by tap handlers; on DONE we copy it back to
// the target setting.
struct KeyboardState {
    SettingId target = ST_RPM_MIN;       // which setting we're editing
    char      editBuf[96] = "";          // 95 char value max (fits SHA-256 hex + b64 etc.)
    uint8_t   editLen     = 0;
    bool      shift       = false;       // SHIFT (caps-lock) state for text keyboard
    bool      dirty       = true;        // edit-field re-render needed
    bool      keys_dirty  = true;        // full key grid re-render needed (shift toggle)
};
static KeyboardState kb;
// (LastDrawn struct below tracks per-element cached values; bg lives there.)

// ---------------------------------------------------------------------------
// UART parser.
// ---------------------------------------------------------------------------
static const char* fixName(uint8_t fix) {
    switch (fix) {
        case 0: return "NONE";
        case 1: return "DR";
        case 2: return "2D";
        case 3: return "3D";
        case 4: return "3D+DR";
        case 5: return "TIME";
        default: return "?";
    }
}
static const char* gpsStatusName(uint8_t s) {
    switch (s) {
        case 0: return "OFF";
        case 1: return "RAW";
        case 2: return "OK";
        case 3: return "STALE";
        default: return "?";
    }
}
static uint16_t gpsStatusColor(uint8_t s) {
    switch (s) {
        case 2: return TFT_GREEN;
        case 1: return TFT_YELLOW;
        case 3: return TFT_YELLOW;
        default: return TFT_RED;
    }
}

static String rxBuf;

static bool parseGpsLine(const String& line) {
    int idx[8], n = 0;
    for (int i = 0; i < (int)line.length() && n < 8; ++i) {
        if (line[i] == ',') idx[n++] = i;
    }
    if (n < 6) return false;
    idx[n] = line.length();
    auto field = [&](int k) { return line.substring(idx[k] + 1, idx[k + 1]); };
    g.fix     = (uint8_t)field(0).toInt();
    g.sats    = (uint8_t)field(1).toInt();
    g.lat_deg = field(2).toFloat();
    g.lon_deg = field(3).toFloat();
    g.mph     = field(4).toFloat();
    g.hdg_deg = field(5).toFloat();
    g.status  = (n >= 7) ? (uint8_t)field(6).toInt() : 0;
    g.last_ms = millis();
    return true;
}
static bool parseEngLine(const String& line) {
    // ENG,<rpm>[,<oil_psi_x10>,<coolant_f_x10>] — 1 OR 3 fields.
    // Older Teensy firmware (or boot before sensors stabilise) sends only
    // the rpm field; the oil/coolant values stay at -1 in that case.
    int idx[4], n = 0;
    for (int i = 0; i < (int)line.length() && n < 4; ++i)
        if (line[i] == ',') idx[n++] = i;
    if (n < 1) return false;
    idx[n] = line.length();
    auto field = [&](int k) { return line.substring(idx[k] + 1, idx[k + 1]); };

    long rpm = field(0).toInt();
    if (rpm < 0)     rpm = 0;
    if (rpm > 65535) rpm = 65535;
    eng.rpm = (uint16_t)rpm;

    if (n >= 3) {
        eng.oil_psi_x10   = (int16_t)field(1).toInt();
        eng.coolant_f_x10 = (int16_t)field(2).toInt();
    }
    eng.last_ms = millis();
    return true;
}
// Format ms into "M:SS.cs" (centiseconds), e.g. "1:23.45".
static void formatLapTime(uint32_t ms, char* buf, int bufLen) {
    const uint32_t cs = ms / 10;
    const uint32_t s  = cs / 100;
    const uint32_t m  = s  / 60;
    snprintf(buf, bufLen, "%u:%02u.%02u", (unsigned)m,
             (unsigned)(s % 60), (unsigned)(cs % 100));
}

// Wipe lap timer state; call at every recording start so times don't bleed
// across sessions. LastDrawn invalidation happens via pageJustEntered.
static void resetLapTimer() {
    lapTimer = LapTimer{};
}

// Called after every fresh GPS parse. Runs regardless of recording state —
// lap timing is based purely on GPS position vs the known S/F line for
// whatever track the car is at. START/STOP only controls data recording.
static void updateLapTimer() {
    if (g.fix < 2) return;    // no usable fix — pause, don't reset

    const int tIdx = closestTrackIdx();
    const uint32_t now = millis();

    if (tIdx < 0) {
        // Not at any known track — clear state so display shows "--:--.--".
        if (lapTimer.active) lapTimer = LapTimer{};
        return;
    }

    // First activation or track changed — initialize fresh.
    if (!lapTimer.active || lapTimer.track_idx != tIdx) {
        lapTimer              = LapTimer{};
        lapTimer.active       = true;
        lapTimer.track_idx    = tIdx;
        lapTimer.lap_start_ms = now;
        lapTimer.prev_gps_ms  = now;
        return;
    }

    // Integrate distance: speed (mph) × elapsed hours = miles.
    const float dt_h = (float)(now - lapTimer.prev_gps_ms) * (1.0f / 3600000.0f);
    lapTimer.dist_miles += g.mph * dt_h;
    lapTimer.prev_gps_ms = now;

    // Distance from THIS track's start/finish line.
    const TrackInfo& track = TRACKS[tIdx];
    const float sfKm = trackDistanceKm(g.lat_deg, g.lon_deg, track.sf_lat, track.sf_lon);
    const uint32_t elapsed = now - lapTimer.lap_start_ms;

    if (!lapTimer.left_start && sfKm > LAP_RADIUS_KM * 2.0f) {
        lapTimer.left_start = true;
    }
    if (lapTimer.left_start && sfKm <= LAP_RADIUS_KM && elapsed >= MIN_LAP_MS) {
        if (lapTimer.timing_started) {
            // Clean completed lap — record it.
            lapTimer.last_lap_ms   = elapsed;
            lapTimer.last_lap_dist = lapTimer.dist_miles;
        }
        // First crossing just arms the timer; subsequent ones record lap times.
        lapTimer.timing_started = true;
        lapTimer.lap_start_ms   = now;
        lapTimer.dist_miles     = 0.0f;
        lapTimer.left_start     = false;
        lapTimer.prev_gps_ms    = now;
    }
}

// Extrapolate a predicted final lap time. Returns 0 when there's not yet
// enough data for a meaningful estimate (< 20 % of last lap distance done).
static uint32_t predictiveLapMs() {
    if (!lapTimer.active)               return 0;
    if (lapTimer.last_lap_ms   == 0)   return 0;
    if (lapTimer.last_lap_dist < 0.001f) return 0;
    if (lapTimer.dist_miles < lapTimer.last_lap_dist * 0.20f) return 0;
    const uint32_t elapsed = millis() - lapTimer.lap_start_ms;
    const float ratio = lapTimer.last_lap_dist / lapTimer.dist_miles;
    return (uint32_t)((float)elapsed * ratio);
}

// ECU,<rpm>,<clt_f_x10>,<map_x10>,<tps_x10>,<afr_x10>,<iat_f_x10>,<bat_x10>
// Emitted by the Teensy once the MS3 CAN transceiver is wired and broadcast
// frames are decoded. Until that lands, no ECU lines arrive and the dash
// leaves ecu.* at -1, which the renderer treats as "---".
//
// All sensor fields are x10 fixed-point integers (e.g. 1450 = 14.5 AFR);
// -1 in any slot means "this field not available from MS3 right now".
static bool parseEcuLine(const String& line) {
    int idx[9], n = 0;
    for (int i = 0; i < (int)line.length() && n < 9; ++i)
        if (line[i] == ',') idx[n++] = i;
    if (n < 1) return false;
    idx[n] = line.length();
    auto field = [&](int k) { return line.substring(idx[k] + 1, idx[k + 1]); };

    long rpm = field(0).toInt();
    if (rpm < 0)     rpm = 0;
    if (rpm > 65535) rpm = 65535;
    ecu.rpm = (uint16_t)rpm;

    if (n >= 2) ecu.coolant_f_x10 = (int16_t)field(1).toInt();
    if (n >= 3) ecu.map_x10       = (int16_t)field(2).toInt();
    if (n >= 4) ecu.tps_x10       = (int16_t)field(3).toInt();
    if (n >= 5) ecu.afr_x10       = (int16_t)field(4).toInt();
    if (n >= 6) ecu.iat_f_x10     = (int16_t)field(5).toInt();
    if (n >= 7) ecu.bat_x10       = (int16_t)field(6).toInt();
    ecu.last_ms = millis();
    return true;
}

static bool parseImuLine(const String& line) {
    // IMU,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
    int idx[7], n = 0;
    for (int i = 0; i < (int)line.length() && n < 7; ++i)
        if (line[i] == ',') idx[n++] = i;
    if (n < 6) return false;
    idx[n] = line.length();
    auto field = [&](int k) { return line.substring(idx[k] + 1, idx[k + 1]); };
    imu.ax = field(0).toFloat();
    imu.ay = field(1).toFloat();
    imu.az = field(2).toFloat();
    imu.gx = field(3).toFloat();
    imu.gy = field(4).toFloat();
    imu.gz = field(5).toFloat();
    imu.last_ms = millis();
    return true;
}

static bool parseEthLine(const String& line) {
    // ETH,<ip>  — sent by Teensy once DHCP assigns an address
    const int comma = line.indexOf(',');
    if (comma < 0) return false;
    const String ip = line.substring(comma + 1);
    strncpy(active_ip, ip.c_str(), sizeof(active_ip) - 1);
    active_ip[sizeof(active_ip) - 1] = '\0';
    return true;
}

static bool parseTimeLine(const String& line) {
    // TIME,<unix_epoch>  — RTC value from the Teensy, sent every 2 s
    const int comma = line.indexOf(',');
    if (comma < 0) return false;
    rtc_epoch = (uint32_t)line.substring(comma + 1).toInt();
    return true;
}

static bool parseSdLine(const String& line) {
    // SD,NONE | SD,FMT,<mb> | SD,READY,<mb>,<free_mb> | SD,ERR | SD,ACTIVE
    const int c1 = line.indexOf(',');
    if (c1 < 0) return false;
    const int c2 = line.indexOf(',', c1 + 1);
    const int c3 = (c2 >= 0) ? line.indexOf(',', c2 + 1) : -1;
    const String tag = (c2 >= 0) ? line.substring(c1 + 1, c2) : line.substring(c1 + 1);
    const uint8_t prev = sd_card_status;
    if (tag == "NONE") {
        sd_card_status = 0; sd_total_mb = 0; sd_free_mb = 0;
    } else if (tag == "FMT") {
        sd_card_status = 1; sd_free_mb = 0;
        sd_total_mb = (c2 >= 0) ? line.substring(c2 + 1, c3 >= 0 ? c3 : (int)line.length()).toInt() : 0;
    } else if (tag == "READY") {
        sd_card_status = 2;
        sd_total_mb = (c2 >= 0) ? line.substring(c2 + 1, c3 >= 0 ? c3 : (int)line.length()).toInt() : 0;
        sd_free_mb  = (c3 >= 0) ? line.substring(c3 + 1).toInt() : 0;
    } else if (tag == "ERR") {
        sd_card_status = 3; sd_total_mb = 0; sd_free_mb = 0;
    } else if (tag == "ACTIVE") {
        sd_card_status = 4;
    } else if (tag == "REC") {
        // SD,REC,<0|1>,<filename>,<samples>
        //   c1=after SD, c2=after REC, c3=after 0|1, c4=after filename
        const int c4 = (c3 >= 0) ? line.indexOf(',', c3 + 1) : -1;
        sd_session_active = (c3 >= 0) ? (line.substring(c2 + 1, c3).toInt() != 0) : false;
        if (c3 >= 0) {
            const int end = (c4 >= 0) ? c4 : (int)line.length();
            line.substring(c3 + 1, end).toCharArray(sd_session_file, sizeof(sd_session_file));
        } else {
            sd_session_file[0] = '\0';
        }
        sd_session_samples = (c4 >= 0) ? line.substring(c4 + 1).toInt() : 0;
        return true;
    } else {
        return false;
    }
    if (sd_card_status != prev) settingsDirty = true;
    return true;
}

// Pop the modal full-screen, save the current page so we can restore it after.
static void openUploadModal(const char* filename, uint32_t total) {
    strncpy(upload_file, filename, sizeof(upload_file) - 1);
    upload_file[sizeof(upload_file) - 1] = '\0';
    upload_total       = total;
    upload_done        = 0;
    upload_active      = true;
    upload_modal_dirty = true;
    upload_result_msg[0] = '\0';
    // Only remember the return page if we weren't already showing the modal
    // (back-to-back uploads from queue drain shouldn't lose the original page).
    if (currentPage != PAGE_UPLOAD) upload_return_page = (uint8_t)currentPage;
    currentPage     = PAGE_UPLOAD;
    pageJustEntered = true;
}

static void closeUploadModal() {
    upload_active      = false;
    currentPage        = (Page)upload_return_page;
    pageJustEntered    = true;
    settingsDirty      = true;
    invalidateAll();
}

static bool parseVerLine(const String& line) {
    // VER,<who>,<version>   e.g. "VER,teensy,0.1.0"
    const int c1 = line.indexOf(',');
    if (c1 < 0) return false;
    const int c2 = line.indexOf(',', c1 + 1);
    if (c2 < 0) return false;
    const String who = line.substring(c1 + 1, c2);
    const String ver = line.substring(c2 + 1);
    if (who == "teensy") {
        ver.toCharArray(teensy_fw_version, sizeof(teensy_fw_version));
        // NOTE: do NOT Serial.printf() here — Serial is UART0 which goes back
        // to the Teensy. Any diagnostic would round-trip and the Teensy would
        // log it as 'unknown dash cmd'. If we ever add a separate USB CDC for
        // dash diagnostics, log it there instead.
    }
    return true;
}

static bool parseUploadLine(const String& line) {
    // UPLOAD,START,<filename>,<total_bytes>
    // UPLOAD,PROG,<bytes_done>
    // UPLOAD,DONE,<OK|FAIL|CANCELLED>
    const int c1 = line.indexOf(',');
    if (c1 < 0) return false;
    const int c2 = line.indexOf(',', c1 + 1);
    const String tag = (c2 >= 0) ? line.substring(c1 + 1, c2) : line.substring(c1 + 1);
    if (tag == "START") {
        const int c3 = (c2 >= 0) ? line.indexOf(',', c2 + 1) : -1;
        if (c3 < 0) return false;
        const String fn = line.substring(c2 + 1, c3);
        const uint32_t total = (uint32_t)line.substring(c3 + 1).toInt();
        char fnBuf[64]; fn.toCharArray(fnBuf, sizeof(fnBuf));
        openUploadModal(fnBuf, total);
        return true;
    }
    if (tag == "PROG") {
        if (c2 < 0) return false;
        upload_done        = (uint32_t)line.substring(c2 + 1).toInt();
        upload_modal_dirty = true;
        return true;
    }
    if (tag == "DONE") {
        const String status = (c2 >= 0) ? line.substring(c2 + 1) : String("OK");
        // Hold the modal up for 800 ms with a status banner so the user
        // sees the outcome rather than a flash-and-gone.
        snprintf(upload_result_msg, sizeof(upload_result_msg), "%s", status.c_str());
        upload_modal_dirty  = true;
        upload_last_draw_ms = millis();   // start the post-DONE timer
        upload_done         = upload_total;
        return true;
    }
    return false;
}

static bool parseCldLine(const String& line) {
    // CLD,<live_ok>,<queue_depth>
    const int c1 = line.indexOf(',');
    const int c2 = (c1 >= 0) ? line.indexOf(',', c1 + 1) : -1;
    if (c1 < 0 || c2 < 0) return false;
    cloud_live_ok     = (line.substring(c1 + 1, c2).toInt() != 0);
    cloud_queue_depth = (uint32_t)line.substring(c2 + 1).toInt();
    return true;
}

static bool parseLine(const String& line) {
    if (line.startsWith("GPS,")) {
        const bool ok = parseGpsLine(line);
        if (ok) updateLapTimer();
        return ok;
    }
    if (line.startsWith("ENG,")) return parseEngLine(line);
    if (line.startsWith("ECU,")) return parseEcuLine(line);
    if (line.startsWith("IMU,")) return parseImuLine(line);
    if (line.startsWith("ETH,"))  return parseEthLine(line);
    if (line.startsWith("SD,"))   return parseSdLine(line);
    if (line.startsWith("CLD,"))  return parseCldLine(line);
    if (line.startsWith("TIME,")) return parseTimeLine(line);
    if (line.startsWith("UPLOAD,")) return parseUploadLine(line);
    if (line.startsWith("VER,"))    return parseVerLine(line);
    return false;
}
static void pumpUart() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') { parseLine(rxBuf); rxBuf = ""; }
        else if (rxBuf.length() < 256) { rxBuf += c; }
        else { rxBuf = ""; }
    }
}

// ---------------------------------------------------------------------------
// Touch handling — swipe detection + tap dispatch.
// Tracks touch start/end, classifies into tap or horizontal swipe on release.
// ---------------------------------------------------------------------------
// Settings page can grow taller than the screen — drag-to-scroll via finger.
// Header and footer are sticky; only the row band scrolls.
static int  settingsScrollY = 0;
static int  settingsContentHeight = 0;       // computed in drawSettingsPage()
static void clampSettingsScroll();

enum Gesture : uint8_t {
    GESTURE_NONE = 0,             // not yet classified
    GESTURE_DRAG_V,               // active vertical drag (settings scroll)
    GESTURE_SWIPE_H,              // tentative horizontal swipe — confirmed on release
};

struct TouchTracker {
    bool     active           = false;
    int32_t  startX           = 0;
    int32_t  startY           = 0;
    int32_t  lastX            = 0;
    int32_t  lastY            = 0;
    uint32_t startMs          = 0;
    Gesture  gesture          = GESTURE_NONE;
    int      scrollAtStart    = 0;
};
static TouchTracker tt;

constexpr int  SWIPE_DX_MIN     = 120;
constexpr int  SWIPE_DY_MAX     = 100;
constexpr uint32_t SWIPE_MS_MAX = 800;
constexpr int  TAP_DXY_MAX      = 30;
constexpr uint32_t TAP_MS_MAX   = 600;
constexpr int  GESTURE_THRESH   = 30;        // movement before we classify

// Forward decls.
static void handleSettingsTap(int x, int y);
static void handleDashTap(int x, int y);
static void handleTimeSetTap(int x, int y);
static void openTrackPicker(bool for_recording = false);
static void openConfigPicker(int track_idx, bool from_auto, bool for_recording = false);
static void handleConfigPickerTap(int x, int y);
static void handleWifiScannerTap(int x, int y);
static void drawWifiScannerPage();
static void drawUploadModal();
static void handleUploadModalTap(int x, int y);
static bool parseUploadLine(const String& line);
static void drawOtaModal();
static void handleOtaModalTap(int x, int y);
static void otaTick();
static void otaStart();
static void drawToolsPage();
static void handleToolsTap(int x, int y);
static bool parseVerLine(const String& line);
static void handleStatusTap(int x, int y);

static void handleTouch() {
    int32_t x, y;
    const bool now = tft.getTouch(&x, &y);

    // Keyboard + track picker pages use a tap-only model (with vertical
    // drag for picker scrolling). No swipe-back; CANCEL is the way out.
    // While an upload is active the modal owns the screen — only its CANCEL
    // button accepts input. Drop every other touch event.
    if (currentPage == PAGE_UPLOAD) {
        if (now && !tt.active) {
            tt.startX = x; tt.startY = y;
            tt.lastX  = x; tt.lastY  = y;
            tt.startMs = millis();
            tt.active  = true;
        } else if (!now && tt.active) {
            handleUploadModalTap(tt.startX, tt.startY);
            tt.active = false;
        }
        return;
    }
    // OTA modal: same as upload — only buttons on the modal are tappable.
    if (currentPage == PAGE_OTA) {
        if (now && !tt.active) {
            tt.startX = x; tt.startY = y;
            tt.lastX  = x; tt.lastY  = y;
            tt.startMs = millis();
            tt.active  = true;
        } else if (!now && tt.active) {
            handleOtaModalTap(tt.startX, tt.startY);
            tt.active = false;
        }
        return;
    }
    if (currentPage == PAGE_NUM_KB || currentPage == PAGE_TEXT_KB ||
        currentPage == PAGE_CONFIG_PICKER || currentPage == PAGE_TIME_SET ||
        currentPage == PAGE_WIFI_SCAN) {
        if (now && !tt.active) {
            tt.startX = x; tt.startY = y;
            tt.lastX  = x; tt.lastY  = y;
            tt.startMs = millis();
            tt.active  = true;
        } else if (!now && tt.active) {
            if      (currentPage == PAGE_CONFIG_PICKER) handleConfigPickerTap(tt.startX, tt.startY);
            else if (currentPage == PAGE_TIME_SET)      handleTimeSetTap(tt.startX, tt.startY);
            else if (currentPage == PAGE_WIFI_SCAN)     handleWifiScannerTap(tt.startX, tt.startY);
            else                                        handleKeyboardTap(tt.startX, tt.startY);
            tt.active = false;
        } else if (now && tt.active) {
            tt.lastX = x; tt.lastY = y;
        }
        return;
    }
    if (currentPage == PAGE_TRACK_PICKER) {
        if (now && !tt.active) {
            tt.startX = x; tt.startY = y;
            tt.lastX  = x; tt.lastY  = y;
            tt.startMs = millis();
            tt.active  = true;
            tt.gesture = GESTURE_NONE;
            tt.scrollAtStart = tp.scrollY;
        } else if (now && tt.active) {
            const int dx = x - tt.startX;
            const int dy = y - tt.startY;
            if (tt.gesture == GESTURE_NONE
                && (abs(dx) > GESTURE_THRESH || abs(dy) > GESTURE_THRESH)) {
                tt.gesture = (abs(dy) > abs(dx)) ? GESTURE_DRAG_V : GESTURE_SWIPE_H;
            }
            if (tt.gesture == GESTURE_DRAG_V) {
                tp.scrollY = tt.scrollAtStart - dy;
                clampPickerScroll();
                tp.dirty = true;
            }
            tt.lastX = x; tt.lastY = y;
        } else if (!now && tt.active) {
            if (tt.gesture == GESTURE_NONE) {
                handleTrackPickerTap(tt.startX, tt.startY);
            }
            tt.active = false;
        }
        return;
    }

    if (now && !tt.active) {
        // ---- Touch start ----
        tt.startX = x; tt.startY = y;
        tt.lastX  = x; tt.lastY  = y;
        tt.startMs = millis();
        tt.active  = true;
        tt.gesture = GESTURE_NONE;
        tt.scrollAtStart = settingsScrollY;
        return;
    }

    if (now && tt.active) {
        // ---- Touch dragging ----
        const int dxFromStart = x - tt.startX;
        const int dyFromStart = y - tt.startY;

        // Classify on first significant movement.
        if (tt.gesture == GESTURE_NONE
            && (abs(dxFromStart) > GESTURE_THRESH || abs(dyFromStart) > GESTURE_THRESH)) {
            if (currentPage == PAGE_SETTINGS && abs(dyFromStart) > abs(dxFromStart)) {
                tt.gesture = GESTURE_DRAG_V;
            } else {
                tt.gesture = GESTURE_SWIPE_H;
            }
        }

        if (tt.gesture == GESTURE_DRAG_V) {
            settingsScrollY = tt.scrollAtStart - dyFromStart;
            clampSettingsScroll();
            settingsDirty = true;
        }

        tt.lastX = x; tt.lastY = y;
        return;
    }

    if (!now && tt.active) {
        // ---- Touch release ----
        const int      dx  = tt.lastX - tt.startX;
        const int      dy  = tt.lastY - tt.startY;
        const uint32_t dur = millis() - tt.startMs;

        if (tt.gesture == GESTURE_DRAG_V) {
            // Already handled per-frame; nothing else to do.
        } else if (tt.gesture == GESTURE_SWIPE_H) {
            if (abs(dx) > SWIPE_DX_MIN && abs(dy) < SWIPE_DY_MAX && dur < SWIPE_MS_MAX) {
                if (dx < 0 && currentPage == PAGE_DASH) {
                    currentPage = PAGE_SETTINGS;
                    pageJustEntered = true;
                } else if (dx > 0 && currentPage == PAGE_SETTINGS) {
                    saveSettings();
                    currentPage = PAGE_DASH;
                    pageJustEntered = true;
                } else if (dx < 0 && currentPage == PAGE_SETTINGS) {
                    saveSettings();
                    currentPage = PAGE_STATUS;
                    pageJustEntered = true;
                } else if (dx > 0 && currentPage == PAGE_STATUS) {
                    currentPage = PAGE_SETTINGS;
                    pageJustEntered = true;
                    settingsDirty = true;
                } else if (dx < 0 && currentPage == PAGE_STATUS) {
                    currentPage = PAGE_TOOLS;
                    pageJustEntered = true;
                } else if (dx > 0 && currentPage == PAGE_TOOLS) {
                    currentPage = PAGE_STATUS;
                    pageJustEntered = true;
                }
            }
        } else {  // GESTURE_NONE — never moved much, treat as tap
            if (abs(dx) < TAP_DXY_MAX && abs(dy) < TAP_DXY_MAX && dur < TAP_MS_MAX) {
                if (currentPage == PAGE_DASH)          handleDashTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_SETTINGS) handleSettingsTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_TOOLS)    handleToolsTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_STATUS)   handleStatusTap(tt.startX, tt.startY);
            }
        }
        tt.active = false;
    }
}

static void handleDashTap(int x, int y) {
    // TRACK button — opens picker in select-only mode (no recording start).
    if (x >= TRKBTN_X && x < TRKBTN_X + TRKBTN_W &&
        y >= TRKBTN_Y && y < TRKBTN_Y + TRKBTN_H) {
        openTrackPicker(false);
        return;
    }

    // Start/Stop button.
    if (x >= RECBTN_X && x < RECBTN_X + RECBTN_W &&
        y >= RECBTN_Y && y < RECBTN_Y + RECBTN_H) {
        if (recording) {
            recording = false;
            Serial.printf("REC,0\n");
            return;
        }
        // START — try GPS auto-select first (only when enabled).
        if (s.auto_select_track) {
            int idx = -1;
            if (last_track_idx >= 0) {
                const float km = trackDistanceKm(g.lat_deg, g.lon_deg,
                                                 TRACKS[last_track_idx].lat,
                                                 TRACKS[last_track_idx].lon);
                if (km <= TRACKS[last_track_idx].radius_km) idx = last_track_idx;
            }
            if (idx < 0) idx = closestTrackIdx();
            if (idx >= 0) {
                if (TRACKS[idx].n_configs > 0) {
                    openConfigPicker(idx, true, true);   // from_auto, for_recording
                } else {
                    saveLastTrack(idx);
                    Serial.printf("TRACK,%s\n", active_track_name);
                    Serial.printf("REC,1\n");
                    recording = true; rec_start_ms = millis();
                }
                return;
            }
        }
        // No auto-select or no GPS match. If a track is already selected, start directly.
        if (active_track_name[0] != '\0') {
            Serial.printf("TRACK,%s\n", active_track_name);
            Serial.printf("REC,1\n");
            recording = true; rec_start_ms = millis();
            return;
        }
        // No track set at all — open picker; confirm will start recording.
        openTrackPicker(true);
    }
}

// ---------------------------------------------------------------------------
// Dash page — RPM bar, huge speed, L/R status columns, optional alert flash.
// ---------------------------------------------------------------------------
namespace {
  constexpr int RPM_BAR_X = 20;
  constexpr int RPM_BAR_Y = 10;
  constexpr int RPM_BAR_W = 760;
  constexpr int RPM_BAR_H = 80;
}

// Smooth green→yellow→red gradient between rpm_min and rpm_max.
static uint16_t rpmBarColor(uint16_t rpm) {
    const uint16_t lo = s.rpm_min, hi = s.rpm_max;
    if (hi <= lo) return TFT_GREEN;
    float ratio = (rpm <= lo) ? 0.0f
                : (rpm >= hi) ? 1.0f
                : (float)(rpm - lo) / (float)(hi - lo);
    float rf = (ratio <= 0.5f) ? (ratio * 2.0f) : 1.0f;
    float gf = (ratio <= 0.5f) ? 1.0f           : (1.0f - (ratio - 0.5f) * 2.0f);
    return tft.color565((uint8_t)(rf * 255), (uint8_t)(gf * 255), 0);
}

// Background flash color based on current RPM and alert thresholds.
// Returns the bg color the dash should paint THIS frame:
//   * TFT_BLACK if alerts disabled, RPM below alert1, or "off" half of blink.
//   * Alert1 color or alertmax color during the "on" half.
// Blink rate lerps from alert1_hz at alert1_rpm to alertmax_hz at alertmax_rpm.
static uint16_t computeBgColor() {
    if (!s.alerts_enabled) return TFT_BLACK;
    if (eng.rpm < s.alert1_rpm) return TFT_BLACK;

    uint16_t color;
    float    hz;
    if (eng.rpm >= s.alertmax_rpm) {
        color = PALETTE[s.alertmax_color_idx];
        hz    = (float)s.alertmax_hz;
    } else if (s.alertmax_rpm > s.alert1_rpm) {
        color = PALETTE[s.alert1_color_idx];
        const float frac = (float)(eng.rpm - s.alert1_rpm)
                         / (float)(s.alertmax_rpm - s.alert1_rpm);
        hz = (float)s.alert1_hz + frac * ((float)s.alertmax_hz - (float)s.alert1_hz);
    } else {
        color = PALETTE[s.alert1_color_idx];
        hz    = (float)s.alert1_hz;
    }
    if (hz < 1.0f) hz = 1.0f;
    const uint32_t halfPeriod = (uint32_t)(500.0f / hz);   // ms per half-cycle
    if (halfPeriod == 0) return color;
    const bool on = ((millis() / halfPeriod) & 1) == 0;
    return on ? color : TFT_BLACK;
}

// Cache the last-drawn value of every dynamic field. Drawing happens only
// when the new value differs from the cached one — this is what eliminates
// the per-frame flicker. Dirtied to force-redraw on bg state changes.
struct LastDrawn {
    uint16_t bg          = 0xDEAD;
    int16_t  rpm_fillW   = -1;       // last drawn bar fill width in px
    uint16_t rpm_color   = 0;        // last drawn bar fill color
    int32_t  rpm_text    = -1;       // last drawn RPM number under the bar (sentinel = redraw)
    int16_t  spd_x10     = -1;       // mph * 10, integer for stable equality checks
    uint8_t  fix         = 0xFF;
    uint8_t  sats        = 0xFF;
    uint8_t  status      = 0xFF;
    int8_t   recording   = -1;       // -1=never drawn; 0=stopped; 1=recording
    uint32_t pred_lap_cs = UINT32_MAX;   // predictive lap time in centiseconds
    uint32_t last_lap_cs = UINT32_MAX;   // last completed lap time in centiseconds
    uint32_t track_tag   = UINT32_MAX;   // first 4 bytes of active_track_name; UINT32_MAX = needs redraw
    // Coolant temp / oil PSI cached state — value is x10 fixed-point (INT32_MIN
    // = redraw on next paint). The col_tag composes the current show flag,
    // the resolved colour, and the fault state into one int so any of those
    // changing forces a redraw without 3 separate fields.
    int32_t  temp_x10    = INT32_MIN;
    uint32_t temp_col_tag = UINT32_MAX;
    int32_t  psi_x10     = INT32_MIN;
    uint32_t psi_col_tag  = UINT32_MAX;
    int32_t  afr_x10     = INT32_MIN;
    uint32_t afr_col_tag  = UINT32_MAX;
    // REC badge state: composite tag of (dash recording bit, teensy ack bit,
    // mismatch-warning bit), plus the last-drawn sample count and queue depth.
    uint8_t  rec_badge_tag = 0xFF;
    uint32_t rec_samples  = UINT32_MAX;
    uint32_t cld_queue    = UINT32_MAX;
};
static LastDrawn ld;
static void invalidateAll() {
    ld.rpm_fillW = -1; ld.rpm_text = -1; ld.spd_x10 = -1;
    ld.fix = 0xFF; ld.sats = 0xFF; ld.status = 0xFF;
    ld.recording = -1;
    ld.rec_badge_tag = 0xFF; ld.rec_samples = UINT32_MAX; ld.cld_queue = UINT32_MAX;
    ld.pred_lap_cs = UINT32_MAX; ld.last_lap_cs = UINT32_MAX;
    ld.track_tag   = UINT32_MAX;
    ld.temp_x10 = INT32_MIN; ld.temp_col_tag = UINT32_MAX;
    ld.psi_x10  = INT32_MIN; ld.psi_col_tag  = UINT32_MAX;
    ld.afr_x10  = INT32_MIN; ld.afr_col_tag  = UINT32_MAX;
}

static void drawRecordButton() {
    const uint16_t fill   = recording ? TFT_RED   : TFT_GREEN;
    const uint16_t border = TFT_WHITE;
    const char*    label  = recording ? "STOP"    : "START";

    tft.fillRect(RECBTN_X, RECBTN_Y, RECBTN_W, RECBTN_H, fill);
    // 3-pixel border for some visual weight
    tft.drawRect(RECBTN_X,     RECBTN_Y,     RECBTN_W,     RECBTN_H,     border);
    tft.drawRect(RECBTN_X + 1, RECBTN_Y + 1, RECBTN_W - 2, RECBTN_H - 2, border);
    tft.drawRect(RECBTN_X + 2, RECBTN_Y + 2, RECBTN_W - 4, RECBTN_H - 4, border);

    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, fill);
    tft.drawString(label, RECBTN_X + RECBTN_W / 2, RECBTN_Y + RECBTN_H / 2);
}

static void drawDashPage() {
    // If the Teensy has stopped sending lines (cable yanked, Teensy
    // power loss, etc.) the parsed g/eng state is now stale. Zero it so
    // the dash visibly reflects the disconnect — FIX flips to NONE,
    // SATS to 0, GPS to OFF, speed/heading/lat/lon to 0, RPM bar empties.
    // 2 s threshold so brief glitches don't blank everything.
    const uint32_t nowMs = millis();
    const bool teensyStale = (g.last_ms == 0) || (nowMs - g.last_ms > 2000);
    if (teensyStale) {
        g.fix     = 0;            // -> NONE
        g.sats    = 0;
        g.lat_deg = 0.0f;
        g.lon_deg = 0.0f;
        g.mph     = 0.0f;
        g.hdg_deg = 0.0f;
        g.status  = 0;            // -> OFF (red)
        eng.rpm           = 0;
        eng.oil_psi_x10   = -1;   // -> "PSI: ---" rather than stale last read
        eng.coolant_f_x10 = -1;
        // ECU fields also clear — the CAN bridge runs on the Teensy, so if
        // the Teensy is silent, the MS3 data can't reach us either.
        ecu.coolant_f_x10 = -1;
        ecu.afr_x10       = -1;
        ecu.map_x10       = -1;
        ecu.tps_x10       = -1;
        ecu.iat_f_x10     = -1;
        ecu.bat_x10       = -1;
        // Don't touch g.last_ms / eng.last_ms / ecu.last_ms — those are the
        // parser's freshness timestamps. The next received line refills.
    }

    const uint16_t bg = computeBgColor();

    // Full repaint when bg state flips OR we just entered the page. After
    // that, every other element only repaints when its value changed —
    // text uses textColor(fg, bg) so character cells redraw their own bg
    // and we never go through a "blanked" intermediate frame. That kills
    // the flicker.
    if (pageJustEntered || bg != ld.bg) {
        tft.fillScreen(bg);
        tft.drawRect(RPM_BAR_X,     RPM_BAR_Y,     RPM_BAR_W,     RPM_BAR_H,     TFT_DARKGREY);
        tft.drawRect(RPM_BAR_X + 1, RPM_BAR_Y + 1, RPM_BAR_W - 2, RPM_BAR_H - 2, TFT_DARKGREY);

        // Static labels — drawn once on entry/bg-flip, not per frame.
        // Force size 1 explicitly: previous draws (boot, settings exit) may
        // have left it at a larger size and Font2 with size!=1 looks wrong.
        // (HDG/LAT/LON labels live on PAGE_STATUS now — bottom-left of the
        //  dash is reused for TEMP/PSI engine sensors, painted dynamically.)
        tft.setTextSize(1);
        tft.setFont(&fonts::Font2);
        tft.setTextDatum(textdatum_t::top_left);
        tft.setTextColor(TFT_DARKGREY, bg);
        tft.drawString("PRED", 255, 380);    // middle column — predictive lap time
        tft.drawString("LAP",  255, 405);    // middle column — last completed lap time
        tft.drawString("FIX",  620, 355);
        tft.drawString("SATS", 620, 380);
        tft.drawString("GPS",  620, 405);

        // TRACK button — static, drawn once on enter / bg-flip.
        tft.fillRect(TRKBTN_X, TRKBTN_Y, TRKBTN_W, TRKBTN_H, TFT_DARKCYAN);
        tft.drawRect(TRKBTN_X,     TRKBTN_Y,     TRKBTN_W,     TRKBTN_H,     TFT_WHITE);
        tft.drawRect(TRKBTN_X + 1, TRKBTN_Y + 1, TRKBTN_W - 2, TRKBTN_H - 2, TFT_WHITE);
        tft.setFont(&fonts::Font4);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextColor(TFT_WHITE, TFT_DARKCYAN);
        tft.drawString("TRACK", TRKBTN_X + TRKBTN_W / 2, TRKBTN_Y + TRKBTN_H / 2);

        ld.bg = bg;
        pageJustEntered = false;
        invalidateAll();
    }

    // ---- RPM bar fill (only if the fill width actually moved) ----
    {
        const int ix = RPM_BAR_X + 2;
        const int iy = RPM_BAR_Y + 2;
        const int iw = RPM_BAR_W - 4;
        const int ih = RPM_BAR_H - 4;
        int      fillW = 0;
        uint16_t fillColor = bg;
        if (eng.rpm >= s.rpm_min && s.rpm_max > s.rpm_min) {
            const uint32_t rpmClamped = (eng.rpm > s.rpm_max) ? s.rpm_max : eng.rpm;
            fillW = (int)(((rpmClamped - s.rpm_min) * (uint32_t)iw)
                          / (uint32_t)(s.rpm_max - s.rpm_min));
            fillColor = rpmBarColor(eng.rpm);
        }
        if (fillW != ld.rpm_fillW || fillColor != ld.rpm_color) {
            // Erase only the strip that changed.
            if (fillW < ld.rpm_fillW) {
                tft.fillRect(ix + fillW, iy, ld.rpm_fillW - fillW, ih, bg);
            }
            if (fillW > 0) tft.fillRect(ix, iy, fillW, ih, fillColor);
            ld.rpm_fillW = fillW;
            ld.rpm_color = fillColor;
        }
    }

    // ---- RPM number, just under the bar at the right edge (Font2) ----
    if ((int32_t)eng.rpm != ld.rpm_text) {
        tft.setTextSize(1);
        tft.setFont(&fonts::Font2);
        tft.setTextColor(TFT_LIGHTGREY, bg);
        tft.setTextDatum(textdatum_t::top_right);
        tft.setTextPadding(80);                            // wide enough for "65535"
        char rpmBuf[8]; snprintf(rpmBuf, sizeof(rpmBuf), "%u", (unsigned)eng.rpm);
        tft.drawString(rpmBuf, RPM_BAR_X + RPM_BAR_W, RPM_BAR_Y + RPM_BAR_H + 4);
        tft.setTextPadding(0);
        ld.rpm_text = eng.rpm;
    }

    // ---- HUGE speed number — Font7 size 4 ----
    // Don't pad with spaces (Font7's space is wide; that pushes the visible
    // digits off-centre). Instead use setTextPadding so the framework auto-
    // fills bg around the centred digits to a fixed width — that keeps the
    // visible digits exactly at x=400 and erases any leftover from a longer
    // previous draw in the same operation.
    {
        const int spd_x10 = (int)(g.mph * 10.0f + 0.5f);
        if (spd_x10 != ld.spd_x10) {
            tft.setTextColor(TFT_WHITE, bg);
            tft.setTextDatum(textdatum_t::middle_center);
            tft.setFont(&fonts::Font7);
            tft.setTextSize(4);
            tft.setTextPadding(SPEED_PAD_W);
            char buf[16];
            // Drop the decimal at 100+ mph so we stay 3 digits wide and
            // fit the right-side band without clipping.
            if (g.mph >= 100.0f) snprintf(buf, sizeof(buf), "%.0f", g.mph);
            else                 snprintf(buf, sizeof(buf), "%.1f", g.mph);
            tft.drawString(buf, SPEED_CX, 230);
            tft.setTextPadding(0);
            tft.setTextSize(1);
            ld.spd_x10 = spd_x10;
            ld.recording = -1;        // speed redraw can clip the button — force its repaint
        }
    }

    // ---- Start/Stop button (sits to the left of the speed) ----
    if ((int)recording != ld.recording) {
        drawRecordButton();
        ld.recording = recording;
    }

    // ---- REC badge: confirmation that the Teensy actually opened a file. ----
    // Sits ABOVE the START button in the empty strip just under the RPM bar,
    // so it doesn't overlap the active-track name (which is drawn below the
    // TRACK button). Text is Font2 size 1 — same as FIX/SATS/GPS labels.
    //
    // States:
    //   - hidden                  when dash thinks recording=false
    //   - amber "REC ? no ack"    when dash sent REC,1 but Teensy hasn't
    //                             acked SD,REC,1,... within a grace window
    //   - red   "REC ● N"         when both sides agree, N samples on disk
    // Plus a small cyan "queue: N" replacement when idle + backlog > 0.
    {
        const uint32_t ackGrace = 800;   // ms before "REC ?" appears
        const bool dash_rec  = recording;
        const bool teensy_ok = sd_session_active;
        const bool grace_passed = (rec_start_ms != 0) &&
                                  (millis() - rec_start_ms > ackGrace);
        const bool warn = dash_rec && !teensy_ok && grace_passed;
        const uint8_t tag = (uint8_t)((dash_rec ? 1 : 0) |
                                     (teensy_ok ? 2 : 0) |
                                     (warn     ? 4 : 0));
        const uint32_t samples = teensy_ok ? sd_session_samples : 0;
        const uint32_t qd      = (!dash_rec) ? cloud_queue_depth : 0;
        if (tag != ld.rec_badge_tag || samples != ld.rec_samples || qd != ld.cld_queue) {
            // Band: just below the RPM number's baseline (y=94), above START (y=155).
            constexpr int BX = 30, BY = 125, BW = 220, BH = 22;
            tft.fillRect(BX, BY, BW, BH, bg);
            tft.setFont(&fonts::Font2);
            tft.setTextSize(1);
            tft.setTextDatum(textdatum_t::middle_left);
            if (dash_rec && teensy_ok) {
                tft.fillCircle(BX + 6, BY + BH/2, 5, TFT_RED);
                tft.setTextColor(TFT_WHITE, bg);
                char buf[24]; snprintf(buf, sizeof(buf), "REC  %lu", (unsigned long)samples);
                tft.drawString(buf, BX + 18, BY + BH/2);
            } else if (warn) {
                tft.fillCircle(BX + 6, BY + BH/2, 5, TFT_ORANGE);
                tft.setTextColor(TFT_ORANGE, bg);
                tft.drawString("REC ? no ack", BX + 18, BY + BH/2);
            } else if (qd > 0) {
                // Same slot, replacement message when idle with backlog.
                tft.setTextColor(TFT_CYAN, bg);
                char qbuf[20]; snprintf(qbuf, sizeof(qbuf), "queue: %lu", (unsigned long)qd);
                tft.drawString(qbuf, BX, BY + BH/2);
            }
            tft.setTextDatum(textdatum_t::top_left);
            ld.rec_badge_tag = tag;
            ld.rec_samples   = samples;
            ld.cld_queue     = qd;
        }
    }

    // ---- Status fields ----
    tft.setFont(&fonts::Font2);
    tft.setTextDatum(textdatum_t::top_left);

    // Left column: TEMP / PSI engine sensors (Font4 — visibly larger than the
    // surrounding Font2 status text). Each line is drawn as one string ("TEMP:
    // 220°F") so the warn colour applies to label and value together. Hidden
    // entirely when the show toggle is off; greyed "---" when the Teensy
    // reports a sensor fault (-1 from the analog input).
    auto drawValue = [&](int x, int y, const char* fmtBuf, uint16_t col) {
        tft.setTextColor(col, bg);
        tft.drawString(fmtBuf, x, y);
    };

    constexpr int SENS_X      = 20;
    constexpr int SENS_TEMP_Y = 358;
    constexpr int SENS_PSI_Y  = 398;
    constexpr int SENS_AFR_Y  = 438;
    constexpr int SENS_W      = 230;   // wide enough for "TEMP: 250°F" at Font4
    constexpr int SENS_H      = 32;    // covers Font4 ascender/descender

    // ECU staleness: no CAN frames received in ~2 s → treat MS3 fields as faulted.
    // Matches the existing g.last_ms check at the top of this function.
    const bool ecuStale = (ecu.last_ms == 0) || (nowMs - ecu.last_ms > 2000);

    // ---- Coolant temp line ----
    // Source depends on sensor_type: direct ADC (eng.*) vs MS3 CAN (ecu.*).
    // Oil PSI stays direct regardless — MS3 typically has no oil-PSI input.
    {
        const bool    fromMs3   = (s.sensor_type == 1);
        const int16_t coolant   = fromMs3 ? ecu.coolant_f_x10 : eng.coolant_f_x10;
        const bool    fault     = (coolant < 0) || (fromMs3 && ecuStale);
        const bool    warn_active = s.show_coolant && !fault
                                    && (coolant >= (int)s.coolant_warn_f * 10);
        const uint32_t tag = ((uint32_t)s.show_coolant << 24)
                           | ((uint32_t)s.coolant_warn_col << 16)
                           | ((uint32_t)fault << 8)
                           | ((uint32_t)warn_active)
                           | ((uint32_t)fromMs3 << 12);   // re-render on source flip
        const int32_t  val = s.show_coolant ? (int32_t)coolant : INT32_MIN + 1;
        if (val != ld.temp_x10 || tag != ld.temp_col_tag) {
            // Wipe the slot — turning the line on/off, fault toggling, or
            // shrinking from "250°F" to "98°F" all need stale pixels cleared.
            tft.fillRect(SENS_X, SENS_TEMP_Y, SENS_W, SENS_H, bg);
            if (s.show_coolant) {
                tft.setFont(&fonts::Font4);
                tft.setTextSize(1);
                tft.setTextDatum(textdatum_t::top_left);
                char buf[24];
                uint16_t col;
                if (fault) {
                    snprintf(buf, sizeof(buf), "TEMP: ---");
                    col = TFT_DARKGREY;
                } else {
                    const int t = (coolant + 5) / 10;   // round to whole °F
                    snprintf(buf, sizeof(buf), "TEMP: %d\xB0""F", t);
                    col = warn_active ? PALETTE[s.coolant_warn_col] : TFT_WHITE;
                }
                drawValue(SENS_X, SENS_TEMP_Y, buf, col);
            }
            ld.temp_x10     = val;
            ld.temp_col_tag = tag;
        }
    }

    // ---- Oil pressure line ----
    {
        const bool   fault       = (eng.oil_psi_x10 < 0);
        const bool   warn_active = s.show_oil_psi && !fault
                                   && (eng.oil_psi_x10 <= (int)s.oil_warn_psi * 10);
        const uint32_t tag = ((uint32_t)s.show_oil_psi << 24)
                           | ((uint32_t)s.oil_warn_col << 16)
                           | ((uint32_t)fault << 8)
                           | ((uint32_t)warn_active);
        const int32_t  val = s.show_oil_psi ? (int32_t)eng.oil_psi_x10 : INT32_MIN + 1;
        if (val != ld.psi_x10 || tag != ld.psi_col_tag) {
            tft.fillRect(SENS_X, SENS_PSI_Y, SENS_W, SENS_H, bg);
            if (s.show_oil_psi) {
                tft.setFont(&fonts::Font4);
                tft.setTextSize(1);
                tft.setTextDatum(textdatum_t::top_left);
                char buf[24];
                uint16_t col;
                if (fault) {
                    snprintf(buf, sizeof(buf), "PSI: ---");
                    col = TFT_DARKGREY;
                } else {
                    const int p = (eng.oil_psi_x10 + 5) / 10;
                    snprintf(buf, sizeof(buf), "PSI: %d", p);
                    col = warn_active ? PALETTE[s.oil_warn_col] : TFT_WHITE;
                }
                drawValue(SENS_X, SENS_PSI_Y, buf, col);
            }
            ld.psi_x10     = val;
            ld.psi_col_tag = tag;
        }
    }

    // ---- AFR line ----
    // Visible only in MegaSquirt sensor mode (no direct AFR source exists).
    // Two-sided warn band: too rich (< afr_warn_lo) and too lean (> afr_warn_hi)
    // both flip the value to s.afr_warn_col.
    {
        const bool    visible   = s.show_afr && (s.sensor_type == 1);
        const bool    fault     = !visible || (ecu.afr_x10 < 0) || ecuStale;
        const bool    warn_active = visible && !fault
                                    && (ecu.afr_x10 < (int)s.afr_warn_lo_x10
                                        || ecu.afr_x10 > (int)s.afr_warn_hi_x10);
        const uint32_t tag = ((uint32_t)visible << 24)
                           | ((uint32_t)s.afr_warn_col << 16)
                           | ((uint32_t)fault << 8)
                           | ((uint32_t)warn_active);
        const int32_t  val = visible ? (int32_t)ecu.afr_x10 : INT32_MIN + 1;
        if (val != ld.afr_x10 || tag != ld.afr_col_tag) {
            tft.fillRect(SENS_X, SENS_AFR_Y, SENS_W, SENS_H, bg);
            if (visible) {
                tft.setFont(&fonts::Font4);
                tft.setTextSize(1);
                tft.setTextDatum(textdatum_t::top_left);
                char buf[24];
                uint16_t col;
                if (fault) {
                    snprintf(buf, sizeof(buf), "AFR: ---");
                    col = TFT_DARKGREY;
                } else {
                    snprintf(buf, sizeof(buf), "AFR: %d.%d",
                             ecu.afr_x10 / 10, ecu.afr_x10 % 10);
                    col = warn_active ? PALETTE[s.afr_warn_col] : TFT_WHITE;
                }
                drawValue(SENS_X, SENS_AFR_Y, buf, col);
            }
            ld.afr_x10     = val;
            ld.afr_col_tag = tag;
        }
    }

    // Restore the small font so the FIX/SATS/GPS block below renders correctly.
    tft.setFont(&fonts::Font2);
    tft.setTextDatum(textdatum_t::top_left);

    // Right column: FIX / SATS / GPS. Pushed further right (x=550 label,
    // value at x=610) so they sit closer to the right edge.
    if (g.fix != ld.fix) {
        const uint16_t col = (g.fix >= 3) ? TFT_GREEN : (g.fix >= 2) ? TFT_YELLOW : TFT_RED;
        char buf[8]; snprintf(buf, sizeof(buf), "%-5s", fixName(g.fix));
        drawValue(680,355, buf, col);
        ld.fix = g.fix;
    }
    if (g.sats != ld.sats) {
        char buf[8]; snprintf(buf, sizeof(buf), "%2u", (unsigned)g.sats);
        drawValue(680,380, buf, TFT_WHITE);
        ld.sats = g.sats;
    }
    if (g.status != ld.status) {
        char buf[8]; snprintf(buf, sizeof(buf), "%-5s", gpsStatusName(g.status));
        drawValue(680,405, buf, gpsStatusColor(g.status));
        ld.status = g.status;
    }

    // Middle column: predictive (PRED) and last completed (LAP) lap times.
    // PRED is green if on pace for a faster lap, red if slower, grey when
    // there's not enough data. LAP is white — it's a static fact.
    tft.setFont(&fonts::Font2);
    tft.setTextDatum(textdatum_t::top_left);
    tft.setTextPadding(100);    // wide enough to overwrite "--:--.--" (8 chars)
    {
        const uint32_t predMs = predictiveLapMs();
        const uint32_t cs = predMs / 10;
        if (cs != ld.pred_lap_cs) {
            char buf[12];
            uint16_t col;
            if (predMs == 0) {
                snprintf(buf, sizeof(buf), "--:--.--");
                col = TFT_DARKGREY;
            } else {
                formatLapTime(predMs, buf, sizeof(buf));
                col = (lapTimer.last_lap_ms > 0 && predMs < lapTimer.last_lap_ms)
                      ? TFT_GREEN : TFT_RED;
            }
            tft.setTextColor(col, bg);
            tft.drawString(buf, 305, 380);
            ld.pred_lap_cs = cs;
        }
    }
    {
        const uint32_t cs = lapTimer.last_lap_ms / 10;
        if (cs != ld.last_lap_cs) {
            char buf[12];
            if (lapTimer.last_lap_ms == 0) snprintf(buf, sizeof(buf), "--:--.--");
            else                           formatLapTime(lapTimer.last_lap_ms, buf, sizeof(buf));
            tft.setTextColor(TFT_WHITE, bg);
            tft.drawString(buf, 305, 405);
            ld.last_lap_cs = cs;
        }
    }
    tft.setTextPadding(0);

    // Active track name displayed under the TRACK button (Font2, same as HDG/LAT/LON labels).
    // Changes only when the user confirms a new track, so we track a tag to avoid redrawing
    // every frame. UINT32_MAX sentinel forces draw on page enter (set in invalidateAll).
    {
        uint32_t tag = 0;
        memcpy(&tag, active_track_name, sizeof(tag));
        if (tag != ld.track_tag) {
            tft.setFont(&fonts::Font2);
            tft.setTextSize(1);
            tft.setTextDatum(textdatum_t::top_left);
            tft.setTextPadding(TRKBTN_W + 160);   // wide enough to clear any leftover
            if (active_track_name[0]) {
                tft.setTextColor(TFT_LIGHTGREY, bg);
                tft.drawString(active_track_name, TRKBTN_X, TRKBTN_Y + TRKBTN_H + 6);
            } else {
                tft.fillRect(TRKBTN_X, TRKBTN_Y + TRKBTN_H + 6, TRKBTN_W + 160, 18, bg);
            }
            tft.setTextPadding(0);
            ld.track_tag = tag;
        }
    }
}

// ---------------------------------------------------------------------------
// Settings page — list of rows, each with -/+ buttons or toggle/color tap.
// (SettingId enum and NumBounds struct are forward-declared at the top.)
// ---------------------------------------------------------------------------
struct SettingRow {
    SettingId   id;
    const char* label;
    // ENUM = cycle through string list (PROTOCOL_NAMES, STREAM_NAMES).
    // TEXT = read-only display today; tap will open a popup keyboard once
    //        that's implemented (next iteration).
    // INFO = read-only display row (no controls, no tap action). Used today
    // for the WiFi connection status line in the Internet block.
    enum Kind { NUMERIC, TOGGLE, COLOR, ENUM, TEXT, ACTION, INFO } kind;
};
static const SettingRow ROWS[ST_COUNT] = {
    { ST_INET_MODE,    "Internet",              SettingRow::ENUM    },
    { ST_WIFI_SSID,    "WiFi network (SSID)",   SettingRow::TEXT    },
    { ST_WIFI_PASS,    "WiFi password",         SettingRow::TEXT    },
    { ST_WIFI_STATUS,  "WiFi status",           SettingRow::INFO    },
    { ST_RPM_MIN,    "Min RPM display",      SettingRow::NUMERIC },
    { ST_RPM_MAX,    "Max RPM display",      SettingRow::NUMERIC },
    { ST_ALERTS,     "Enable RPM alerts",    SettingRow::TOGGLE  },
    { ST_A1_RPM,     "Alert 1 RPM",          SettingRow::NUMERIC },
    { ST_A1_COL,     "Alert 1 color",        SettingRow::COLOR   },
    { ST_A1_HZ,      "Alert 1 blink (Hz)",   SettingRow::NUMERIC },
    { ST_AM_RPM,     "MAX alert RPM",        SettingRow::NUMERIC },
    { ST_AM_COL,     "MAX alert color",      SettingRow::COLOR   },
    { ST_AM_HZ,      "MAX alert blink (Hz)", SettingRow::NUMERIC },
    { ST_SHOW_TEMP,    "Show coolant temp",     SettingRow::TOGGLE  },
    { ST_TEMP_WARN_F,  "Coolant warn (\xB0""F)", SettingRow::NUMERIC },
    { ST_TEMP_WARN_COL,"Coolant warn color",    SettingRow::COLOR   },
    { ST_SHOW_PSI,     "Show oil pressure",     SettingRow::TOGGLE  },
    { ST_PSI_WARN_PSI, "Oil low-warn (PSI)",    SettingRow::NUMERIC },
    { ST_PSI_WARN_COL, "Oil warn color",        SettingRow::COLOR   },
    { ST_SENSOR_TYPE,  "Sensor data source",    SettingRow::ENUM    },
    { ST_SHOW_AFR,     "Show AFR (MS3 only)",   SettingRow::TOGGLE  },
    { ST_AFR_WARN_LO,  "AFR rich-warn (x10)",   SettingRow::NUMERIC },
    { ST_AFR_WARN_HI,  "AFR lean-warn (x10)",   SettingRow::NUMERIC },
    { ST_AFR_WARN_COL, "AFR warn color",        SettingRow::COLOR   },
    { ST_REC_SD,     "Record to SD card",    SettingRow::TOGGLE  },
    { ST_REC_CLOUD,  "Record to cloud",      SettingRow::TOGGLE  },
    { ST_CL_HOST,      "Cloud host (DNS/IP)",   SettingRow::TEXT    },
    { ST_CL_PORT,      "Cloud port",            SettingRow::TEXT    },
    { ST_CL_PROTO,     "Cloud protocol",        SettingRow::ENUM    },
    { ST_CL_STREAM,    "Cloud stream mode",     SettingRow::ENUM    },
    { ST_CL_AUTH_USER, "User email",            SettingRow::TEXT    },
    { ST_CL_AUTH_PASS, "API key",               SettingRow::TEXT    },
    { ST_AUTO_TRACK,   "Auto select by GPS",    SettingRow::TOGGLE  },
    { ST_TIMEZONE,     "Time zone",              SettingRow::ENUM    },
    { ST_SET_TIME,     "Set time",                SettingRow::ACTION  },
};

constexpr int SETTINGS_ROW_Y0     = 70;
constexpr int SETTINGS_ROW_DY     = 40;
constexpr int SETTINGS_ROW_HEIGHT = 36;
constexpr int CTRL_MINUS_X = 410, CTRL_MINUS_W = 60;
constexpr int CTRL_PLUS_X  = 720, CTRL_PLUS_W  = 60;
constexpr int CTRL_VALUE_X = 470, CTRL_VALUE_W = 240;
constexpr int CTRL_TOGGLE_X = 410, CTRL_TOGGLE_W = 100;
constexpr int CTRL_COLOR_X  = 410, CTRL_COLOR_W  = 130;

// (rowY removed — scroll-aware rowScreenY() defined further down replaces it.)

// Bounds + step for each numeric setting (NumBounds defined at top of file).
// All RPM-stepping is 50 RPM per tap so fine-tuning the alert points is easy
// (140 taps to walk the bar end-to-end is fine; we can add a long-press
// fast-step later if it gets annoying).
static NumBounds numBounds(SettingId id) {
    switch (id) {
        case ST_RPM_MIN: return {    0,  3000,  50 };
        case ST_RPM_MAX: return { 4000, 14000,  50 };
        case ST_A1_RPM:  return { 1000, 13000,  50 };
        case ST_AM_RPM:  return { 1000, 14000,  50 };
        case ST_A1_HZ:   return {    1,    14,   1 };
        case ST_AM_HZ:   return {    1,    15,   1 };
        case ST_CL_PORT: return {    1, 65535,   1 };
        case ST_TEMP_WARN_F:  return { 150, 300, 5 };  // useful overheat range
        case ST_PSI_WARN_PSI: return {   5, 100, 1 };  // low-oil thresholds
        // AFR thresholds are stored as AFR × 10; step 1 = 0.1 AFR per tap.
        // Range 8.0–20.0 spans every realistic operating point on gasoline.
        case ST_AFR_WARN_LO:  return {  80, 200, 1 };
        case ST_AFR_WARN_HI:  return {  80, 200, 1 };
        default:         return {    0,     0,   0 };
    }
}

static uint16_t getNum(SettingId id) {
    switch (id) {
        case ST_RPM_MIN: return s.rpm_min;
        case ST_RPM_MAX: return s.rpm_max;
        case ST_A1_RPM:  return s.alert1_rpm;
        case ST_AM_RPM:  return s.alertmax_rpm;
        case ST_A1_HZ:   return s.alert1_hz;
        case ST_AM_HZ:   return s.alertmax_hz;
        case ST_CL_PORT: return s.cloud_port;
        case ST_TEMP_WARN_F:  return s.coolant_warn_f;
        case ST_PSI_WARN_PSI: return s.oil_warn_psi;
        case ST_AFR_WARN_LO:  return s.afr_warn_lo_x10;
        case ST_AFR_WARN_HI:  return s.afr_warn_hi_x10;
        default:         return 0;
    }
}
static void setNum(SettingId id, uint16_t v) {
    switch (id) {
        case ST_RPM_MIN: s.rpm_min      = v; break;
        case ST_RPM_MAX: s.rpm_max      = v; break;
        case ST_A1_RPM:  s.alert1_rpm   = v; break;
        case ST_AM_RPM:  s.alertmax_rpm = v; break;
        case ST_A1_HZ:   s.alert1_hz    = (uint8_t)v; break;
        case ST_AM_HZ:   s.alertmax_hz  = (uint8_t)v; break;
        case ST_CL_PORT: s.cloud_port   = v; break;
        case ST_TEMP_WARN_F:  s.coolant_warn_f = v; break;
        case ST_PSI_WARN_PSI: s.oil_warn_psi   = v; break;
        case ST_AFR_WARN_LO:  s.afr_warn_lo_x10 = v; break;
        case ST_AFR_WARN_HI:  s.afr_warn_hi_x10 = v; break;
        default: break;
    }
}

// Cloud-port +/- uses bigger steps for usability — 1-by-1 is impractical
// to walk from 80 to 8080. Override the generic step in handleSettingsTap.
constexpr uint16_t CL_PORT_STEP = 10;

// Enforce relational invariants AFTER any single setting change.
//   rpm_min < rpm_max
//   alert1_rpm < alertmax_rpm
//   alert1_hz < alertmax_hz
//   alert1_rpm and alertmax_rpm sit between rpm_min and rpm_max
static void clampInvariants() {
    if (s.rpm_max <= s.rpm_min) s.rpm_max = s.rpm_min + 50;
    if (s.alert1_rpm < s.rpm_min) s.alert1_rpm = s.rpm_min;
    if (s.alert1_rpm > s.rpm_max) s.alert1_rpm = s.rpm_max;
    if (s.alertmax_rpm < s.rpm_min) s.alertmax_rpm = s.rpm_min;
    if (s.alertmax_rpm > s.rpm_max) s.alertmax_rpm = s.rpm_max;
    if (s.alertmax_rpm <= s.alert1_rpm) s.alertmax_rpm = s.alert1_rpm + 50;
    if (s.alertmax_rpm > s.rpm_max)     s.alertmax_rpm = s.rpm_max;
    if (s.alertmax_hz <= s.alert1_hz)   s.alertmax_hz  = s.alert1_hz + 1;
    if (s.alertmax_hz > 15)             s.alertmax_hz  = 15;
    // AFR: low (rich) threshold must be below high (lean) threshold, with at
    // least 0.5 AFR (5 in x10) between them so the "safe" band is meaningful.
    if (s.afr_warn_hi_x10 <= s.afr_warn_lo_x10 + 5)
        s.afr_warn_hi_x10 = s.afr_warn_lo_x10 + 5;
    if (s.afr_warn_hi_x10 > 200) s.afr_warn_hi_x10 = 200;
}

// Settings page is repainted on entry and on every tap that mutates state.
// In between, draw is skipped — same trick that killed the dash flicker.
// (settingsDirty itself is declared above, near the touch handler that sets it.)

// Sticky header/footer bands. Body band (rows) lives between them and
// scrolls vertically when content is taller than the band.
constexpr int HDR_H        = 60;
constexpr int FTR_H        = 30;
constexpr int BODY_TOP     = HDR_H;
constexpr int BODY_BOTTOM  = 480 - FTR_H;
constexpr int BODY_HEIGHT  = BODY_BOTTOM - BODY_TOP;

static void clampSettingsScroll() {
    const int maxScroll = (settingsContentHeight > BODY_HEIGHT)
                            ? (settingsContentHeight - BODY_HEIGHT) : 0;
    if (settingsScrollY < 0)         settingsScrollY = 0;
    if (settingsScrollY > maxScroll) settingsScrollY = maxScroll;
}

static const char* boolValueOnRow(SettingId id) {
    switch (id) {
        case ST_ALERTS:      return s.alerts_enabled    ? "ON" : "OFF";
        case ST_REC_SD:      return s.record_sd         ? "ON" : "OFF";
        case ST_REC_CLOUD:   return s.record_cloud      ? "ON" : "OFF";
        case ST_AUTO_TRACK:  return s.auto_select_track ? "ON" : "OFF";
        case ST_SHOW_TEMP:   return s.show_coolant      ? "ON" : "OFF";
        case ST_SHOW_PSI:    return s.show_oil_psi      ? "ON" : "OFF";
        case ST_SHOW_AFR:    return s.show_afr          ? "ON" : "OFF";
        default:             return "?";
    }
}
static bool boolValueOnState(SettingId id) {
    switch (id) {
        case ST_ALERTS:      return s.alerts_enabled;
        case ST_REC_SD:      return s.record_sd;
        case ST_REC_CLOUD:   return s.record_cloud;
        case ST_AUTO_TRACK:  return s.auto_select_track;
        case ST_SHOW_TEMP:   return s.show_coolant;
        case ST_SHOW_PSI:    return s.show_oil_psi;
        case ST_SHOW_AFR:    return s.show_afr;
        default:             return false;
    }
}
// ---------------------------------------------------------------------------
// WiFi state machine — entire lifecycle of the ESP32-S3 radio. Driven by
// the Internet=WiFi setting. Periodic tick from loop() at 1 Hz; only stays
// up when mode==WiFi and an SSID is set. NTP runs once after successful
// connect and pushes SETTIME,<epoch> to the Teensy (which sets its RTC).
// (WifiState enum is forward-declared up top — see auto-prototyper note.)
// ---------------------------------------------------------------------------
static WifiState wifi_state          = WS_OFF;
static char      wifi_ip[16]         = "";
static uint32_t  wifi_state_ms       = 0;
static bool      wifi_ntp_done       = false;   // set true once we pushed SETTIME
static char      wifi_status_buf[64] = "";       // display string for INFO row

static void formatWifiStatus() {
    switch (wifi_state) {
        case WS_OFF:        snprintf(wifi_status_buf, sizeof(wifi_status_buf),
                                  s.internet_mode == 1 ? "idle (no SSID)" : "disabled");
                              break;
        case WS_CONNECTING: snprintf(wifi_status_buf, sizeof(wifi_status_buf),
                                  "connecting to %s...", s.wifi_ssid);
                              break;
        case WS_CONNECTED:  snprintf(wifi_status_buf, sizeof(wifi_status_buf),
                                  "connected: %s", wifi_ip);
                              break;
        case WS_FAILED:     snprintf(wifi_status_buf, sizeof(wifi_status_buf),
                                  "failed — retry in 30s"); break;
    }
}

static void setWifiState(WifiState st) {
    if (st == wifi_state) return;
    wifi_state    = st;
    wifi_state_ms = millis();
    formatWifiStatus();
    settingsDirty = true;   // INFO row refresh
}

static void wifiKickNtp() {
    if (wifi_ntp_done) return;
    configTime(0, 0, "0.pool.ntp.org", "1.pool.ntp.org", "time.google.com");
}

static void wifiTickNtp() {
    if (wifi_ntp_done) return;
    if (wifi_state != WS_CONNECTED) return;
    const time_t t = time(nullptr);
    if (t > 1700000000) {   // sane (year 2023+)
        Serial.printf("SETTIME,%lu\n", (unsigned long)t);
        rtc_epoch     = (uint32_t)t;
        wifi_ntp_done = true;
        Serial.printf("[wifi-ntp] synced epoch=%lu\n", (unsigned long)t);
    }
}

static void wifiTick() {
    static uint32_t last_tick_ms = 0;
    if (millis() - last_tick_ms < 1000) { wifiTickNtp(); return; }
    last_tick_ms = millis();

    if (s.internet_mode != 1) {
        if (wifi_state != WS_OFF) {
            WiFi.disconnect(true, true);
            WiFi.mode(WIFI_OFF);   // ESP32 wifi_mode_t (radio off), NOT our WS_OFF state
            wifi_ip[0]    = '\0';
            wifi_ntp_done = false;
            setWifiState(WS_OFF);
        }
        return;
    }

    if (s.wifi_ssid[0] == '\0') {
        if (wifi_state != WS_OFF) {
            WiFi.disconnect(true, true);
            wifi_ip[0]    = '\0';
            wifi_ntp_done = false;
            setWifiState(WS_OFF);
        }
        return;
    }

    switch (wifi_state) {
        case WS_OFF: {
            WiFi.mode(WIFI_STA);
            WiFi.begin(s.wifi_ssid, s.wifi_pass);
            setWifiState(WS_CONNECTING);
            break;
        }
        case WS_CONNECTING: {
            if (WiFi.status() == WL_CONNECTED) {
                const IPAddress ip = WiFi.localIP();
                snprintf(wifi_ip, sizeof(wifi_ip), "%d.%d.%d.%d",
                         ip[0], ip[1], ip[2], ip[3]);
                setWifiState(WS_CONNECTED);
                wifiKickNtp();
            } else if (millis() - wifi_state_ms > 20000) {
                WiFi.disconnect(true, false);
                setWifiState(WS_FAILED);
            }
            break;
        }
        case WS_CONNECTED: {
            if (WiFi.status() != WL_CONNECTED) {
                wifi_ip[0]    = '\0';
                wifi_ntp_done = false;
                setWifiState(WS_CONNECTING);
                wifi_state_ms = millis();
            } else {
                wifiTickNtp();
            }
            break;
        }
        case WS_FAILED: {
            if (millis() - wifi_state_ms > 30000) setWifiState(WS_OFF);
            break;
        }
    }
}

// ---------------------------------------------------------------------------
// WiFi scanner — PAGE_WIFI_SCAN. Tap WiFi SSID row in settings to open.
// Lists nearby networks (sorted by RSSI — strongest first), tap one to
// select. Open networks save immediately; secured networks then open the
// password keyboard.
// ---------------------------------------------------------------------------
enum WifiScanState : uint8_t { WSC_IDLE = 0, WSC_RUNNING, WSC_DONE };
struct WifiScanItem {
    char    ssid[33];
    int8_t  rssi;
    bool    secured;
};
static WifiScanItem  wifi_scan_list[16];
static uint8_t       wifi_scan_count = 0;
static WifiScanState wifi_scan_state = WSC_IDLE;
static bool          wifi_scan_dirty = false;

static void startWifiScan() {
    wifi_scan_count = 0;
    wifi_scan_state = WSC_RUNNING;
    wifi_scan_dirty = true;
    WiFi.disconnect(false, false);
    WiFi.mode(WIFI_STA);
    delay(50);
    WiFi.scanNetworks(/*async=*/true, /*show_hidden=*/false);
}

static void openWifiScanner() {
    currentPage     = PAGE_WIFI_SCAN;
    pageJustEntered = true;
    startWifiScan();
}

static void pollWifiScanner() {
    if (wifi_scan_state != WSC_RUNNING) return;
    const int n = WiFi.scanComplete();
    if (n < 0) return;   // -1 = still running, -2 = not started

    wifi_scan_count = (n > 16) ? 16 : (uint8_t)n;
    for (int i = 0; i < wifi_scan_count; i++) {
        WiFi.SSID(i).toCharArray(wifi_scan_list[i].ssid, sizeof(wifi_scan_list[i].ssid));
        wifi_scan_list[i].rssi    = (int8_t)WiFi.RSSI(i);
        wifi_scan_list[i].secured = (WiFi.encryptionType(i) != WIFI_AUTH_OPEN);
    }
    WiFi.scanDelete();
    wifi_scan_state = WSC_DONE;
    wifi_scan_dirty = true;
}

static void wifiForceReconfigure() {
    WiFi.disconnect(true, false);
    wifi_ip[0]    = '\0';
    wifi_ntp_done = false;
    setWifiState(WS_OFF);   // next wifiTick (within 1 s) re-evaluates mode + creds
}

static const char* enumValue(SettingId id) {
    switch (id) {
        case ST_INET_MODE:   return INET_MODE_NAMES[s.internet_mode % N_INET_MODE];
        case ST_CL_PROTO:    return PROTOCOL_NAMES[s.cloud_protocol % N_PROTOCOL];
        case ST_CL_STREAM:   return STREAM_NAMES[s.cloud_stream % N_STREAM];
        case ST_TIMEZONE:    return TIMEZONES[s.timezone_idx % N_TIMEZONES].name;
        case ST_SENSOR_TYPE: return SENSOR_TYPE_NAMES[s.sensor_type % N_SENSOR_TYPE];
        default:             return "?";
    }
}

// Conditionally hide settings rows based on hardware state.
// ST_REC_SD only makes sense when a card is mounted; ST_SD_FORMAT now lives
// the card is present but unformatted. Hiding keeps the list uncluttered
// and prevents tapping controls that have no effect.
static bool rowShouldShow(SettingId id) {
    switch (id) {
        case ST_REC_SD:    return sd_card_status == 2;  // hidden unless card is READY
        // (ST_SD_FORMAT row never appears in the settings list anymore —
        //  the maintenance action moved to PAGE_TOOLS.)
        // WiFi credential + status rows only meaningful when mode=WiFi.
        case ST_WIFI_SSID:
        case ST_WIFI_PASS:
        case ST_WIFI_STATUS: return s.internet_mode == 1;
        default:           return true;
    }
}

// On-screen Y of row i, compacting out any hidden rows above it.
static int rowScreenY(uint8_t i) {
    int vis = 0;
    for (uint8_t j = 0; j < i; ++j)
        if (rowShouldShow(ROWS[j].id)) vis++;
    return BODY_TOP + vis * SETTINGS_ROW_DY - settingsScrollY;
}
// True when the row is at least partially visible in the body band.
static bool rowVisible(int yScreen) {
    return yScreen + SETTINGS_ROW_HEIGHT > BODY_TOP && yScreen < BODY_BOTTOM;
}

static void drawSettingsPage() {
    // Content height depends on how many rows are currently visible.
    {
        int vis = 0;
        for (uint8_t i = 0; i < ST_COUNT; ++i)
            if (rowShouldShow(ROWS[i].id)) vis++;
        settingsContentHeight = vis * SETTINGS_ROW_DY + 10;
    }
    clampSettingsScroll();

    if (pageJustEntered) {
        tft.fillScreen(TFT_BLACK);
        // Sticky header
        tft.setTextDatum(textdatum_t::top_left);
        tft.setFont(&fonts::Font4);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.drawString("S E T T I N G S", 20, 18);
        // Sticky footer
        tft.setFont(&fonts::Font2);
        tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
        tft.drawString("drag to scroll  \xB7  swipe right: dash  \xB7  swipe left: status", 20, 458);
        pageJustEntered = false;
        settingsDirty   = true;
    }

    if (!settingsDirty) return;
    settingsDirty = false;

    // On scroll-position change, wipe ONLY the narrow strips at the top
    // and bottom of the body band — that's where partial rows sliding
    // off-edge leave a hairline of stale pixels. Full-body wipe (the
    // previous version) was 624 KB of PSRAM writes which the LCD scan-out
    // caught mid-render and looked like flashing. Two 64 KB strip wipes
    // are ~1.3 ms each, well under the LCD's 16 ms scan period.
    static int lastDrawnScrollY = -1;
    if (settingsScrollY != lastDrawnScrollY) {
        tft.fillRect(0, BODY_TOP,                       800, SETTINGS_ROW_DY, TFT_BLACK);
        tft.fillRect(0, BODY_BOTTOM - SETTINGS_ROW_DY,  800, SETTINGS_ROW_DY, TFT_BLACK);
        lastDrawnScrollY = settingsScrollY;
    }

    // Per-row redraw — each row paints its own SETTINGS_ROW_DY-tall band.
    tft.setTextSize(1);
    tft.setFont(&fonts::Font4);
    tft.setTextDatum(textdatum_t::top_left);

    for (uint8_t i = 0; i < ST_COUNT; ++i) {
        const SettingRow& r = ROWS[i];
        if (!rowShouldShow(r.id)) continue;
        const int y = rowScreenY(i);
        if (!rowVisible(y)) continue;

        // Clip drawing to the body band so partially-visible rows don't
        // bleed into the sticky header or footer.
        tft.setClipRect(0, BODY_TOP, 800, BODY_HEIGHT);

        // Wipe THIS row's full DY band only.
        tft.fillRect(0, y, 800, SETTINGS_ROW_DY, TFT_BLACK);

        // Label
        tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
        tft.drawString(r.label, 20, y + 6);

        if (r.kind == SettingRow::NUMERIC) {
            // [-]
            tft.fillRect(CTRL_MINUS_X, y, CTRL_MINUS_W, SETTINGS_ROW_HEIGHT, TFT_DARKGREY);
            tft.setTextColor(TFT_WHITE, TFT_DARKGREY);
            tft.setTextDatum(textdatum_t::middle_center);
            tft.drawString("-", CTRL_MINUS_X + CTRL_MINUS_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            // value
            char buf[16]; snprintf(buf, sizeof(buf), "%u", (unsigned)getNum(r.id));
            tft.setTextColor(TFT_WHITE, TFT_BLACK);
            tft.drawString(buf, CTRL_VALUE_X + CTRL_VALUE_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            // [+]
            tft.fillRect(CTRL_PLUS_X, y, CTRL_PLUS_W, SETTINGS_ROW_HEIGHT, TFT_DARKGREY);
            tft.setTextColor(TFT_WHITE, TFT_DARKGREY);
            tft.drawString("+", CTRL_PLUS_X + CTRL_PLUS_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            tft.setTextDatum(textdatum_t::top_left);
        } else if (r.kind == SettingRow::TOGGLE) {
            const bool on = boolValueOnState(r.id);
            tft.fillRect(CTRL_TOGGLE_X, y, CTRL_TOGGLE_W, SETTINGS_ROW_HEIGHT,
                         on ? TFT_DARKGREEN : TFT_DARKGREY);
            tft.setTextColor(TFT_WHITE, on ? TFT_DARKGREEN : TFT_DARKGREY);
            tft.setTextDatum(textdatum_t::middle_center);
            tft.drawString(boolValueOnRow(r.id),
                           CTRL_TOGGLE_X + CTRL_TOGGLE_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            tft.setTextDatum(textdatum_t::top_left);
        } else if (r.kind == SettingRow::COLOR) {
            uint8_t cidx = 0;
            switch (r.id) {
                case ST_A1_COL:        cidx = s.alert1_color_idx;   break;
                case ST_AM_COL:        cidx = s.alertmax_color_idx; break;
                case ST_TEMP_WARN_COL: cidx = s.coolant_warn_col;   break;
                case ST_PSI_WARN_COL:  cidx = s.oil_warn_col;       break;
                case ST_AFR_WARN_COL:  cidx = s.afr_warn_col;       break;
                default: break;
            }
            tft.fillRect(CTRL_COLOR_X, y, CTRL_COLOR_W, SETTINGS_ROW_HEIGHT, PALETTE[cidx]);
            tft.drawRect(CTRL_COLOR_X, y, CTRL_COLOR_W, SETTINGS_ROW_HEIGHT, TFT_WHITE);
            tft.setTextColor(TFT_BLACK, PALETTE[cidx]);
            tft.setTextDatum(textdatum_t::middle_center);
            tft.drawString(PALETTE_NAMES[cidx],
                           CTRL_COLOR_X + CTRL_COLOR_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            tft.setTextDatum(textdatum_t::top_left);
        } else if (r.kind == SettingRow::ENUM) {
            // Tap-to-cycle pill: blue background with the current option text.
            constexpr int ENUM_X = 410, ENUM_W = 360;
            tft.fillRect(ENUM_X, y, ENUM_W, SETTINGS_ROW_HEIGHT, TFT_NAVY);
            tft.drawRect(ENUM_X, y, ENUM_W, SETTINGS_ROW_HEIGHT, TFT_WHITE);
            tft.setTextColor(TFT_WHITE, TFT_NAVY);
            tft.setTextDatum(textdatum_t::middle_center);
            tft.drawString(enumValue(r.id), ENUM_X + ENUM_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            tft.setTextDatum(textdatum_t::top_left);
        } else if (r.kind == SettingRow::ACTION) {
            constexpr int ACT_X = 410, ACT_W = 360;
            tft.setTextDatum(textdatum_t::middle_center);
            if (r.id == ST_SET_TIME) {
                tft.fillRect(ACT_X, y, ACT_W, SETTINGS_ROW_HEIGHT, TFT_NAVY);
                tft.drawRect(ACT_X, y, ACT_W, SETTINGS_ROW_HEIGHT, TFT_WHITE);
                tft.setTextColor(TFT_WHITE, TFT_NAVY);
                char buf[28];
                if (rtc_epoch > 0) {
                    const TimeZone& tz  = TIMEZONES[s.timezone_idx % N_TIMEZONES];
                    const time_t    loc = utcToLocal((time_t)rtc_epoch, tz);
                    struct tm*      tmv = gmtime(&loc);
                    snprintf(buf, sizeof(buf), "%04d-%02d-%02d  %02d:%02d %s",
                             tmv->tm_year + 1900, tmv->tm_mon + 1, tmv->tm_mday,
                             tmv->tm_hour, tmv->tm_min,
                             tzAbbrevFor((time_t)rtc_epoch, tz));
                } else {
                    strncpy(buf, "-- not set --", sizeof(buf));
                }
                tft.drawString(buf, ACT_X + ACT_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            }
            tft.setTextDatum(textdatum_t::top_left);
        } else if (r.kind == SettingRow::INFO) {
            // Display-only row — WiFi status today. Right-aligned status string,
            // no border, slightly dimmer text to read as a label not a control.
            constexpr int INFO_X = 410, INFO_W = 360;
            tft.fillRect(INFO_X, y, INFO_W, SETTINGS_ROW_HEIGHT, TFT_BLACK);
            tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
            tft.setTextDatum(textdatum_t::middle_left);
            const char* shown = "";
            if (r.id == ST_WIFI_STATUS) shown = wifi_status_buf[0] ? wifi_status_buf : "-";
            tft.drawString(shown, INFO_X + 10, y + SETTINGS_ROW_HEIGHT / 2);
            tft.setTextDatum(textdatum_t::top_left);
        } else { // TEXT — tap-to-edit field. Used for cloud_host (string,
                 // opens text keyboard) and cloud_port (number, opens
                 // numeric keypad).
            constexpr int TEXT_X = 410, TEXT_W = 360;
            tft.fillRect(TEXT_X, y, TEXT_W, SETTINGS_ROW_HEIGHT, TFT_BLACK);
            tft.drawRect(TEXT_X, y, TEXT_W, SETTINGS_ROW_HEIGHT, TFT_DARKGREY);
            char portBuf[8];
            char passMask[24];
            const char* shown = "?";
            if (r.id == ST_CL_HOST) {
                shown = (s.cloud_host[0] == '\0') ? "<tap to set>" : s.cloud_host;
            } else if (r.id == ST_CL_PORT) {
                snprintf(portBuf, sizeof(portBuf), "%u", (unsigned)s.cloud_port);
                shown = portBuf;
            } else if (r.id == ST_CL_AUTH_USER) {
                shown = (s.cloud_auth_user[0] == '\0') ? "<tap to set>" : s.cloud_auth_user;
            } else if (r.id == ST_CL_AUTH_PASS) {
                if (s.cloud_auth_pass[0] == '\0') {
                    shown = "<tap to set>";
                } else {
                    // Mask: render '*' per char, capped at array size.
                    const int n = strlen(s.cloud_auth_pass);
                    const int m = (n > (int)sizeof(passMask) - 1) ? (int)sizeof(passMask) - 1 : n;
                    for (int k = 0; k < m; ++k) passMask[k] = '*';
                    passMask[m] = '\0';
                    shown = passMask;
                }
            } else if (r.id == ST_WIFI_SSID) {
                shown = (s.wifi_ssid[0] == '\0') ? "<tap to set>" : s.wifi_ssid;
            } else if (r.id == ST_WIFI_PASS) {
                if (s.wifi_pass[0] == '\0') {
                    shown = "<tap to set>";
                } else {
                    const int n = strlen(s.wifi_pass);
                    const int m = (n > (int)sizeof(passMask) - 1) ? (int)sizeof(passMask) - 1 : n;
                    for (int k = 0; k < m; ++k) passMask[k] = '*';
                    passMask[m] = '\0';
                    shown = passMask;
                }
            }
            tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
            tft.setTextDatum(textdatum_t::middle_left);
            tft.drawString(shown, TEXT_X + 10, y + SETTINGS_ROW_HEIGHT / 2);
            tft.setTextDatum(textdatum_t::top_left);
        }

        tft.clearClipRect();
    }
}

static bool inRect(int x, int y, int rx, int ry, int rw, int rh) {
    return x >= rx && x < rx + rw && y >= ry && y < ry + rh;
}

static void handleSettingsTap(int x, int y) {
    // Body band is the only tap-able zone (sticky header/footer ignore taps).
    if (y < BODY_TOP || y >= BODY_BOTTOM) return;

    for (uint8_t i = 0; i < ST_COUNT; ++i) {
        const SettingRow& r = ROWS[i];
        if (!rowShouldShow(r.id)) continue;
        const int ry = rowScreenY(i);
        if (!rowVisible(ry)) continue;

        if (r.kind == SettingRow::NUMERIC) {
            const NumBounds nb = numBounds(r.id);
            const uint16_t step = (r.id == ST_CL_PORT) ? CL_PORT_STEP : nb.step;
            if (inRect(x, y, CTRL_MINUS_X, ry, CTRL_MINUS_W, SETTINGS_ROW_HEIGHT)) {
                uint16_t v = getNum(r.id);
                v = (v > nb.lo + step) ? (v - step) : nb.lo;
                setNum(r.id, v); clampInvariants();
                settingsDirty = true;
                return;
            }
            if (inRect(x, y, CTRL_PLUS_X, ry, CTRL_PLUS_W, SETTINGS_ROW_HEIGHT)) {
                uint16_t v = getNum(r.id);
                v = (v + step <= nb.hi) ? (v + step) : nb.hi;
                setNum(r.id, v); clampInvariants();
                settingsDirty = true;
                return;
            }
        } else if (r.kind == SettingRow::TOGGLE) {
            if (inRect(x, y, CTRL_TOGGLE_X, ry, CTRL_TOGGLE_W, SETTINGS_ROW_HEIGHT)) {
                switch (r.id) {
                    case ST_ALERTS:     s.alerts_enabled    = !s.alerts_enabled;    break;
                    case ST_REC_SD:     s.record_sd         = !s.record_sd;         break;
                    case ST_REC_CLOUD:  s.record_cloud      = !s.record_cloud;      break;
                    case ST_AUTO_TRACK: s.auto_select_track = !s.auto_select_track; break;
                    case ST_SHOW_TEMP:  s.show_coolant      = !s.show_coolant;      break;
                    case ST_SHOW_PSI:   s.show_oil_psi      = !s.show_oil_psi;      break;
                    case ST_SHOW_AFR:   s.show_afr          = !s.show_afr;          break;
                    default: break;
                }
                settingsDirty = true;
                return;
            }
        } else if (r.kind == SettingRow::COLOR) {
            if (inRect(x, y, CTRL_COLOR_X, ry, CTRL_COLOR_W, SETTINGS_ROW_HEIGHT)) {
                switch (r.id) {
                    case ST_A1_COL:        s.alert1_color_idx   = (s.alert1_color_idx   + 1) % N_PALETTE; break;
                    case ST_AM_COL:        s.alertmax_color_idx = (s.alertmax_color_idx + 1) % N_PALETTE; break;
                    case ST_TEMP_WARN_COL: s.coolant_warn_col   = (s.coolant_warn_col   + 1) % N_PALETTE; break;
                    case ST_PSI_WARN_COL:  s.oil_warn_col       = (s.oil_warn_col       + 1) % N_PALETTE; break;
                    case ST_AFR_WARN_COL:  s.afr_warn_col       = (s.afr_warn_col       + 1) % N_PALETTE; break;
                    default: break;
                }
                settingsDirty = true;
                return;
            }
        } else if (r.kind == SettingRow::ENUM) {
            constexpr int ENUM_X = 410, ENUM_W = 360;
            if (inRect(x, y, ENUM_X, ry, ENUM_W, SETTINGS_ROW_HEIGHT)) {
                if (r.id == ST_CL_PROTO) {
                    s.cloud_protocol = (s.cloud_protocol + 1) % N_PROTOCOL;
                } else if (r.id == ST_CL_STREAM) {
                    s.cloud_stream   = (s.cloud_stream   + 1) % N_STREAM;
                } else if (r.id == ST_TIMEZONE) {
                    s.timezone_idx = (s.timezone_idx + 1) % N_TIMEZONES;
                    // Notify Teensy so it has the active TZ for future use
                    // (SD filenames, cloud metadata). Display still uses UTC
                    // from the Teensy and we apply the offset locally.
                    Serial.printf("TZ,%s\n", TIMEZONES[s.timezone_idx].id);
                } else if (r.id == ST_INET_MODE) {
                    s.internet_mode = (s.internet_mode + 1) % N_INET_MODE;
                    wifiForceReconfigure();
                    Serial.printf("CFG,inet,%u\n", (unsigned)s.internet_mode);
                } else if (r.id == ST_SENSOR_TYPE) {
                    s.sensor_type = (s.sensor_type + 1) % N_SENSOR_TYPE;
                    // Reset stale ECU state on a source flip so the dash
                    // doesn't show stale CAN data right after switching to
                    // MegaSquirt, or stale direct data after switching back.
                    ecu = EcuState{};
                }
                settingsDirty = true;
                return;
            }
        } else if (r.kind == SettingRow::ACTION) {
            constexpr int ACT_X = 410, ACT_W = 360;
            if (inRect(x, y, ACT_X, ry, ACT_W, SETTINGS_ROW_HEIGHT)) {
                if (r.id == ST_SET_TIME) {
                    // Pre-fill time-set page with current LOCAL time (active TZ)
                    // so the user is editing what they see on the dash.
                    if (rtc_epoch > 0) {
                        const TimeZone& tz  = TIMEZONES[s.timezone_idx % N_TIMEZONES];
                        const time_t    loc = utcToLocal((time_t)rtc_epoch, tz);
                        struct tm*      tmv = gmtime(&loc);
                        ts_year  = tmv->tm_year + 1900;
                        ts_month = tmv->tm_mon + 1;
                        ts_day   = tmv->tm_mday;
                        ts_hour  = tmv->tm_hour;
                        ts_min   = tmv->tm_min;
                        ts_sec   = tmv->tm_sec;
                    } else {
                        ts_year = 2025; ts_month = 1; ts_day = 1;
                        ts_hour = 0;    ts_min   = 0; ts_sec = 0;
                    }
                    currentPage     = PAGE_TIME_SET;
                    pageJustEntered = true;
                    return;
                }
                // (Check for updates + Format SD have moved to PAGE_TOOLS.
                //  Tap dispatch for those lives in handleToolsTap.)
            }
        } else { // TEXT — open the appropriate on-screen keyboard.
            constexpr int TEXT_X = 410, TEXT_W = 360;
            if (inRect(x, y, TEXT_X, ry, TEXT_W, SETTINGS_ROW_HEIGHT)) {
                if (r.id == ST_CL_PORT)              openNumericKeyboard(r.id);
                else if (r.id == ST_WIFI_SSID)       openWifiScanner();
                else if (r.id == ST_WIFI_PASS)       openTextKeyboard(r.id);
                else if (r.id == ST_CL_HOST    ||
                         r.id == ST_CL_AUTH_USER ||
                         r.id == ST_CL_AUTH_PASS)    openTextKeyboard(r.id);
                return;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Popup keyboards (numeric + text). Shared edit buffer; differ only in
// which key array gets rendered and tapped.
// ---------------------------------------------------------------------------

// Action code constants — distinguish from printable ASCII.
constexpr char K_SHIFT= 0x07;
constexpr char K_BACK = 0x01;
constexpr char K_CLR  = 0x02;
constexpr char K_DONE = 0x03;
constexpr char K_CXL  = 0x04;
constexpr char K_SPC  = ' ';

// Numeric keypad: 3 cols × 4 key rows + action row.
static const KbKey NUM_KEYS[] = {
    { 70, 110, 220, 60, "1", '1' }, { 298, 110, 220, 60, "2", '2' }, { 526, 110, 220, 60, "3", '3' },
    { 70, 178, 220, 60, "4", '4' }, { 298, 178, 220, 60, "5", '5' }, { 526, 178, 220, 60, "6", '6' },
    { 70, 246, 220, 60, "7", '7' }, { 298, 246, 220, 60, "8", '8' }, { 526, 246, 220, 60, "9", '9' },
    { 70, 314, 220, 60, "CLR",  K_CLR  }, { 298, 314, 220, 60, "0", '0' }, { 526, 314, 220, 60, "BACK", K_BACK },
    // Action row
    { 70, 392, 354, 60, "DONE",   K_DONE }, { 432, 392, 314, 60, "CANCEL", K_CXL },
};
constexpr int N_NUM_KEYS = sizeof(NUM_KEYS) / sizeof(NUM_KEYS[0]);

// Text keyboard: 10 cols × 4 letter/digit rows + action row. Tight at 75x55
// per key for finger-tap.
static const KbKey TEXT_KEYS[] = {
    // Row 1: digits
    { 10,  100, 75, 55, "1", '1' }, { 89,  100, 75, 55, "2", '2' }, { 168, 100, 75, 55, "3", '3' },
    { 247, 100, 75, 55, "4", '4' }, { 326, 100, 75, 55, "5", '5' }, { 405, 100, 75, 55, "6", '6' },
    { 484, 100, 75, 55, "7", '7' }, { 563, 100, 75, 55, "8", '8' }, { 642, 100, 75, 55, "9", '9' },
    { 721, 100, 75, 55, "0", '0' },
    // Row 2: q-p
    { 10,  160, 75, 55, "q", 'q' }, { 89,  160, 75, 55, "w", 'w' }, { 168, 160, 75, 55, "e", 'e' },
    { 247, 160, 75, 55, "r", 'r' }, { 326, 160, 75, 55, "t", 't' }, { 405, 160, 75, 55, "y", 'y' },
    { 484, 160, 75, 55, "u", 'u' }, { 563, 160, 75, 55, "i", 'i' }, { 642, 160, 75, 55, "o", 'o' },
    { 721, 160, 75, 55, "p", 'p' },
    // Row 3: a-l + dot
    { 10,  220, 75, 55, "a", 'a' }, { 89,  220, 75, 55, "s", 's' }, { 168, 220, 75, 55, "d", 'd' },
    { 247, 220, 75, 55, "f", 'f' }, { 326, 220, 75, 55, "g", 'g' }, { 405, 220, 75, 55, "h", 'h' },
    { 484, 220, 75, 55, "j", 'j' }, { 563, 220, 75, 55, "k", 'k' }, { 642, 220, 75, 55, "l", 'l' },
    { 721, 220, 75, 55, ".", '.' },
    // Row 4: z-m + special chars. '/' dropped in favour of '@' — cloud host
    // is always a bare DNS name (no paths) and emails need '@'. SHIFT key
    // remaps these to capital letters and base64-ish symbols (see shiftedChar).
    { 10,  280, 75, 55, "z", 'z' }, { 89,  280, 75, 55, "x", 'x' }, { 168, 280, 75, 55, "c", 'c' },
    { 247, 280, 75, 55, "v", 'v' }, { 326, 280, 75, 55, "b", 'b' }, { 405, 280, 75, 55, "n", 'n' },
    { 484, 280, 75, 55, "m", 'm' }, { 563, 280, 75, 55, "-", '-' }, { 642, 280, 75, 55, "_", '_' },
    { 721, 280, 75, 55, "@", '@' },
    // Action row: SHIFT BACK SPACE DONE CANCEL (5 buttons; SHIFT toggles caps).
    { 10,  350, 100, 60, "SHIFT",  K_SHIFT},
    { 115, 350, 130, 60, "BACK",   K_BACK },
    { 250, 350, 170, 60, "SPACE",  K_SPC  },
    { 425, 350, 175, 60, "DONE",   K_DONE },
    { 605, 350, 185, 60, "CANCEL", K_CXL  },
};

// Apply QWERTY-style shift mapping. Returns the unshifted char unchanged for
// anything we don't have a shifted variant for (incl. action codes).
static char shiftedChar(char c) {
    if (c >= 'a' && c <= 'z') return (char)(c - 32);
    switch (c) {
        case '1': return '!';  case '2': return '@';  case '3': return '#';
        case '4': return '$';  case '5': return '%';  case '6': return '^';
        case '7': return '&';  case '8': return '*';  case '9': return '(';
        case '0': return ')';
        case '.': return ':';
        case '-': return '+';
        case '_': return '=';
        case '@': return '/';   // gives us '/' back — useful for std base64 keys
    }
    return c;
}
constexpr int N_TEXT_KEYS = sizeof(TEXT_KEYS) / sizeof(TEXT_KEYS[0]);

static void openNumericKeyboard(SettingId target) {
    kb.target = target;
    snprintf(kb.editBuf, sizeof(kb.editBuf), "%u", (unsigned)getNum(target));
    kb.editLen = strlen(kb.editBuf);
    kb.dirty   = true;
    currentPage = PAGE_NUM_KB;
    pageJustEntered = true;
}

static void openTextKeyboard(SettingId target) {
    kb.target = target;
    const char* src = "";
    switch (target) {
        case ST_CL_HOST:      src = s.cloud_host;      break;
        case ST_CL_AUTH_USER: src = s.cloud_auth_user; break;
        case ST_CL_AUTH_PASS: src = s.cloud_auth_pass; break;
        case ST_WIFI_SSID:    src = s.wifi_ssid;       break;
        case ST_WIFI_PASS:    src = s.wifi_pass;       break;
        default:              src = "";                break;
    }
    strncpy(kb.editBuf, src, sizeof(kb.editBuf) - 1);
    kb.editBuf[sizeof(kb.editBuf) - 1] = '\0';
    kb.editLen = strlen(kb.editBuf);
    kb.dirty   = true;
    currentPage = PAGE_TEXT_KB;
    pageJustEntered = true;
}

static void closeKeyboard(bool commit) {
    if (commit) {
        switch (kb.target) {
            case ST_CL_PORT: {
                long v = atol(kb.editBuf);
                if (v < 1)     v = 1;
                if (v > 65535) v = 65535;
                s.cloud_port = (uint16_t)v;
                break;
            }
            case ST_CL_HOST:
                strncpy(s.cloud_host, kb.editBuf, sizeof(s.cloud_host) - 1);
                s.cloud_host[sizeof(s.cloud_host) - 1] = '\0';
                break;
            case ST_CL_AUTH_USER:
                strncpy(s.cloud_auth_user, kb.editBuf, sizeof(s.cloud_auth_user) - 1);
                s.cloud_auth_user[sizeof(s.cloud_auth_user) - 1] = '\0';
                break;
            case ST_CL_AUTH_PASS:
                strncpy(s.cloud_auth_pass, kb.editBuf, sizeof(s.cloud_auth_pass) - 1);
                s.cloud_auth_pass[sizeof(s.cloud_auth_pass) - 1] = '\0';
                break;
            case ST_WIFI_SSID:
                strncpy(s.wifi_ssid, kb.editBuf, sizeof(s.wifi_ssid) - 1);
                s.wifi_ssid[sizeof(s.wifi_ssid) - 1] = '\0';
                wifiForceReconfigure();
                break;
            case ST_WIFI_PASS:
                strncpy(s.wifi_pass, kb.editBuf, sizeof(s.wifi_pass) - 1);
                s.wifi_pass[sizeof(s.wifi_pass) - 1] = '\0';
                wifiForceReconfigure();
                break;
            default: break;
        }
    }
    currentPage = PAGE_SETTINGS;
    pageJustEntered = true;
    settingsDirty   = true;
}

// Render a single key. Honours kb.shift: shows the shifted label/colour for
// printable keys, and a brighter fill for the SHIFT button itself when active.
static void drawKey(const KbKey& k) {
    const bool isAction = (k.action == K_DONE || k.action == K_CXL ||
                           k.action == K_BACK || k.action == K_CLR  ||
                           k.action == K_SPC  || k.action == K_SHIFT);
    const bool isShiftKey = (k.action == K_SHIFT);
    // SHIFT key glows blue when active so the state is unambiguous.
    const uint16_t fill = isShiftKey && kb.shift ? TFT_BLUE
                        : isAction               ? TFT_DARKGREY
                                                 : TFT_NAVY;
    const uint16_t txtC = TFT_WHITE;
    tft.fillRect(k.x, k.y, k.w, k.h, fill);
    tft.drawRect(k.x, k.y, k.w, k.h, TFT_LIGHTGREY);
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextColor(txtC, fill);
    tft.setTextDatum(textdatum_t::middle_center);

    // For printable keys, show the shifted glyph when shift is on.
    char buf[2] = { 0, 0 };
    const char* label = k.label;
    if (kb.shift && !isAction && k.action > 0x20) {
        buf[0] = shiftedChar(k.action);
        label  = buf;
    }
    tft.drawString(label, k.x + k.w / 2, k.y + k.h / 2);
    tft.setTextDatum(textdatum_t::top_left);
}

static void drawKeyboardChrome(const char* title) {
    tft.fillScreen(TFT_BLACK);
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(textdatum_t::top_left);
    tft.drawString(title, 10, 10);
}

static void drawEditField() {
    // Display the current edit buffer with a thin frame at the top.
    constexpr int FY = 50, FH = 40;
    tft.fillRect(10, FY, 780, FH, TFT_BLACK);
    tft.drawRect(10, FY, 780, FH, TFT_LIGHTGREY);
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(textdatum_t::middle_left);
    const char* shown = (kb.editLen == 0) ? " " : kb.editBuf;
    tft.drawString(shown, 20, FY + FH / 2);
    tft.setTextDatum(textdatum_t::top_left);
}

static const char* kbTitleFor(SettingId id) {
    switch (id) {
        case ST_CL_HOST:      return "Edit Cloud host (DNS or IP)";
        case ST_CL_PORT:      return "Edit Cloud port";
        case ST_CL_AUTH_USER: return "Edit user email (data tag, not auth)";
        case ST_CL_AUTH_PASS: return "Edit API key (sent as X-API-Key)";
        case ST_WIFI_SSID:    return "Edit WiFi network name";
        case ST_WIFI_PASS:    return "Edit WiFi password";
        default:              return "Edit value";
    }
}

static void drawNumKeyboard() {
    if (pageJustEntered) {
        drawKeyboardChrome(kbTitleFor(kb.target));
        for (int i = 0; i < N_NUM_KEYS; ++i) drawKey(NUM_KEYS[i]);
        pageJustEntered = false;
        kb.dirty = true;
    }
    if (kb.dirty) {
        drawEditField();
        kb.dirty = false;
    }
}

static void drawTextKeyboard() {
    if (pageJustEntered) {
        drawKeyboardChrome(kbTitleFor(kb.target));
        kb.shift      = false;
        kb.keys_dirty = true;
        pageJustEntered = false;
        kb.dirty = true;
    }
    if (kb.keys_dirty) {
        for (int i = 0; i < N_TEXT_KEYS; ++i) drawKey(TEXT_KEYS[i]);
        kb.keys_dirty = false;
    }
    if (kb.dirty) {
        drawEditField();
        kb.dirty = false;
    }
}

// Apply a key action to the edit buffer. Returns false if the keyboard
// should close (DONE/CANCEL).
static bool applyKey(char action, bool& commit) {
    if (action == K_DONE)  { commit = true;  return false; }
    if (action == K_CXL)   { commit = false; return false; }
    if (action == K_BACK) {
        if (kb.editLen > 0) { kb.editLen--; kb.editBuf[kb.editLen] = '\0'; }
        kb.dirty = true;
        return true;
    }
    if (action == K_CLR) {
        kb.editLen = 0; kb.editBuf[0] = '\0';
        kb.dirty = true;
        return true;
    }
    if (action == K_SHIFT) {
        kb.shift      = !kb.shift;
        kb.keys_dirty = true;
        kb.dirty      = true;   // ensure drawTextKeyboard runs the redraw branch
        return true;
    }
    // Insert printable char (space included). Apply shift if active.
    const char ch = kb.shift ? shiftedChar(action) : action;
    if (kb.editLen < sizeof(kb.editBuf) - 1) {
        kb.editBuf[kb.editLen++] = ch;
        kb.editBuf[kb.editLen]   = '\0';
        kb.dirty = true;
    }
    return true;
}

static void handleKeyboardTap(int x, int y) {
    const KbKey* keys = (currentPage == PAGE_NUM_KB) ? NUM_KEYS : TEXT_KEYS;
    const int    n    = (currentPage == PAGE_NUM_KB) ? N_NUM_KEYS : N_TEXT_KEYS;
    for (int i = 0; i < n; ++i) {
        const KbKey& k = keys[i];
        if (x >= k.x && x < k.x + k.w && y >= k.y && y < k.y + k.h) {
            bool commit = false;
            if (!applyKey(k.action, commit)) {
                closeKeyboard(commit);
            }
            return;
        }
    }
}

// ---------------------------------------------------------------------------
// Track picker — modal page that opens when the START button is tapped.
// (Data declarations moved up next to the TRACKS table so handleTouch can
// reference tp without forward-decl gymnastics.)
// ---------------------------------------------------------------------------

// Re-build the display order: closest track (if any) on top, then all
// tracks alphabetical. Always finishes with a synthetic UNKNOWN entry so
// the user can record without picking a real track.
static void buildTrackOrder() {
    const int closest = closestTrackIdx();
    uint8_t pos = 0;
    if (closest >= 0) tp.order[pos++] = (uint8_t)closest;
    for (uint8_t i = 0; i < N_TRACKS; ++i) {
        if ((int)i == closest) continue;
        tp.order[pos++] = i;
    }
    tp.order[pos++] = TP_UNKNOWN_IDX;
    tp.count = pos;
}

static void openTrackPicker(bool for_recording) {
    buildTrackOrder();
    tp.selected      = (tp.count > 0) ? 0 : -1;
    tp.scrollY       = 0;
    tp.dirty         = true;
    tp.for_recording = for_recording;
    currentPage      = PAGE_TRACK_PICKER;
    pageJustEntered  = true;
}

static void clampPickerScroll() {
    const int contentH = tp.count * TP_ROW_DY + 6;
    const int maxScroll = (contentH > TP_BODY_HEIGHT) ? (contentH - TP_BODY_HEIGHT) : 0;
    if (tp.scrollY < 0)         tp.scrollY = 0;
    if (tp.scrollY > maxScroll) tp.scrollY = maxScroll;
}
static int  pickerRowY(uint8_t i) { return TP_BODY_TOP + i * TP_ROW_DY - tp.scrollY; }
static bool pickerRowVisible(int yScreen) {
    return yScreen + TP_ROW_HEIGHT > TP_BODY_TOP && yScreen < TP_BODY_BOTTOM;
}

// Confirm track selection. If tp.for_recording, also sends REC,1.
static void confirmTrackAndStart() {
    if (tp.selected >= 0 && tp.selected < (int)tp.count) {
        const uint8_t idx = tp.order[tp.selected];
        if (idx != TP_UNKNOWN_IDX && TRACKS[idx].n_configs > 0) {
            // Delegate to config picker, forwarding the recording intent.
            openConfigPicker((int)idx, false, tp.for_recording);
            return;
        }
    }
    const char* trackName = "UNKNOWN";
    if (tp.selected >= 0 && tp.selected < (int)tp.count) {
        const uint8_t idx = tp.order[tp.selected];
        if (idx != TP_UNKNOWN_IDX) {
            trackName = TRACKS[idx].name;
            saveLastTrack((int)idx, trackName);
        }
    }
    Serial.printf("TRACK,%s\n", trackName);
    if (tp.for_recording) {
        Serial.printf("REC,1\n");
        recording = true; rec_start_ms = millis();
    }
    currentPage = PAGE_DASH;
    pageJustEntered = true;
}

static void cancelTrackPicker() {
    currentPage = PAGE_DASH;
    pageJustEntered = true;
}

static void drawTrackPicker() {
    clampPickerScroll();

    if (pageJustEntered) {
        tft.fillScreen(TFT_BLACK);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.setTextDatum(textdatum_t::top_left);
        tft.drawString("Select Track", 20, 18);

        // Footer (sticky): CANCEL on the left, CONFIRM on the right.
        // Drawn by drawPickerFooter() so it can refresh when selection
        // changes (CONFIRM enables once a row is selected).
        pageJustEntered = false;
        tp.dirty = true;
    }

    if (!tp.dirty) return;
    tp.dirty = false;

    // Same trick as settings: narrow strip wipes at body edges instead of
    // full-body wipe (which caused visible flashing). Only on scroll change.
    static int lastPickerScrollY = -1;
    if (tp.scrollY != lastPickerScrollY) {
        tft.fillRect(0, TP_BODY_TOP,                  800, TP_ROW_DY, TFT_BLACK);
        tft.fillRect(0, TP_BODY_BOTTOM - TP_ROW_DY,   800, TP_ROW_DY, TFT_BLACK);
        lastPickerScrollY = tp.scrollY;
    }

    // Body — per-row redraw (same anti-flicker pattern as settings).
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::top_left);

    for (uint8_t i = 0; i < tp.count; ++i) {
        const int y = pickerRowY(i);
        if (!pickerRowVisible(y)) continue;

        tft.setClipRect(0, TP_BODY_TOP, 800, TP_BODY_HEIGHT);
        tft.fillRect(0, y, 800, TP_ROW_DY, TFT_BLACK);

        const bool isSelected = ((int)i == tp.selected);
        const uint8_t idx = tp.order[i];

        if (isSelected) {
            tft.fillRect(10, y, 780, TP_ROW_HEIGHT, TFT_DARKGREEN);
        }
        tft.drawRect(10, y, 780, TP_ROW_HEIGHT, TFT_DARKGREY);

        // Label
        tft.setTextColor(TFT_WHITE, isSelected ? TFT_DARKGREEN : TFT_BLACK);
        if (idx == TP_UNKNOWN_IDX) {
            tft.drawString("(no track / unknown)", 20, y + 6);
        } else {
            tft.drawString(TRACKS[idx].name, 20, y + 6);

            // First entry = closest match — show distance label on the right.
            if (i == 0 && closestTrackIdx() >= 0) {
                const float km = trackDistanceKm(g.lat_deg, g.lon_deg,
                                                 TRACKS[idx].lat, TRACKS[idx].lon);
                char dbuf[32];
                snprintf(dbuf, sizeof(dbuf), "closest \xB7 %.1f km", km);
                tft.setTextColor(TFT_LIGHTGREY, isSelected ? TFT_DARKGREEN : TFT_BLACK);
                tft.setTextDatum(textdatum_t::top_right);
                tft.drawString(dbuf, 780, y + 8);
                tft.setTextDatum(textdatum_t::top_left);
            }
        }

        tft.clearClipRect();
    }

    // Footer
    tft.fillRect(0, TP_BODY_BOTTOM, 800, TP_FTR_H, TFT_BLACK);
    tft.fillRect(20,  TP_BODY_BOTTOM + 10, 360, 50, TFT_DARKGREY);
    tft.drawRect(20,  TP_BODY_BOTTOM + 10, 360, 50, TFT_LIGHTGREY);
    tft.fillRect(420, TP_BODY_BOTTOM + 10, 360, 50,
                 (tp.selected >= 0) ? TFT_DARKGREEN : TFT_DARKGREY);
    tft.drawRect(420, TP_BODY_BOTTOM + 10, 360, 50, TFT_LIGHTGREY);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, TFT_DARKGREY);
    tft.drawString("CANCEL", 200, TP_BODY_BOTTOM + 35);
    tft.setTextColor(TFT_WHITE, (tp.selected >= 0) ? TFT_DARKGREEN : TFT_DARKGREY);
    tft.drawString("CONFIRM", 600, TP_BODY_BOTTOM + 35);
    tft.setTextDatum(textdatum_t::top_left);
}

// Tap classification: row click selects it; bottom CANCEL/CONFIRM act on
// release.
static void handleTrackPickerTap(int x, int y) {
    // Body — row hit-test
    if (y >= TP_BODY_TOP && y < TP_BODY_BOTTOM) {
        for (uint8_t i = 0; i < tp.count; ++i) {
            const int ry = pickerRowY(i);
            if (!pickerRowVisible(ry)) continue;
            if (x >= 10 && x <= 790 && y >= ry && y < ry + TP_ROW_HEIGHT) {
                tp.selected = (int8_t)i;
                tp.dirty = true;
                return;
            }
        }
    }
    // Footer: CANCEL / CONFIRM
    const int fy = TP_BODY_BOTTOM + 10;
    if (y >= fy && y < fy + 50) {
        if (x >= 20  && x < 380) { cancelTrackPicker(); return; }
        if (x >= 420 && x < 780 && tp.selected >= 0) { confirmTrackAndStart(); return; }
    }
}

// ---------------------------------------------------------------------------
// Config picker — appears when a track has multiple layouts sharing one S/F.
// Tap-only (no scroll); max configs in DB is 3 so all fit on screen at once.
// ---------------------------------------------------------------------------
constexpr int CFG_BTN_H   = 80;
constexpr int CFG_BTN_GAP = 24;
constexpr int CFG_BTN_X   = 50;
constexpr int CFG_BTN_W   = 700;

static int cfgBtnY(int n_total, int i) {
    const int total_h = n_total * CFG_BTN_H + (n_total - 1) * CFG_BTN_GAP;
    const int start_y = 70 + (370 - total_h) / 2;   // centred in body y=70..440
    return start_y + i * (CFG_BTN_H + CFG_BTN_GAP);
}

static void openConfigPicker(int track_idx, bool from_auto, bool for_recording) {
    cp.track_idx     = track_idx;
    cp.selected      = 0;
    cp.dirty         = true;
    cp.from_auto     = from_auto;
    cp.for_recording = for_recording;
    currentPage      = PAGE_CONFIG_PICKER;
    pageJustEntered  = true;
}

static void confirmConfigAndStart() {
    const TrackInfo& t = TRACKS[cp.track_idx];
    char trackName[48];
    if (cp.selected >= 0 && cp.selected < (int)t.n_configs) {
        snprintf(trackName, sizeof(trackName), "%s %s",
                 t.name, t.configs[cp.selected].name);
    } else {
        strncpy(trackName, t.name, sizeof(trackName) - 1);
        trackName[sizeof(trackName) - 1] = '\0';
    }
    saveLastTrack(cp.track_idx, trackName);
    Serial.printf("TRACK,%s\n", trackName);
    if (cp.for_recording) {
        Serial.printf("REC,1\n");
        recording = true; rec_start_ms = millis();
    }
    currentPage = PAGE_DASH;
    pageJustEntered = true;
}

static void cancelConfigPicker() {
    if (cp.from_auto) {
        currentPage = PAGE_DASH;
    } else {
        currentPage = PAGE_TRACK_PICKER;
        tp.dirty = true;
    }
    pageJustEntered = true;
}

static void drawConfigPicker() {
    if (pageJustEntered) {
        tft.fillScreen(TFT_BLACK);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.setTextDatum(textdatum_t::top_left);
        const char* tname = (cp.track_idx >= 0) ? TRACKS[cp.track_idx].name : "?";
        char hdr[64]; snprintf(hdr, sizeof(hdr), "Configure  \xBB  %s", tname);
        tft.drawString(hdr, 20, 18);
        pageJustEntered = false;
        cp.dirty = true;
    }
    if (!cp.dirty) return;
    cp.dirty = false;

    const TrackInfo& t = TRACKS[cp.track_idx];
    const int n = (int)t.n_configs;

    tft.fillRect(0, 60, 800, 390, TFT_BLACK);
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);

    for (int i = 0; i < n; ++i) {
        const int y   = cfgBtnY(n, i);
        const bool sel = (i == cp.selected);
        tft.fillRect(CFG_BTN_X, y, CFG_BTN_W, CFG_BTN_H,
                     sel ? TFT_DARKGREEN : TFT_NAVY);
        tft.drawRect(CFG_BTN_X, y, CFG_BTN_W, CFG_BTN_H, TFT_WHITE);
        tft.setTextColor(TFT_WHITE, sel ? TFT_DARKGREEN : TFT_NAVY);
        tft.drawString(t.configs[i].name,
                       CFG_BTN_X + CFG_BTN_W / 2, y + CFG_BTN_H / 2);
    }
    tft.setTextDatum(textdatum_t::top_left);

    // Footer — CANCEL left, CONFIRM right (green when something selected).
    tft.fillRect(0, 430, 800, 50, TFT_BLACK);
    tft.fillRect(20,  432, 360, 40, TFT_DARKGREY);
    tft.drawRect(20,  432, 360, 40, TFT_LIGHTGREY);
    const uint16_t cfmFill = (cp.selected >= 0) ? TFT_DARKGREEN : TFT_DARKGREY;
    tft.fillRect(420, 432, 360, 40, cfmFill);
    tft.drawRect(420, 432, 360, 40, TFT_LIGHTGREY);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, TFT_DARKGREY);
    tft.drawString("CANCEL",  200, 452);
    tft.setTextColor(TFT_WHITE, cfmFill);
    tft.drawString("CONFIRM", 600, 452);
    tft.setTextDatum(textdatum_t::top_left);
}

// ---------------------------------------------------------------------------
// WiFi scanner page rendering + tap dispatch.
// Layout:
//   y 0-40    : navy title bar
//   y 50-400  : scrollable-free list (cap 16 networks, fits in 350 px @ ~22 px/row)
//   y 410-470 : action row — RESCAN / TYPE SSID / CANCEL
// ---------------------------------------------------------------------------
namespace {
    constexpr int WSC_LIST_Y    = 50;
    constexpr int WSC_ROW_H     = 22;
    constexpr int WSC_LIST_BOT  = 400;
    constexpr int WSC_BTN_Y     = 410;
    constexpr int WSC_BTN_H     = 55;
    constexpr int WSC_BTN1_X    =  20, WSC_BTN1_W = 230;   // RESCAN
    constexpr int WSC_BTN2_X    = 285, WSC_BTN2_W = 230;   // TYPE SSID
    constexpr int WSC_BTN3_X    = 550, WSC_BTN3_W = 230;   // CANCEL
}

static void drawWifiScannerPage() {
    if (pageJustEntered) {
        tft.fillScreen(TFT_BLACK);
        pageJustEntered = false;
        wifi_scan_dirty = true;
        // Title bar (drawn once — doesn't change while scanner is open)
        tft.fillRect(0, 0, 800, 40, TFT_NAVY);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_left);
        tft.drawString("WiFi networks", 10, 20);
        // Static action buttons
        tft.fillRect(WSC_BTN1_X, WSC_BTN_Y, WSC_BTN1_W, WSC_BTN_H, TFT_NAVY);
        tft.drawRect(WSC_BTN1_X, WSC_BTN_Y, WSC_BTN1_W, WSC_BTN_H, TFT_WHITE);
        tft.fillRect(WSC_BTN2_X, WSC_BTN_Y, WSC_BTN2_W, WSC_BTN_H, TFT_DARKGREY);
        tft.drawRect(WSC_BTN2_X, WSC_BTN_Y, WSC_BTN2_W, WSC_BTN_H, TFT_WHITE);
        tft.fillRect(WSC_BTN3_X, WSC_BTN_Y, WSC_BTN3_W, WSC_BTN_H, TFT_MAROON);
        tft.drawRect(WSC_BTN3_X, WSC_BTN_Y, WSC_BTN3_W, WSC_BTN_H, TFT_WHITE);
        tft.setTextDatum(textdatum_t::middle_center);
        const int byc = WSC_BTN_Y + WSC_BTN_H / 2;
        tft.setTextColor(TFT_WHITE, TFT_NAVY);     tft.drawString("RESCAN",   WSC_BTN1_X + WSC_BTN1_W/2, byc);
        tft.setTextColor(TFT_WHITE, TFT_DARKGREY); tft.drawString("TYPE SSID",WSC_BTN2_X + WSC_BTN2_W/2, byc);
        tft.setTextColor(TFT_WHITE, TFT_MAROON);   tft.drawString("CANCEL",   WSC_BTN3_X + WSC_BTN3_W/2, byc);
        tft.setTextDatum(textdatum_t::top_left);
    }
    if (!wifi_scan_dirty) return;
    wifi_scan_dirty = false;

    // Clear body band
    tft.fillRect(0, 50, 800, WSC_LIST_BOT - 50, TFT_BLACK);

    tft.setFont(&fonts::Font2);
    tft.setTextSize(1);
    if (wifi_scan_state == WSC_RUNNING) {
        tft.setFont(&fonts::Font4);
        tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("Scanning for networks...", 400, (50 + WSC_LIST_BOT)/2);
        tft.setTextDatum(textdatum_t::top_left);
        return;
    }
    if (wifi_scan_count == 0) {
        tft.setFont(&fonts::Font4);
        tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("No networks found.", 400, (50 + WSC_LIST_BOT)/2);
        tft.setTextDatum(textdatum_t::top_left);
        return;
    }

    // List — strongest first (ESP32 returns in RSSI order).
    for (uint8_t i = 0; i < wifi_scan_count; i++) {
        const int y = WSC_LIST_Y + i * (WSC_ROW_H + 2);
        if (y + WSC_ROW_H > WSC_LIST_BOT) break;
        // Row background + border
        tft.fillRect(10, y, 780, WSC_ROW_H, TFT_BLACK);
        tft.drawRect(10, y, 780, WSC_ROW_H, TFT_DARKGREY);
        // SSID left
        tft.setFont(&fonts::Font2);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.setTextDatum(textdatum_t::middle_left);
        tft.drawString(wifi_scan_list[i].ssid, 20, y + WSC_ROW_H/2);
        // Right: WPA/OPN  + RSSI dBm
        char info[24];
        snprintf(info, sizeof(info), "%s  %ddBm",
                 wifi_scan_list[i].secured ? "WPA" : "OPN",
                 (int)wifi_scan_list[i].rssi);
        tft.setTextColor(wifi_scan_list[i].secured ? TFT_LIGHTGREY : TFT_CYAN, TFT_BLACK);
        tft.setTextDatum(textdatum_t::middle_right);
        tft.drawString(info, 770, y + WSC_ROW_H/2);
    }
    tft.setTextDatum(textdatum_t::top_left);
}

static void handleWifiScannerTap(int x, int y) {
    // Action row first (fixed positions).
    if (y >= WSC_BTN_Y && y <= WSC_BTN_Y + WSC_BTN_H) {
        if (x >= WSC_BTN1_X && x <= WSC_BTN1_X + WSC_BTN1_W) {
            startWifiScan();   // RESCAN
            return;
        }
        if (x >= WSC_BTN2_X && x <= WSC_BTN2_X + WSC_BTN2_W) {
            // TYPE SSID — open the existing text keyboard for manual entry.
            wifi_scan_state = WSC_IDLE;
            openTextKeyboard(ST_WIFI_SSID);
            return;
        }
        if (x >= WSC_BTN3_X && x <= WSC_BTN3_X + WSC_BTN3_W) {
            // CANCEL — back to settings; restart whatever WiFi connection.
            wifi_scan_state = WSC_IDLE;
            currentPage = PAGE_SETTINGS;
            pageJustEntered = true;
            settingsDirty   = true;
            wifiForceReconfigure();
            return;
        }
    }
    // List rows — only tappable once scan completed.
    if (wifi_scan_state != WSC_DONE) return;
    for (uint8_t i = 0; i < wifi_scan_count; i++) {
        const int ry = WSC_LIST_Y + i * (WSC_ROW_H + 2);
        if (ry + WSC_ROW_H > WSC_LIST_BOT) break;
        if (y >= ry && y <= ry + WSC_ROW_H) {
            // Selected this network.
            strncpy(s.wifi_ssid, wifi_scan_list[i].ssid, sizeof(s.wifi_ssid) - 1);
            s.wifi_ssid[sizeof(s.wifi_ssid) - 1] = '\0';
            wifi_scan_state = WSC_IDLE;
            if (!wifi_scan_list[i].secured) {
                // Open network — no password needed.
                s.wifi_pass[0] = '\0';
                wifiForceReconfigure();
                currentPage = PAGE_SETTINGS;
                pageJustEntered = true;
                settingsDirty   = true;
            } else {
                // Secured — prompt for password via keyboard.
                openTextKeyboard(ST_WIFI_PASS);
            }
            return;
        }
    }
}

// ---------------------------------------------------------------------------
// Upload progress modal — full-screen, blocks all other input. Driven by the
// Teensy's UPLOAD,START/PROG/DONE lines emitted from httpPost() while it
// streams a session/queue file. Only escape is the CANCEL button, which
// emits UPLOAD,CANCEL to the Teensy. After CANCEL, no further uploads will
// start until the system is power-cycled (the Teensy latches uploads_disabled).
// ---------------------------------------------------------------------------
namespace {
    constexpr int UM_CARD_X = 100, UM_CARD_Y = 110, UM_CARD_W = 600, UM_CARD_H = 260;
    constexpr int UM_BAR_X  = UM_CARD_X + 30, UM_BAR_Y = UM_CARD_Y + 130;
    constexpr int UM_BAR_W  = UM_CARD_W - 60, UM_BAR_H = 32;
    constexpr int UM_BTN_X  = UM_CARD_X + (UM_CARD_W - 220) / 2;
    constexpr int UM_BTN_Y  = UM_CARD_Y + UM_CARD_H - 70;
    constexpr int UM_BTN_W  = 220, UM_BTN_H = 56;
}

static void drawUploadModal() {
    upload_modal_dirty = false;
    if (pageJustEntered) {
        // Dim the whole screen, then draw the card.
        tft.fillScreen(TFT_BLACK);
        tft.fillRect(UM_CARD_X, UM_CARD_Y, UM_CARD_W, UM_CARD_H, TFT_NAVY);
        tft.drawRect(UM_CARD_X,   UM_CARD_Y,   UM_CARD_W,   UM_CARD_H,   TFT_WHITE);
        tft.drawRect(UM_CARD_X+1, UM_CARD_Y+1, UM_CARD_W-2, UM_CARD_H-2, TFT_WHITE);
        // Title
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("Uploading session", UM_CARD_X + UM_CARD_W / 2,
                       UM_CARD_Y + 30);
        // Static CANCEL button frame
        tft.fillRect(UM_BTN_X, UM_BTN_Y, UM_BTN_W, UM_BTN_H, TFT_MAROON);
        tft.drawRect(UM_BTN_X, UM_BTN_Y, UM_BTN_W, UM_BTN_H, TFT_WHITE);
        tft.drawRect(UM_BTN_X+1, UM_BTN_Y+1, UM_BTN_W-2, UM_BTN_H-2, TFT_WHITE);
        tft.setTextColor(TFT_WHITE, TFT_MAROON);
        tft.drawString("CANCEL", UM_BTN_X + UM_BTN_W/2, UM_BTN_Y + UM_BTN_H/2);
        tft.setTextDatum(textdatum_t::top_left);
        pageJustEntered = false;
    }

    // Filename (truncated visually by the padded background).
    tft.setFont(&fonts::Font2);
    tft.setTextSize(1);
    tft.setTextColor(TFT_LIGHTGREY, TFT_NAVY);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextPadding(UM_CARD_W - 40);
    tft.drawString(upload_file, UM_CARD_X + UM_CARD_W / 2, UM_CARD_Y + 70);
    tft.setTextPadding(0);

    // Progress bar frame + fill
    const uint32_t total = upload_total > 0 ? upload_total : 1;
    const uint32_t done  = upload_done > total ? total : upload_done;
    const int fillW = (int)((uint64_t)done * (UM_BAR_W - 4) / total);
    tft.drawRect(UM_BAR_X, UM_BAR_Y, UM_BAR_W, UM_BAR_H, TFT_WHITE);
    tft.fillRect(UM_BAR_X + 2, UM_BAR_Y + 2, UM_BAR_W - 4, UM_BAR_H - 4, TFT_BLACK);
    if (fillW > 0)
        tft.fillRect(UM_BAR_X + 2, UM_BAR_Y + 2, fillW, UM_BAR_H - 4, TFT_GREEN);

    // Bytes / percentage line
    char line[64];
    const int pct = (int)((uint64_t)done * 100 / total);
    if (upload_total >= 1024 * 1024) {
        snprintf(line, sizeof(line), "%lu.%lu / %lu.%lu MB   %d%%",
                 (unsigned long)(done  / (1024UL*1024UL)),
                 (unsigned long)((done  % (1024UL*1024UL)) / 104858UL),   // 0-9 = 1/10 MB
                 (unsigned long)(total / (1024UL*1024UL)),
                 (unsigned long)((total % (1024UL*1024UL)) / 104858UL),
                 pct);
    } else {
        snprintf(line, sizeof(line), "%lu / %lu KB   %d%%",
                 (unsigned long)(done  / 1024UL),
                 (unsigned long)(total / 1024UL),
                 pct);
    }
    tft.setFont(&fonts::Font4);
    tft.setTextColor(TFT_WHITE, TFT_NAVY);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextPadding(UM_CARD_W - 40);
    tft.drawString(line, UM_CARD_X + UM_CARD_W / 2, UM_BAR_Y + UM_BAR_H + 24);
    tft.setTextPadding(0);

    // Result banner replaces filename area when DONE arrives.
    if (upload_result_msg[0] != '\0') {
        uint16_t banner = TFT_GREEN;
        if (strcmp(upload_result_msg, "FAIL")      == 0) banner = TFT_RED;
        if (strcmp(upload_result_msg, "CANCELLED") == 0) banner = TFT_ORANGE;
        tft.setFont(&fonts::Font4);
        tft.setTextColor(banner, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextPadding(UM_CARD_W - 40);
        tft.drawString(upload_result_msg, UM_CARD_X + UM_CARD_W / 2, UM_CARD_Y + 70);
        tft.setTextPadding(0);
    }
    tft.setTextDatum(textdatum_t::top_left);
}

static void handleUploadModalTap(int x, int y) {
    // Only the CANCEL button is tappable. Everything else: ignored.
    if (x >= UM_BTN_X && x <= UM_BTN_X + UM_BTN_W &&
        y >= UM_BTN_Y && y <= UM_BTN_Y + UM_BTN_H) {
        if (!upload_locally_cancelled) {
            Serial.println("UPLOAD,CANCEL");
            upload_locally_cancelled = true;
        }
        // Update button to a 'cancelling...' indication while waiting for
        // the Teensy's DONE,CANCELLED ack.
        tft.fillRect(UM_BTN_X, UM_BTN_Y, UM_BTN_W, UM_BTN_H, TFT_DARKGREY);
        tft.drawRect(UM_BTN_X, UM_BTN_Y, UM_BTN_W, UM_BTN_H, TFT_WHITE);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_DARKGREY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("CANCELLING...", UM_BTN_X + UM_BTN_W/2, UM_BTN_Y + UM_BTN_H/2);
        tft.setTextDatum(textdatum_t::top_left);
    }
}

// ---------------------------------------------------------------------------
// OTA modal + state machine (CrowPanel self-update over WiFi).
// Pulls firmware/manifest.json from this repo's main branch on GitHub,
// compares versions, and if newer is available, streams the .bin into
// Update.h. Teensy forwarding lands in a follow-up patch (Phase 2b).
//
// CA validation: WiFiClientSecure.setInsecure() for v1. GitHub serves with
// DigiCert; embedding the root would be more secure. Listed as future work.
// ---------------------------------------------------------------------------
static constexpr const char* OTA_MANIFEST_URL =
    "https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/manifest.json";

namespace {
    constexpr int OM_CARD_X = 80,  OM_CARD_Y = 100;
    constexpr int OM_CARD_W = 640, OM_CARD_H = 280;
    constexpr int OM_BAR_X  = OM_CARD_X + 30, OM_BAR_Y = OM_CARD_Y + 160;
    constexpr int OM_BAR_W  = OM_CARD_W - 60, OM_BAR_H = 28;
    // Two-button row: confirm (or close) + cancel
    constexpr int OM_BTN_Y  = OM_CARD_Y + OM_CARD_H - 70;
    constexpr int OM_BTN_H  = 56;
    constexpr int OM_BTN1_X = OM_CARD_X + 60,  OM_BTN1_W = 220;
    constexpr int OM_BTN2_X = OM_CARD_X + 360, OM_BTN2_W = 220;
}

static const char* otaStateLabel() {
    switch (ota_state) {
        case OTA_S_CHECKING:           return "Checking for updates...";
        case OTA_S_UPTODATE:           return ota_err_msg[0] ? ota_err_msg : "Up to date";
        case OTA_S_AVAILABLE: {
            // Show what we're going to update.
            static char buf[40];
            if (ota_need_teensy && ota_need_crowpanel) return "Update Teensy + Dash";
            if (ota_need_teensy)                       return "Update Teensy";
            return "Update Dash";
        }
        case OTA_S_TEENSY_DOWNLOADING: return "Updating Teensy...";
        case OTA_S_TEENSY_WAITING:     return "Waiting for Teensy reboot...";
        case OTA_S_DOWNLOADING:        return "Updating Dash...";
        case OTA_S_APPLYING:           return "Applying update...";
        case OTA_S_REBOOT:             return "Restarting...";
        case OTA_S_FAILED:             return "Update failed";
        default:                       return "";
    }
}

static void otaOpenModal() {
    if (currentPage != PAGE_OTA) ota_return_page = (uint8_t)currentPage;
    currentPage     = PAGE_OTA;
    pageJustEntered = true;
    ota_modal_dirty = true;
}

static void otaCloseModal() {
    ota_state        = OTA_S_IDLE;
    currentPage      = (Page)ota_return_page;
    pageJustEntered  = true;
    settingsDirty    = true;
    invalidateAll();
}

// Very small JSON value extractor for our specific manifest shape. Looks for
// the first '"key":"value"' AFTER the section marker. Robust enough for the
// hand-authored manifest.json we ship; would be naive on arbitrary JSON.
static bool jsonStr(const String& body, const char* section, const char* key,
                    char* out, size_t outsize) {
    int s = body.indexOf(section);
    if (s < 0) return false;
    int k = body.indexOf(key, s);
    if (k < 0) return false;
    int q = body.indexOf('"', k + strlen(key));
    if (q < 0) return false;
    int q2 = body.indexOf('"', q + 1);
    if (q2 < 0) return false;
    int n = q2 - q - 1;
    if (n >= (int)outsize) n = (int)outsize - 1;
    body.substring(q + 1, q + 1 + n).toCharArray(out, n + 1);
    return true;
}

static const char* versionNoV(const char* v) {
    if (!v) return "";
    return (v[0] == 'v' || v[0] == 'V') ? v + 1 : v;
}

static int versionCmp(const char* a, const char* b) {
    a = versionNoV(a);
    b = versionNoV(b);
    int aa[4] = {0,0,0,0}, bb[4] = {0,0,0,0};
    sscanf(a, "%d.%d.%d.%d", &aa[0],&aa[1],&aa[2],&aa[3]);
    sscanf(b, "%d.%d.%d.%d", &bb[0],&bb[1],&bb[2],&bb[3]);
    for (int i = 0; i < 4; ++i) if (aa[i] != bb[i]) return aa[i] - bb[i];
    return 0;
}

static void otaStart() {
    if (s.internet_mode != 1) {
        snprintf(ota_err_msg, sizeof(ota_err_msg),
                 "OTA needs WiFi mode (Ethernet OTA coming soon)");
        ota_state = OTA_S_FAILED;
    } else if (wifi_state != WS_CONNECTED) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "WiFi not connected");
        ota_state = OTA_S_FAILED;
    } else {
        ota_latest_version[0] = '\0';
        ota_url[0]            = '\0';
        ota_err_msg[0]        = '\0';
        ota_total_bytes       = 0;
        ota_done_bytes        = 0;
        ota_cancel_requested  = false;
        ota_state             = OTA_S_CHECKING;
    }
    otaOpenModal();
}

static void otaDoCheck() {
    WiFiClientSecure client;
    client.setInsecure();   // TODO: embed GitHub root CA for proper validation
    HTTPClient http;
    if (!http.begin(client, OTA_MANIFEST_URL)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "manifest HTTPS begin failed");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    const int code = http.GET();
    if (code != HTTP_CODE_OK) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "manifest fetch HTTP %d", code);
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    const String body = http.getString();
    http.end();

    if (!jsonStr(body, "\"crowpanel\"", "\"version\"",
                 ota_latest_version, sizeof(ota_latest_version)) ||
        !jsonStr(body, "\"crowpanel\"", "\"url\"",
                 ota_url, sizeof(ota_url))) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "manifest parse failed");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    // Parse teensy entry too (optional — if missing, only crowpanel update considered)
    ota_teensy_version[0] = '\0';
    ota_teensy_url[0]     = '\0';
    ota_teensy_size       = 0;
    char tsize_buf[16] = "";
    jsonStr(body, "\"teensy\"", "\"version\"",
            ota_teensy_version, sizeof(ota_teensy_version));
    jsonStr(body, "\"teensy\"", "\"url\"",
            ota_teensy_url, sizeof(ota_teensy_url));
    // Size is a number in the manifest, not a quoted string. Find it by hand.
    {
        int s = body.indexOf("\"teensy\"");
        int k = (s >= 0) ? body.indexOf("\"size\"", s) : -1;
        int colon = (k >= 0) ? body.indexOf(':', k + 6) : -1;
        if (colon >= 0) ota_teensy_size = (uint32_t)body.substring(colon + 1).toInt();
    }

    ota_need_crowpanel = (versionCmp(ota_latest_version, FIRMWARE_VERSION) > 0);
    const bool teensy_known = (strcmp(teensy_fw_version, "?") != 0);
    ota_need_teensy    = teensy_known && ota_teensy_version[0] &&
                         (versionCmp(ota_teensy_version, teensy_fw_version) > 0);

    if (!ota_need_crowpanel && !ota_need_teensy) {
        ota_state = OTA_S_UPTODATE;
    } else {
        ota_state = OTA_S_AVAILABLE;
    }
    ota_modal_dirty = true;
}

// Read one line (terminated by \n) from Serial with a timeout. Returns true if
// a line was captured into `out` (NUL-terminated), false on timeout. Used to
// catch the Teensy's 'FW,READY,<size>' ack without going through pumpUart
// (which is not running while we're inside the synchronous OTA loop).
static bool readSerialLineTimeout(char* out, size_t outsize, uint32_t timeout_ms) {
    out[0] = '\0';
    size_t n = 0;
    const uint32_t end = millis() + timeout_ms;
    while ((int32_t)(end - millis()) > 0) {
        while (Serial.available()) {
            const char c = (char)Serial.read();
            if (c == '\n') { out[n] = '\0'; return true; }
            if (c != '\r' && n + 1 < outsize) out[n++] = c;
        }
        delay(2);
    }
    out[n] = '\0';
    return false;
}

// Download teensy.hex over WiFi into PSRAM, then forward line-by-line to the
// Teensy over UART with per-line ACK. Why download upfront:
//   - the ACK-paced UART transfer takes 30-60 s total
//   - if we read from the HTTPS stream that whole time the GitHub CDN closes
//     the connection from idleness (we previously saw 'ran out of stream
//     at 51628 / 524284 B')
//   - PSRAM has plenty of headroom (8 MB) for a ~512 KB hex file
static void otaDoTeensyUpdate() {
    if (ota_teensy_url[0] == '\0' || ota_teensy_size == 0) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "teensy: missing url/size");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(15);
    HTTPClient http;
    if (!http.begin(client, ota_teensy_url)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "teensy hex HTTPS begin failed");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    const int code = http.GET();
    if (code != HTTP_CODE_OK) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "teensy hex HTTP %d", code);
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    const int contentLen = http.getSize();
    if (contentLen <= 0) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "teensy hex no content-length");
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    ota_total_bytes = (uint32_t)contentLen;
    ota_done_bytes  = 0;

    // Phase 1/2: pull the entire hex body into PSRAM (fast, ~3 s for 512 KB).
    uint8_t* hexbuf = (uint8_t*)ps_malloc((size_t)contentLen);
    if (!hexbuf) {
        snprintf(ota_err_msg, sizeof(ota_err_msg),
                 "PSRAM alloc failed (%d B)", contentLen);
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    {
        WiFiClient* stream = http.getStreamPtr();
        uint32_t dl_done = 0;
        uint32_t last_dl_draw = millis();
        while (dl_done < (uint32_t)contentLen) {
            if (ota_cancel_requested) {
                free(hexbuf); http.end();
                snprintf(ota_err_msg, sizeof(ota_err_msg), "cancelled during download");
                ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
            }
            const size_t avail = stream->available();
            if (avail == 0) {
                if (!http.connected()) {
                    free(hexbuf); http.end();
                    snprintf(ota_err_msg, sizeof(ota_err_msg),
                             "teensy download truncated: %lu/%d",
                             (unsigned long)dl_done, contentLen);
                    ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
                }
                delay(2); continue;
            }
            const size_t want = avail > (uint32_t)contentLen - dl_done
                                ? (uint32_t)contentLen - dl_done : avail;
            const int got = stream->read(hexbuf + dl_done, want);
            if (got <= 0) { delay(2); continue; }
            dl_done += (uint32_t)got;
            // Modal progress for phase 1: show 0-50% of total.
            if (millis() - last_dl_draw >= 250) {
                last_dl_draw = millis();
                ota_done_bytes = dl_done / 2;
                ota_modal_dirty = true;
                drawOtaModal();
            }
        }
        http.end();
    }

    // Drain anything the Teensy might have queued before our request.
    while (Serial.available()) Serial.read();

    // Phase 2/2: kick the Teensy into firmware-receive mode and wait for FW,READY.
    Serial.println("FWUPDATE");
    Serial.flush();
    char reply[80];
    if (!readSerialLineTimeout(reply, sizeof(reply), 3000) ||
        strncmp(reply, "FW,READY", 8) != 0) {
        free(hexbuf);
        snprintf(ota_err_msg, sizeof(ota_err_msg),
                 "teensy didn't ack FW,READY (%s)", reply[0] ? reply : "timeout");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }

    // Stream the hex body LINE BY LINE with per-line ACK from the Teensy.
    // This is the only reliable way at 921 600 baud — the previous raw byte
    // stream lost ~0.8% of records to UART overflow / signal-integrity
    // glitches during flash sector erases. With ACKs, the Teensy controls
    // the pace: each line is only sent once we know the previous one
    // landed cleanly.
    char    ack_buf[40];
    uint32_t last_draw_ms = millis();
    uint32_t pos = 0;
    uint32_t line_no = 0;
    while (pos < (uint32_t)contentLen) {
        if (ota_cancel_requested) {
            free(hexbuf);
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "teensy update cancelled — reflash via USB to recover");
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
        // Locate the next '\n' in the PSRAM buffer.
        uint32_t line_start = pos;
        while (pos < (uint32_t)contentLen && hexbuf[pos] != '\n') pos++;
        uint32_t line_len = pos - line_start;
        if (pos < (uint32_t)contentLen) pos++;   // skip the '\n'
        // Strip a trailing '\r' if present (CRLF line endings in the file).
        if (line_len > 0 && hexbuf[line_start + line_len - 1] == '\r') line_len--;
        if (line_len == 0) continue;
        line_no++;

        // Send line + '\n'.
        Serial.write(hexbuf + line_start, (size_t)line_len);
        Serial.write('\n');
        Serial.flush();

        // Wait for 'A\n' ack (or FW,ERR,*). 2 s timeout covers any flash stall.
        if (!readSerialLineTimeout(ack_buf, sizeof(ack_buf), 2000)) {
            free(hexbuf);
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "teensy ACK timeout after %lu lines", (unsigned long)line_no);
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
        if (ack_buf[0] == 'A' && (ack_buf[1] == '\0' || ack_buf[1] == '\r')) {
            // ack ok
        } else if (strncmp(ack_buf, "FW,ERR", 6) == 0) {
            free(hexbuf);
            snprintf(ota_err_msg, sizeof(ota_err_msg), "teensy: %s", ack_buf);
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        } else {
            // Stray telemetry interleaved? Try one more line for the ACK.
            if (!readSerialLineTimeout(ack_buf, sizeof(ack_buf), 2000) ||
                ack_buf[0] != 'A') {
                free(hexbuf);
                snprintf(ota_err_msg, sizeof(ota_err_msg),
                         "teensy unexpected ACK: %s", ack_buf);
                ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
            }
        }
        // Modal progress for phase 2: 50% -> 100% of total bytes.
        ota_done_bytes = (uint32_t)contentLen / 2 + pos / 2;
        if (millis() - last_draw_ms >= 400) {
            last_draw_ms = millis();
            ota_modal_dirty = true;
            drawOtaModal();
        }
    }
    free(hexbuf);
    Serial.flush();
    // EOF of the .hex file (':00000001FF') was sent in-stream. Teensy will now
    // call flash_move() and reboot. Transition to TEENSY_WAITING so the modal
    // shows the wait + future ticks come back through.
    ota_state       = OTA_S_TEENSY_WAITING;
    ota_modal_dirty = true;
}

// After we've finished streaming the Teensy hex, the Teensy still has to run
// flash_move() and reboot. That can take longer than a fixed 12 s window, so
// poll for VER,teensy,<target> for a generous period instead of declaring a
// scary false failure while the update is actually finishing.
static void otaDoTeensyWaiting() {
    static uint32_t wait_start   = 0;
    static uint32_t last_ping_ms = 0;
    static uint32_t last_draw_ms = 0;
    static char     line[160];
    static size_t   line_n = 0;
    static char     last_seen[80];

    const uint32_t now = millis();
    constexpr uint32_t TEENSY_REBOOT_TIMEOUT_MS = 60000;
    constexpr uint32_t TEENSY_VER_PING_MS       = 1000;

    if (wait_start == 0) {
        wait_start   = now;
        last_ping_ms = 0;
        last_draw_ms = 0;
        line_n       = 0;
        last_seen[0] = '\0';
        // Clear any stale bytes left from FW,COMMITTING/boot chatter; from
        // here onward we parse every full line so we don't miss VER.
        while (Serial.available()) Serial.read();
    }

    // Periodically ask for the version. If the Teensy is still inside
    // flash_move() or rebooting, this is harmless; once setup()/loop() are
    // alive, handleDashCommand() answers with VER,teensy,<version>.
    if (last_ping_ms == 0 || now - last_ping_ms >= TEENSY_VER_PING_MS) {
        Serial.println("VER?");
        Serial.flush();
        last_ping_ms = now;
    }

    // Non-blocking UART line parser. Do not call readSerialLineTimeout() here:
    // short timeouts can consume partial lines and accidentally split the VER
    // response. This keeps a persistent line buffer across otaTick() calls.
    bool saw_target = false;
    while (Serial.available()) {
        const char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            line[line_n] = '\0';
            if (line_n > 0) {
                strncpy(last_seen, line, sizeof(last_seen) - 1);
                last_seen[sizeof(last_seen) - 1] = '\0';
                if (strncmp(line, "VER,teensy,", 11) == 0) {
                    strncpy(teensy_fw_version, versionNoV(line + 11),
                            sizeof(teensy_fw_version) - 1);
                    teensy_fw_version[sizeof(teensy_fw_version) - 1] = '\0';
                    if (versionCmp(teensy_fw_version, ota_teensy_version) == 0) {
                        saw_target = true;
                    }
                }
            }
            line_n = 0;
        } else if (line_n + 1 < sizeof(line)) {
            line[line_n++] = c;
        } else {
            line_n = 0;  // malformed/too long line; resync at next newline
        }
    }

    if (saw_target) {
        wait_start = 0;
        // Teensy is now at the target version. Continue to CrowPanel if needed.
        if (ota_need_crowpanel) {
            ota_total_bytes = 0;
            ota_done_bytes  = 0;
            ota_state       = OTA_S_DOWNLOADING;
        } else {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "Teensy updated to v%s", teensy_fw_version);
            ota_state = OTA_S_UPTODATE;
        }
        ota_modal_dirty = true;
        return;
    }

    const uint32_t elapsed = now - wait_start;
    if (elapsed >= TEENSY_REBOOT_TIMEOUT_MS) {
        wait_start = 0;
        if (teensy_fw_version[0] && strcmp(teensy_fw_version, "?") != 0) {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "teensy reports v%s, expected v%s",
                     teensy_fw_version, ota_teensy_version);
        } else if (last_seen[0]) {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "no VER from teensy; last: %.45s", last_seen);
        } else {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "no VER from teensy after 60s");
        }
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }

    // Progress bar during wait/poll. Redraw at 5 Hz max.
    if (last_draw_ms == 0 || now - last_draw_ms >= 200) {
        last_draw_ms = now;
        ota_done_bytes  = elapsed;
        ota_total_bytes = TEENSY_REBOOT_TIMEOUT_MS;
        ota_modal_dirty = true;
        drawOtaModal();
    }
}

static void otaDoDownload() {
    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(15);   // seconds; setTimeout is in seconds for WiFiClient
    HTTPClient http;
    if (!http.begin(client, ota_url)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), ".bin HTTPS begin failed");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    const int code = http.GET();
    if (code != HTTP_CODE_OK) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), ".bin fetch HTTP %d", code);
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    const int contentLen = http.getSize();
    if (contentLen <= 0) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "unknown content length");
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }

    // Download the CrowPanel .bin into PSRAM first, then close HTTPS BEFORE
    // writing flash. This mirrors the Teensy-hex path and avoids doing LCD
    // redraws + WiFi/TLS reads + Update.write flash erases all at once. The
    // previous direct stream-to-Update path could make the RGB panel flicker
    // badly while flash writes were occurring.
    uint8_t* binbuf = (uint8_t*)ps_malloc((size_t)contentLen);
    if (!binbuf) {
        snprintf(ota_err_msg, sizeof(ota_err_msg),
                 "PSRAM alloc failed (%d B)", contentLen);
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }

    ota_total_bytes = (uint32_t)contentLen;
    ota_done_bytes  = 0;
    WiFiClient* stream = http.getStreamPtr();
    uint32_t dl_done = 0;
    uint32_t last_draw_ms = millis();
    while (dl_done < (uint32_t)contentLen) {
        if (ota_cancel_requested) {
            free(binbuf); http.end();
            snprintf(ota_err_msg, sizeof(ota_err_msg), "Cancelled by user");
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
        const size_t avail = stream->available();
        if (avail == 0) {
            // If the HTTP socket closed and no buffered data remains, this is
            // a real truncated download. Otherwise wait for more bytes.
            if (!http.connected()) {
                free(binbuf); http.end();
                snprintf(ota_err_msg, sizeof(ota_err_msg),
                         "dash download truncated: %lu/%d",
                         (unsigned long)dl_done, contentLen);
                ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
            }
            delay(2); continue;
        }
        const size_t want = avail > (uint32_t)contentLen - dl_done
                            ? (uint32_t)contentLen - dl_done : avail;
        const int got = stream->read(binbuf + dl_done, want);
        if (got <= 0) { delay(2); continue; }
        dl_done += (uint32_t)got;
        ota_done_bytes = dl_done;
        // Slow UI updates during download only; flash-write phase below does
        // not repaint the screen at all.
        if (millis() - last_draw_ms >= 500) {
            last_draw_ms = millis();
            ota_modal_dirty = true;
            drawOtaModal();
        }
    }
    http.end();

    // Stable one-time transition to APPLYING before flash writes begin.
    ota_state       = OTA_S_APPLYING;
    ota_done_bytes  = ota_total_bytes;
    ota_modal_dirty = true;
    drawOtaModal();

    if (!Update.begin((size_t)contentLen)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "Update.begin: %s",
                 Update.errorString());
        free(binbuf);
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }

    // Write to OTA partition from PSRAM. Do NOT redraw the LCD in this loop:
    // Update.write triggers flash erases/writes, and mixing flash writes with
    // RGB panel drawing is what caused the hard visible flashing.
    uint32_t written_total = 0;
    while (written_total < (uint32_t)contentLen) {
        const size_t chunk = ((uint32_t)contentLen - written_total) > 4096
                             ? 4096 : ((uint32_t)contentLen - written_total);
        const size_t w = Update.write(binbuf + written_total, chunk);
        if (w != chunk) {
            snprintf(ota_err_msg, sizeof(ota_err_msg), "Update.write: %s",
                     Update.errorString());
            Update.abort();
            free(binbuf);
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
        written_total += (uint32_t)w;
        delay(0);   // feed background tasks, but no display work here
    }
    free(binbuf);

    if (!Update.end(true)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "Update.end: %s",
                 Update.errorString());
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    ota_state       = OTA_S_REBOOT;
    ota_modal_dirty = true;
    drawOtaModal();
    delay(800);     // give the user a chance to see the success banner
    ESP.restart();
}

static void otaTick() {
    if (currentPage != PAGE_OTA) return;
    if (ota_state == OTA_S_CHECKING)             { otaDoCheck();         return; }
    if (ota_state == OTA_S_TEENSY_DOWNLOADING)   { otaDoTeensyUpdate();  return; }
    if (ota_state == OTA_S_TEENSY_WAITING)       { otaDoTeensyWaiting(); return; }
    if (ota_state == OTA_S_DOWNLOADING)          { otaDoDownload();      return; }
}

// Anti-flicker version. Static chrome (card frame + title) painted once on
// page entry. Dynamic regions (state label, version line, progress bar,
// button strip) repaint only when their underlying value/state changes, and
// use setTextColor(fg, TFT_NAVY) + setTextPadding so text changes don't
// require a fillRect-then-drawString that catches the LCD mid-scan.
static OtaState  om_last_state    = (OtaState)0xFF;       // sentinel
static uint32_t  om_last_done_kb  = 0xFFFFFFFFu;
static uint32_t  om_last_total_kb = 0xFFFFFFFFu;
static char      om_last_status[16] = "\x01";              // sentinel
static char      om_last_vline[40]  = "\x01";

static void omPaintButton(int x, int w, uint16_t fill, const char* label) {
    tft.fillRect(x, OM_BTN_Y, w, OM_BTN_H, fill);
    tft.drawRect(x, OM_BTN_Y, w, OM_BTN_H, TFT_WHITE);
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextColor(TFT_WHITE, fill);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.drawString(label, x + w/2, OM_BTN_Y + OM_BTN_H/2);
}

static void drawOtaModal() {
    if (!ota_modal_dirty && !pageJustEntered) return;
    ota_modal_dirty = false;

    if (pageJustEntered) {
        // Full chrome paint, once.
        tft.fillScreen(TFT_BLACK);
        tft.fillRect(OM_CARD_X,   OM_CARD_Y,   OM_CARD_W,   OM_CARD_H,   TFT_NAVY);
        tft.drawRect(OM_CARD_X,   OM_CARD_Y,   OM_CARD_W,   OM_CARD_H,   TFT_WHITE);
        tft.drawRect(OM_CARD_X+1, OM_CARD_Y+1, OM_CARD_W-2, OM_CARD_H-2, TFT_WHITE);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("Firmware update", OM_CARD_X + OM_CARD_W / 2, OM_CARD_Y + 30);
        // Progress bar outline (drawn once — only the inner fill is dynamic).
        tft.drawRect(OM_BAR_X, OM_BAR_Y, OM_BAR_W, OM_BAR_H, TFT_WHITE);
        tft.fillRect(OM_BAR_X + 2, OM_BAR_Y + 2, OM_BAR_W - 4, OM_BAR_H - 4, TFT_BLACK);
        pageJustEntered = false;
        // Force every dynamic block to repaint on this first frame.
        om_last_state      = (OtaState)0xFF;
        om_last_done_kb    = 0xFFFFFFFFu;
        om_last_total_kb   = 0xFFFFFFFFu;
        om_last_status[0]  = '\x01';
        om_last_vline[0]   = '\x01';
    }

    // ---- State label (line under title). Repaint only on label change. ----
    const char* slabel = otaStateLabel();
    if (strncmp(slabel, om_last_status, sizeof(om_last_status)) != 0) {
        strncpy(om_last_status, slabel, sizeof(om_last_status));
        om_last_status[sizeof(om_last_status)-1] = '\0';
        tft.setFont(&fonts::Font2);
        tft.setTextSize(1);
        tft.setTextColor(TFT_LIGHTGREY, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextPadding(OM_CARD_W - 40);
        tft.drawString(slabel, OM_CARD_X + OM_CARD_W / 2, OM_CARD_Y + 65);
        tft.setTextPadding(0);
    }

    // ---- Version line. Repaint only when string content changes. ----
    char vline[64];
    if (ota_state == OTA_S_TEENSY_DOWNLOADING || ota_state == OTA_S_TEENSY_WAITING) {
        snprintf(vline, sizeof(vline), "Teensy  v%s  →  v%s",
                 teensy_fw_version, ota_teensy_version);
    } else if (ota_state == OTA_S_DOWNLOADING || ota_state == OTA_S_APPLYING ||
               ota_state == OTA_S_REBOOT) {
        snprintf(vline, sizeof(vline), "Dash    v%s  →  v%s",
                 FIRMWARE_VERSION, ota_latest_version);
    } else if (ota_state == OTA_S_AVAILABLE) {
        // Show whichever sides will get hit.
        if (ota_need_teensy && ota_need_crowpanel)
            snprintf(vline, sizeof(vline), "Teensy v%s→v%s  /  Dash v%s→v%s",
                     teensy_fw_version, ota_teensy_version,
                     FIRMWARE_VERSION,  ota_latest_version);
        else if (ota_need_teensy)
            snprintf(vline, sizeof(vline), "Teensy  v%s  →  v%s",
                     teensy_fw_version, ota_teensy_version);
        else
            snprintf(vline, sizeof(vline), "Dash    v%s  →  v%s",
                     FIRMWARE_VERSION, ota_latest_version);
    } else if (ota_latest_version[0]) {
        snprintf(vline, sizeof(vline), "v%s  →  v%s", FIRMWARE_VERSION, ota_latest_version);
    } else {
        snprintf(vline, sizeof(vline), "current: v%s", FIRMWARE_VERSION);
    }
    if (strncmp(vline, om_last_vline, sizeof(om_last_vline)) != 0) {
        strncpy(om_last_vline, vline, sizeof(om_last_vline));
        om_last_vline[sizeof(om_last_vline)-1] = '\0';
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextPadding(OM_CARD_W - 40);
        tft.drawString(vline, OM_CARD_X + OM_CARD_W / 2, OM_CARD_Y + 100);
        tft.setTextPadding(0);
    }

    // ---- Progress bar inner fill + KB/% line. Only when bytes-delta moves. ----
    const bool show_bar = (ota_state == OTA_S_DOWNLOADING        ||
                           ota_state == OTA_S_TEENSY_DOWNLOADING ||
                           ota_state == OTA_S_TEENSY_WAITING     ||
                           ota_state == OTA_S_APPLYING           ||
                           ota_state == OTA_S_REBOOT);
    if (show_bar) {
        const uint32_t total_kb = ota_total_bytes > 0 ? (ota_total_bytes / 1024UL) : 1;
        const uint32_t done_kb  = ota_done_bytes / 1024UL;
        if (done_kb != om_last_done_kb || total_kb != om_last_total_kb) {
            om_last_done_kb  = done_kb;
            om_last_total_kb = total_kb;
            const uint32_t total = ota_total_bytes > 0 ? ota_total_bytes : 1;
            const uint32_t done  = ota_done_bytes  > total ? total : ota_done_bytes;
            const int fillW = (int)((uint64_t)done * (OM_BAR_W - 4) / total);
            // Paint only the green delta band — don't touch already-green cells.
            // Right side beyond current fill stays black (drawn once on entry).
            if (fillW > 0)
                tft.fillRect(OM_BAR_X + 2, OM_BAR_Y + 2, fillW, OM_BAR_H - 4, TFT_GREEN);
            const int pct = (int)((uint64_t)done * 100 / total);
            char pbline[40];
            snprintf(pbline, sizeof(pbline), "%lu / %lu KB   %d%%",
                     (unsigned long)done_kb, (unsigned long)total_kb, pct);
            tft.setFont(&fonts::Font2);
            tft.setTextSize(1);
            tft.setTextColor(TFT_WHITE, TFT_NAVY);
            tft.setTextDatum(textdatum_t::middle_center);
            // Tight padding (300 px) instead of card-wide so the bg wipe area
            // is small — LCD scan-out won't visibly catch a 300 px strip the
            // way it caught a 600 px one. "4000 / 4000 KB   100%" fits.
            tft.setTextPadding(300);
            tft.drawString(pbline, OM_CARD_X + OM_CARD_W / 2, OM_BAR_Y + OM_BAR_H + 16);
            tft.setTextPadding(0);
        }
    } else if (ota_state == OTA_S_FAILED && ota_err_msg[0] &&
               om_last_state != OTA_S_FAILED) {
        // Wipe the bar area, draw error message in its place. Done once per
        // state transition into FAILED.
        tft.fillRect(OM_BAR_X, OM_BAR_Y, OM_BAR_W, OM_BAR_H + 32, TFT_NAVY);
        tft.setFont(&fonts::Font2);
        tft.setTextSize(1);
        tft.setTextColor(TFT_ORANGE, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextPadding(OM_CARD_W - 40);
        tft.drawString(ota_err_msg, OM_CARD_X + OM_CARD_W / 2, OM_BAR_Y + 10);
        tft.setTextPadding(0);
    }

    // ---- Bottom button row: repaint only on state transitions. ----
    if (ota_state != om_last_state) {
        om_last_state = ota_state;
        tft.fillRect(OM_CARD_X + 30, OM_BTN_Y, OM_CARD_W - 60, OM_BTN_H, TFT_NAVY);
        switch (ota_state) {
            case OTA_S_AVAILABLE:
                omPaintButton(OM_BTN1_X, OM_BTN1_W, TFT_DARKGREEN, "UPDATE NOW");
                omPaintButton(OM_BTN2_X, OM_BTN2_W, TFT_MAROON,    "CANCEL");
                break;
            case OTA_S_DOWNLOADING:
            case OTA_S_APPLYING:
            case OTA_S_TEENSY_DOWNLOADING:
                omPaintButton(OM_BTN2_X, OM_BTN2_W, TFT_MAROON,    "CANCEL");
                break;
            case OTA_S_UPTODATE:
            case OTA_S_FAILED:
                omPaintButton(OM_BTN2_X, OM_BTN2_W, TFT_DARKGREY,  "CLOSE");
                break;
            default: break;   // CHECKING / TEENSY_WAITING / REBOOT — no button
        }
    }
    tft.setTextDatum(textdatum_t::top_left);
}

static void handleOtaModalTap(int x, int y) {
    if (y < OM_BTN_Y || y > OM_BTN_Y + OM_BTN_H) return;
    const bool inBtn1 = (x >= OM_BTN1_X && x <= OM_BTN1_X + OM_BTN1_W);
    const bool inBtn2 = (x >= OM_BTN2_X && x <= OM_BTN2_X + OM_BTN2_W);
    if (ota_state == OTA_S_AVAILABLE) {
        if (inBtn1) {
            // Teensy first (UART transfer + reboot), then CrowPanel (which
            // reboots us). If only one side needs an update, the state
            // machine skips the other side.
            if (ota_need_teensy) ota_state = OTA_S_TEENSY_DOWNLOADING;
            else                 ota_state = OTA_S_DOWNLOADING;
            ota_modal_dirty = true;
            return;
        }
        if (inBtn2) { otaCloseModal(); return; }
    } else if (ota_state == OTA_S_DOWNLOADING || ota_state == OTA_S_APPLYING ||
               ota_state == OTA_S_TEENSY_DOWNLOADING || ota_state == OTA_S_TEENSY_WAITING) {
        if (inBtn2) { ota_cancel_requested = true; return; }
    } else if (ota_state == OTA_S_UPTODATE || ota_state == OTA_S_FAILED) {
        if (inBtn2) { otaCloseModal(); return; }
    }
}

// ---------------------------------------------------------------------------
// PAGE_TOOLS — maintenance actions. Swipe right from STATUS to reach it.
// Big touch-friendly buttons, no scrolling. Currently:
//   - Check for updates  (opens OTA modal)
//   - Format SD card     (two-tap-armed; only sends SDFORMAT when NEEDS_FORMAT)
// More tools can land here as the project grows (factory reset, diagnostics
// dump, calibrate IMU zero, etc.) without re-cluttering settings.
// ---------------------------------------------------------------------------
namespace {
    constexpr int TOOLS_BTN_X = 40,  TOOLS_BTN_W = 720;
    constexpr int TOOLS_BTN_H = 90,  TOOLS_GAP   = 24;
    constexpr int TOOLS_BTN1_Y = 80;
    constexpr int TOOLS_BTN2_Y = TOOLS_BTN1_Y + TOOLS_BTN_H + TOOLS_GAP;
}

static const char* sdStatusText() {
    switch (sd_card_status) {
        case 0: return "no card";
        case 1: return "needs format";
        case 2: return "ready";
        case 3: return "error";
        case 4: return "formatting…";
    }
    return "";
}

static void drawToolsPage() {
    if (pageJustEntered) {
        tft.fillScreen(TFT_BLACK);
        // Header strip
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("TOOLS", 400, 21);
        tft.fillRect(0, 42, 800, 1, TFT_DARKGREY);
        // Footer hint
        tft.setFont(&fonts::Font2);
        tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
        // Footer also stamps the running firmware version so we can see at a
        // glance whether the last OTA actually landed.
        char foot[64];
        snprintf(foot, sizeof(foot), "firmware v%s   •   swipe right ← STATUS",
                 FIRMWARE_VERSION);
        tft.drawString(foot, 400, 460);
        tft.setTextDatum(textdatum_t::top_left);
        pageJustEntered = false;
    }

    // ---- Button 1: Check for updates ----
    const bool ota_ready = (s.internet_mode == 1 && wifi_state == WS_CONNECTED);
    const uint16_t b1_fill = ota_ready ? TFT_NAVY : TFT_DARKGREY;
    tft.fillRect(TOOLS_BTN_X, TOOLS_BTN1_Y, TOOLS_BTN_W, TOOLS_BTN_H, b1_fill);
    tft.drawRect(TOOLS_BTN_X, TOOLS_BTN1_Y, TOOLS_BTN_W, TOOLS_BTN_H, TFT_WHITE);
    tft.drawRect(TOOLS_BTN_X+1, TOOLS_BTN1_Y+1, TOOLS_BTN_W-2, TOOLS_BTN_H-2, TFT_WHITE);
    tft.setFont(&fonts::Font4);
    tft.setTextColor(TFT_WHITE, b1_fill);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.drawString("Check for updates",
                   TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN1_Y + 30);
    tft.setFont(&fonts::Font2);
    tft.setTextColor(TFT_LIGHTGREY, b1_fill);
    char b1sub[80];
    if (!ota_ready) {
        snprintf(b1sub, sizeof(b1sub),
                 "requires WiFi mode (currently %s, %s)",
                 s.internet_mode == 1 ? "WiFi"     : "Ethernet",
                 wifi_state == WS_CONNECTED ? "connected" : "not connected");
    } else {
        snprintf(b1sub, sizeof(b1sub), "current v%s", FIRMWARE_VERSION);
    }
    tft.drawString(b1sub, TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN1_Y + 65);

    // ---- Button 2: Format SD card (two-tap arming) ----
    const bool armed = sd_format_armed && (millis() - sd_format_arm_ms < 5000);
    const bool sd_busy = (sd_card_status == 4);
    const uint16_t b2_fill = sd_busy  ? TFT_DARKGREY
                            : armed   ? TFT_ORANGE
                                       : TFT_MAROON;
    tft.fillRect(TOOLS_BTN_X, TOOLS_BTN2_Y, TOOLS_BTN_W, TOOLS_BTN_H, b2_fill);
    tft.drawRect(TOOLS_BTN_X, TOOLS_BTN2_Y, TOOLS_BTN_W, TOOLS_BTN_H, TFT_WHITE);
    tft.drawRect(TOOLS_BTN_X+1, TOOLS_BTN2_Y+1, TOOLS_BTN_W-2, TOOLS_BTN_H-2, TFT_WHITE);
    tft.setFont(&fonts::Font4);
    tft.setTextColor(TFT_WHITE, b2_fill);
    tft.drawString(armed ? "TAP AGAIN TO CONFIRM" : "Format SD card",
                   TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN2_Y + 30);
    tft.setFont(&fonts::Font2);
    tft.setTextColor(TFT_LIGHTGREY, b2_fill);
    char b2sub[64];
    snprintf(b2sub, sizeof(b2sub), "SD: %s%s%s", sdStatusText(),
             (sd_total_mb > 0 ? "  •  " : ""),
             (sd_total_mb > 0 ? "" : ""));
    if (sd_total_mb > 0) {
        char ssz[24]; snprintf(ssz, sizeof(ssz), "%lu MB free / %lu MB total",
                               (unsigned long)sd_free_mb, (unsigned long)sd_total_mb);
        strncat(b2sub, ssz, sizeof(b2sub) - strlen(b2sub) - 1);
    }
    tft.drawString(b2sub, TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN2_Y + 65);
    tft.setTextDatum(textdatum_t::top_left);
}

static void handleToolsTap(int x, int y) {
    // Button 1: Check for updates
    if (x >= TOOLS_BTN_X && x <= TOOLS_BTN_X + TOOLS_BTN_W &&
        y >= TOOLS_BTN1_Y && y <= TOOLS_BTN1_Y + TOOLS_BTN_H) {
        // otaStart() handles "not in WiFi mode" + "WiFi not connected" itself,
        // showing an error in the modal. Don't gate it here — the user gets
        // explicit feedback inside the modal that way.
        otaStart();
        return;
    }
    // Button 2: Format SD card (two-tap arming)
    if (x >= TOOLS_BTN_X && x <= TOOLS_BTN_X + TOOLS_BTN_W &&
        y >= TOOLS_BTN2_Y && y <= TOOLS_BTN2_Y + TOOLS_BTN_H) {
        if (sd_card_status == 4) return;   // already formatting
        const bool wasArmed = sd_format_armed
                          && (millis() - sd_format_arm_ms < 5000);
        if (wasArmed) {
            sd_format_armed = false;
            sd_card_status  = 4;
            Serial.printf("SDFORMAT\n");
        } else {
            sd_format_armed  = true;
            sd_format_arm_ms = millis();
        }
        return;
    }
}

static void handleConfigPickerTap(int x, int y) {
    const TrackInfo& t = TRACKS[cp.track_idx];
    const int n = (int)t.n_configs;
    for (int i = 0; i < n; ++i) {
        const int by = cfgBtnY(n, i);
        if (x >= CFG_BTN_X && x < CFG_BTN_X + CFG_BTN_W &&
            y >= by         && y < by + CFG_BTN_H) {
            cp.selected = (int8_t)i;
            cp.dirty    = true;
            return;
        }
    }
    if (y >= 432 && y < 472) {
        if (x >= 20  && x < 380)                    { cancelConfigPicker();   return; }
        if (x >= 420 && x < 780 && cp.selected >= 0) { confirmConfigAndStart(); return; }
    }
}

// ---------------------------------------------------------------------------
// Status page — live read-out of everything on the data bus.
// Redraws at 5 Hz; labels drawn once on pageJustEntered, values every pass.
// Navigation: swipe right → settings.
// ---------------------------------------------------------------------------
static void formatElapsedHms(uint32_t ms, char* buf, size_t sz) {
    const uint32_t totalSec = ms / 1000;
    const uint32_t h   = totalSec / 3600;
    const uint32_t m   = (totalSec % 3600) / 60;
    const uint32_t sec = totalSec % 60;
    snprintf(buf, sz, "%02u:%02u:%02u", (unsigned)h, (unsigned)m, (unsigned)sec);
}

// ---------------------------------------------------------------------------
// Time-set page — +/- buttons for each of Y/M/D/H/M/S.
// Entered from settings by tapping ST_SET_TIME. SAVE sends SETTIME,<epoch>
// to the Teensy (UTC epoch — we convert from the locally-displayed time
// using the active timezone before sending). CANCEL returns unchanged.
// daysInMonth() and makeEpochUtc() are defined in the time helpers block
// near the top of the file.
// ---------------------------------------------------------------------------

static void drawTimeSetPage() {
    constexpr uint16_t BG  = TFT_BLACK;
    constexpr uint16_t LBL = TFT_DARKGREY;
    constexpr uint16_t VAL = TFT_WHITE;
    constexpr uint16_t HDR = TFT_CYAN;

    // Column centres for the 3 date and 3 time fields.
    constexpr int C0 = 133, C1 = 400, C2 = 667;
    constexpr int BTN_W = 160, BTN_H = 46;
    // Date row: section label at 50, field labels 66, "+" at 84, value at 140, "-" at 154
    // Time row: section label at 222, field labels 238, "+" at 256, value at 312, "-" at 326
    constexpr int DR_TOP = 50, TR_TOP = 222;    // row start y

    if (pageJustEntered) {
        tft.fillScreen(BG);
        tft.fillRect(0, 42, 800, 1, TFT_DARKGREY);

        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(VAL, BG);
        tft.setTextDatum(textdatum_t::middle_center);
        char hdr[32];
        snprintf(hdr, sizeof(hdr), "SET TIME (%s)",
                 TIMEZONES[s.timezone_idx % N_TIMEZONES].id);
        tft.drawString(hdr, 400, 21);

        // Section headers
        tft.setFont(&fonts::Font2);
        tft.setTextDatum(textdatum_t::top_left);
        tft.setTextColor(HDR, BG);
        tft.drawString("DATE", 10, DR_TOP);
        tft.drawString("TIME", 10, TR_TOP);

        // Field labels
        tft.setTextColor(LBL, BG);
        tft.setTextDatum(textdatum_t::top_center);
        tft.drawString("YEAR", C0, DR_TOP + 16);
        tft.drawString("MON",  C1, DR_TOP + 16);
        tft.drawString("DAY",  C2, DR_TOP + 16);
        tft.drawString("HOUR", C0, TR_TOP + 16);
        tft.drawString("MIN",  C1, TR_TOP + 16);
        tft.drawString("SEC",  C2, TR_TOP + 16);

        // Static +/- buttons (same appearance regardless of value)
        tft.setTextColor(VAL, TFT_DARKGREY);
        tft.setTextDatum(textdatum_t::middle_center);
        const int cols[] = {C0, C1, C2};
        for (int cx : cols) {
            // Date "+"
            tft.fillRect(cx - BTN_W/2, DR_TOP + 36, BTN_W, BTN_H, TFT_DARKGREY);
            tft.drawRect(cx - BTN_W/2, DR_TOP + 36, BTN_W, BTN_H, TFT_WHITE);
            tft.drawString("+", cx, DR_TOP + 36 + BTN_H/2);
            // Date "-"
            tft.fillRect(cx - BTN_W/2, DR_TOP + 100, BTN_W, BTN_H, TFT_DARKGREY);
            tft.drawRect(cx - BTN_W/2, DR_TOP + 100, BTN_W, BTN_H, TFT_WHITE);
            tft.drawString("-", cx, DR_TOP + 100 + BTN_H/2);
            // Time "+"
            tft.fillRect(cx - BTN_W/2, TR_TOP + 36, BTN_W, BTN_H, TFT_DARKGREY);
            tft.drawRect(cx - BTN_W/2, TR_TOP + 36, BTN_W, BTN_H, TFT_WHITE);
            tft.drawString("+", cx, TR_TOP + 36 + BTN_H/2);
            // Time "-"
            tft.fillRect(cx - BTN_W/2, TR_TOP + 100, BTN_W, BTN_H, TFT_DARKGREY);
            tft.drawRect(cx - BTN_W/2, TR_TOP + 100, BTN_W, BTN_H, TFT_WHITE);
            tft.drawString("-", cx, TR_TOP + 100 + BTN_H/2);
        }

        // Footer
        tft.setTextColor(VAL, TFT_DARKGREY);
        tft.fillRect(10,  420, 380, 50, TFT_DARKGREY);
        tft.drawRect(10,  420, 380, 50, TFT_WHITE);
        tft.drawString("CANCEL", 200, 445);
        tft.fillRect(410, 420, 380, 50, TFT_NAVY);
        tft.drawRect(410, 420, 380, 50, TFT_WHITE);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.drawString("SAVE", 600, 445);

        pageJustEntered = false;
    }

    // Value labels — redrawn on every dirty call.
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(VAL, BG);
    tft.setTextPadding(140);
    {
        char buf[8];
        // Date values (y = DR_TOP + 73, midpoint between + and - buttons)
        snprintf(buf, sizeof(buf), "%4d", ts_year);  tft.drawString(buf, C0, DR_TOP + 73);
        snprintf(buf, sizeof(buf), "%02d", ts_month); tft.drawString(buf, C1, DR_TOP + 73);
        snprintf(buf, sizeof(buf), "%02d", ts_day);   tft.drawString(buf, C2, DR_TOP + 73);
        // Time values
        snprintf(buf, sizeof(buf), "%02d", ts_hour);  tft.drawString(buf, C0, TR_TOP + 73);
        snprintf(buf, sizeof(buf), "%02d", ts_min);   tft.drawString(buf, C1, TR_TOP + 73);
        snprintf(buf, sizeof(buf), "%02d", ts_sec);   tft.drawString(buf, C2, TR_TOP + 73);
    }
    tft.setTextPadding(0);
}

static void handleTimeSetTap(int x, int y) {
    constexpr int C0 = 133, C1 = 400, C2 = 667;
    constexpr int BTN_W = 160, BTN_H = 46;
    constexpr int DR_TOP = 50, TR_TOP = 222;

    // Which column?
    const int col = (abs(x - C0) <= BTN_W/2) ? 0 :
                    (abs(x - C1) <= BTN_W/2) ? 1 :
                    (abs(x - C2) <= BTN_W/2) ? 2 : -1;

    bool changed = false;

    if (col >= 0) {
        // Date "+" (y = DR_TOP+36 to DR_TOP+82)
        if (y >= DR_TOP + 36 && y < DR_TOP + 36 + BTN_H) {
            if      (col == 0) ts_year  = (ts_year  < 2099) ? ts_year  + 1 : ts_year;
            else if (col == 1) ts_month = (ts_month < 12)   ? ts_month + 1 : 1;
            else if (col == 2) ts_day   = (ts_day < daysInMonth(ts_year, ts_month)) ? ts_day + 1 : 1;
            changed = true;
        }
        // Date "-" (y = DR_TOP+100 to DR_TOP+146)
        if (y >= DR_TOP + 100 && y < DR_TOP + 100 + BTN_H) {
            if      (col == 0) ts_year  = (ts_year  > 2020) ? ts_year  - 1 : ts_year;
            else if (col == 1) ts_month = (ts_month > 1)    ? ts_month - 1 : 12;
            else if (col == 2) ts_day   = (ts_day   > 1)    ? ts_day   - 1 : daysInMonth(ts_year, ts_month);
            changed = true;
        }
        // Time "+" (y = TR_TOP+36 to TR_TOP+82)
        if (y >= TR_TOP + 36 && y < TR_TOP + 36 + BTN_H) {
            if      (col == 0) ts_hour = (ts_hour < 23) ? ts_hour + 1 : 0;
            else if (col == 1) ts_min  = (ts_min  < 59) ? ts_min  + 1 : 0;
            else if (col == 2) ts_sec  = (ts_sec  < 59) ? ts_sec  + 1 : 0;
            changed = true;
        }
        // Time "-" (y = TR_TOP+100 to TR_TOP+146)
        if (y >= TR_TOP + 100 && y < TR_TOP + 100 + BTN_H) {
            if      (col == 0) ts_hour = (ts_hour > 0) ? ts_hour - 1 : 23;
            else if (col == 1) ts_min  = (ts_min  > 0) ? ts_min  - 1 : 59;
            else if (col == 2) ts_sec  = (ts_sec  > 0) ? ts_sec  - 1 : 59;
            changed = true;
        }
        if (changed) {
            // Clamp day if month or year changed it out of range.
            const int maxDay = daysInMonth(ts_year, ts_month);
            if (ts_day > maxDay) ts_day = maxDay;
            ts_dirty = true;
        }
    }

    // Footer buttons (y = 420-470)
    if (y >= 420 && y < 470) {
        if (x >= 10 && x < 390) {
            // CANCEL
            currentPage     = PAGE_SETTINGS;
            pageJustEntered = true;
            settingsDirty   = true;
        } else if (x >= 410 && x < 790) {
            // SAVE — the ts_* fields are LOCAL time (the user typed in local
            // wall-clock for the active timezone). Build a local-epoch first,
            // then convert to UTC before sending. The Teensy stores UTC.
            const TimeZone& tz = TIMEZONES[s.timezone_idx % N_TIMEZONES];
            const uint32_t localEpoch = makeEpochUtc(ts_year, ts_month, ts_day,
                                                     ts_hour, ts_min, ts_sec);
            const uint32_t utcEpoch   = (uint32_t)localToUtc((time_t)localEpoch, tz);
            Serial.printf("SETTIME,%lu\n", (unsigned long)utcEpoch);
            rtc_epoch       = utcEpoch;   // update locally without waiting for echo
            currentPage     = PAGE_SETTINGS;
            pageJustEntered = true;
            settingsDirty   = true;
        }
    }
}

static void drawStatusPage() {
    constexpr uint16_t BG  = TFT_BLACK;
    constexpr uint16_t LBL = TFT_DARKGREY;
    constexpr uint16_t VAL = TFT_WHITE;
    constexpr uint16_t HDR = TFT_CYAN;

    if (pageJustEntered) {
        tft.fillScreen(BG);

        // Header bar
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(VAL, BG);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("SYSTEM STATUS", 400, 21);
        tft.fillRect(0, 42, 800, 1, TFT_DARKGREY);     // horizontal rule
        tft.fillRect(400, 42, 1, 388, TFT_DARKGREY);   // vertical divider

        // Section headers (left)
        tft.setFont(&fonts::Font2);
        tft.setTextDatum(textdatum_t::top_left);
        tft.setTextColor(HDR, BG);
        tft.drawString("LINK",    10,  50);
        tft.drawString("GPS",     10, 116);
        tft.drawString("SESSION", 10, 282);
        tft.drawString("FIRMWARE",10, 360);

        // Section headers (right)
        tft.drawString("ENGINE",  410,  50);
        tft.drawString("IMU",     410,  94);
        tft.drawString("STORAGE", 410, 238);

        // Labels (left)
        tft.setTextColor(LBL, BG);
        tft.drawString("UART",    15,  68);
        tft.drawString("IP",      15,  88);
        tft.drawString("FIX",     15, 132);
        tft.drawString("SATS",    15, 152);
        tft.drawString("LAT",     15, 172);
        tft.drawString("LON",     15, 192);
        tft.drawString("SPD",     15, 212);
        tft.drawString("HDG",     15, 232);
        tft.drawString("SIG",     15, 252);
        tft.drawString("TRACK",   15, 298);
        tft.drawString("REC",     15, 318);
        tft.drawString("ELAPSED", 15, 338);
        tft.drawString("DASH",    15, 380);
        tft.drawString("TEENSY",  15, 400);
        tft.drawString("MATCH",   15, 420);

        // Labels (right)
        tft.drawString("RPM",   410,  68);
        tft.drawString("AX",    410, 112);
        tft.drawString("AY",    410, 132);
        tft.drawString("AZ",    410, 152);
        tft.drawString("GX",    410, 172);
        tft.drawString("GY",    410, 192);
        tft.drawString("GZ",    410, 212);
        tft.drawString("SD",    410, 256);
        tft.drawString("CLOUD", 410, 276);
        tft.drawString("CLOCK", 410, 310);
        tft.setTextColor(LBL, BG);
        tft.drawString("TIME",  415, 326);

        // Footer hint
        tft.setTextColor(TFT_DARKGREY, BG);
        tft.drawString("swipe ← settings    •    → tools", 10, 458);

        pageJustEntered = false;
        // Re-ask Teensy for its version each time the status page opens so the
        // value can't be silently stale from a UART hiccup hours earlier.
        Serial.println("VER?");
    }

    // Values — repainted every call (5 Hz)
    const uint32_t nowMs = millis();
    tft.setFont(&fonts::Font2);
    tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::top_left);

    constexpr int LV   = 95;   // left column value x
    constexpr int RV   = 475;  // right column value x
    constexpr int LPAD = 290;  // padding to end of left column
    constexpr int RPAD = 310;  // padding to end of right column

    tft.setTextPadding(LPAD);

    // LINK — UART freshness
    {
        const bool live = (g.last_ms != 0) && (nowMs - g.last_ms < 2000);
        tft.setTextColor(live ? TFT_GREEN : TFT_RED, BG);
        tft.drawString(live ? "LIVE" : "STALE", LV, 68);
    }
    // IP
    {
        tft.setTextColor(VAL, BG);
        tft.drawString(active_ip, LV, 88);
    }
    // GPS
    {
        char buf[8]; snprintf(buf, sizeof(buf), "%-5s", fixName(g.fix));
        const uint16_t col = (g.fix >= 3) ? TFT_GREEN : (g.fix >= 2) ? TFT_YELLOW : TFT_RED;
        tft.setTextColor(col, BG);
        tft.drawString(buf, LV, 132);
    }
    {
        char buf[8]; snprintf(buf, sizeof(buf), "%u", (unsigned)g.sats);
        tft.setTextColor(VAL, BG);
        tft.drawString(buf, LV, 152);
    }
    {
        char buf[16]; snprintf(buf, sizeof(buf), "%.6f", g.lat_deg);
        tft.setTextColor(VAL, BG);
        tft.drawString(buf, LV, 172);
    }
    {
        char buf[16]; snprintf(buf, sizeof(buf), "%.6f", g.lon_deg);
        tft.setTextColor(VAL, BG);
        tft.drawString(buf, LV, 192);
    }
    {
        char buf[12]; snprintf(buf, sizeof(buf), "%.1f mph", g.mph);
        tft.setTextColor(VAL, BG);
        tft.drawString(buf, LV, 212);
    }
    {
        char buf[12]; snprintf(buf, sizeof(buf), "%.1f deg", g.hdg_deg);
        tft.setTextColor(VAL, BG);
        tft.drawString(buf, LV, 232);
    }
    {
        char buf[8]; snprintf(buf, sizeof(buf), "%-5s", gpsStatusName(g.status));
        tft.setTextColor(gpsStatusColor(g.status), BG);
        tft.drawString(buf, LV, 252);
    }
    // SESSION
    {
        const char* tn = (active_track_name[0]) ? active_track_name : "(none)";
        tft.setTextColor(VAL, BG);
        tft.drawString(tn, LV, 298);
    }
    {
        tft.setTextColor(recording ? TFT_RED : TFT_DARKGREY, BG);
        tft.drawString(recording ? "RECORDING" : "STOPPED", LV, 318);
    }
    {
        char buf[12];
        if (recording && rec_start_ms > 0) formatElapsedHms(nowMs - rec_start_ms, buf, sizeof(buf));
        else                               strncpy(buf, "--:--:--", sizeof(buf));
        tft.setTextColor(VAL, BG);
        tft.drawString(buf, LV, 338);
    }

    tft.setTextPadding(RPAD);

    // ENGINE
    {
        char buf[8]; snprintf(buf, sizeof(buf), "%u", (unsigned)eng.rpm);
        tft.setTextColor(VAL, BG);
        tft.drawString(buf, RV, 68);
    }
    // IMU
    {
        char buf[14]; snprintf(buf, sizeof(buf), "%.2f g", imu.ax);
        tft.setTextColor(VAL, BG); tft.drawString(buf, RV, 112);
    }
    {
        char buf[14]; snprintf(buf, sizeof(buf), "%.2f g", imu.ay);
        tft.setTextColor(VAL, BG); tft.drawString(buf, RV, 132);
    }
    {
        char buf[14]; snprintf(buf, sizeof(buf), "%.2f g", imu.az);
        tft.setTextColor(VAL, BG); tft.drawString(buf, RV, 152);
    }
    {
        char buf[14]; snprintf(buf, sizeof(buf), "%.1f d/s", imu.gx);
        tft.setTextColor(VAL, BG); tft.drawString(buf, RV, 172);
    }
    {
        char buf[14]; snprintf(buf, sizeof(buf), "%.1f d/s", imu.gy);
        tft.setTextColor(VAL, BG); tft.drawString(buf, RV, 192);
    }
    {
        char buf[14]; snprintf(buf, sizeof(buf), "%.1f d/s", imu.gz);
        tft.setTextColor(VAL, BG); tft.drawString(buf, RV, 212);
    }
    // STORAGE — SD
    {
        char buf[32];
        if (sd_card_status == 0) {
            strncpy(buf, "No card", sizeof(buf));
        } else if (sd_card_status == 1) {
            if (sd_total_mb >= 1024)
                snprintf(buf, sizeof(buf), "Fmt reqd (%.1f GB)", (float)sd_total_mb / 1024.0f);
            else
                snprintf(buf, sizeof(buf), "Fmt reqd (%u MB)", (unsigned)sd_total_mb);
        } else if (sd_card_status == 2) {
            char fr[10], tot[10];
            if (sd_free_mb  >= 1024) snprintf(fr,  sizeof(fr),  "%.1f GB", (float)sd_free_mb  / 1024.0f);
            else                     snprintf(fr,  sizeof(fr),  "%u MB",   (unsigned)sd_free_mb);
            if (sd_total_mb >= 1024) snprintf(tot, sizeof(tot), "%.1f GB", (float)sd_total_mb / 1024.0f);
            else                     snprintf(tot, sizeof(tot), "%u MB",   (unsigned)sd_total_mb);
            snprintf(buf, sizeof(buf), "%s free / %s", fr, tot);
        } else if (sd_card_status == 4) {
            strncpy(buf, "Formatting...", sizeof(buf));
        } else {
            strncpy(buf, "Error", sizeof(buf));
        }
        const uint16_t sdCol = (sd_card_status == 2) ? TFT_GREEN
                             : (sd_card_status == 0) ? TFT_DARKGREY : TFT_YELLOW;
        tft.setTextColor(sdCol, BG);
        tft.drawString(buf, RV, 256);
    }
    {
        const bool hasIp   = (strcmp(active_ip, "NOT CONNECTED") != 0);
        const bool cloudOn = hasIp && s.record_cloud;
        tft.setTextColor(cloudOn ? TFT_GREEN : TFT_DARKGREY, BG);
        tft.drawString(cloudOn ? "CONNECTED" : "NOT CONNECTED", RV, 276);
    }
    // CLOCK — display in active timezone with DST applied. Source-of-truth
    // is rtc_epoch (UTC unix epoch), see TIME, line from Teensy.
    {
        char buf[28];
        if (rtc_epoch > 0) {
            const TimeZone& tz  = TIMEZONES[s.timezone_idx % N_TIMEZONES];
            const time_t    loc = utcToLocal((time_t)rtc_epoch, tz);
            struct tm*      tmv = gmtime(&loc);
            snprintf(buf, sizeof(buf), "%02d:%02d:%02d %s  %04d-%02d-%02d",
                     tmv->tm_hour, tmv->tm_min, tmv->tm_sec,
                     tzAbbrevFor((time_t)rtc_epoch, tz),
                     tmv->tm_year + 1900, tmv->tm_mon + 1, tmv->tm_mday);
            tft.setTextColor(VAL, BG);
        } else {
            strncpy(buf, "--:--:-- ---  ----------", sizeof(buf));
            tft.setTextColor(TFT_DARKGREY, BG);
        }
        tft.drawString(buf, RV, 326);
    }

    // FIRMWARE section — dash version, teensy version, mismatch indicator,
    // and (if mismatched) a resolve button on the right column.
    tft.setTextPadding(LPAD);
    {
        // DASH version (compile-time)
        tft.setTextColor(VAL, BG);
        char buf[20]; snprintf(buf, sizeof(buf), "v%s", FIRMWARE_VERSION);
        tft.drawString(buf, LV, 380);
    }
    {
        // TEENSY version (from VER, line)
        char buf[20]; snprintf(buf, sizeof(buf), "v%s", teensy_fw_version);
        const bool known = (strcmp(teensy_fw_version, "?") != 0);
        tft.setTextColor(known ? VAL : TFT_DARKGREY, BG);
        tft.drawString(buf, LV, 400);
    }
    const bool versions_known = (strcmp(teensy_fw_version, "?") != 0);
    const bool versions_match = versions_known &&
                                (strcmp(teensy_fw_version, FIRMWARE_VERSION) == 0);
    {
        const char* lbl = !versions_known ? "...waiting"
                          : versions_match ? "OK" : "MISMATCH";
        const uint16_t col = !versions_known ? TFT_DARKGREY
                           : versions_match ? TFT_GREEN : TFT_RED;
        tft.setTextColor(col, BG);
        tft.drawString(lbl, LV, 420);
    }
    tft.setTextPadding(0);

    // Right column: resolve-mismatch button (only when there is a mismatch).
    // 360 px wide button below the CLOCK block.
    if (versions_known && !versions_match) {
        const int bx = 410, by = 380, bw = 360, bh = 60;
        tft.fillRect(bx, by, bw, bh, TFT_DARKGREEN);
        tft.drawRect(bx, by, bw, bh, TFT_WHITE);
        tft.drawRect(bx+1, by+1, bw-2, bh-2, TFT_WHITE);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_DARKGREEN);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("Resolve version mismatch", bx + bw/2, by + bh/2);
        tft.setTextDatum(textdatum_t::top_left);
    } else if (pageJustEntered == false) {
        // No mismatch button — ensure any leftover paint is cleared. Cheap.
        tft.fillRect(410, 380, 360, 60, BG);
    }
}

static void handleStatusTap(int x, int y) {
    // Only the mismatch button is currently tappable on the status page.
    const bool versions_known = (strcmp(teensy_fw_version, "?") != 0);
    const bool versions_match = versions_known &&
                                (strcmp(teensy_fw_version, FIRMWARE_VERSION) == 0);
    if (!versions_known || versions_match) return;
    if (x >= 410 && x <= 770 && y >= 380 && y <= 440) {
        // Tap on the green button — open the OTA modal. Today that only
        // updates the CrowPanel; once Phase 2b lands, it'll forward the
        // Teensy hex over UART so a single update brings both into sync.
        otaStart();
    }
}

// ---------------------------------------------------------------------------
// Boot — same V3.0 init sequence that worked in PanelTest.ino.
// ---------------------------------------------------------------------------
void setup() {
    // Default ESP32 Serial RX buffer is 256 bytes; at 921 600 baud that's only
    // ~2.7 ms of slack. Bump to 4 KB BEFORE begin() so the bigger buffer is
    // allocated up front — keeps us safe across slow draw frames + WiFi work.
    Serial.setRxBufferSize(4096);
    Serial.begin(921600);
    delay(800);
    Serial.printf("\n=== racecar-35 dash crowpanel boot, firmware v%s ===\n",
                  FIRMWARE_VERSION);

    // Silence the ESP-IDF WiFi log output — it would otherwise spam Serial
    // (= UART0 = the Teensy bridge) once WiFi.begin() runs. Without this,
    // the Teensy parser sees junk lines and we get a noisy debug log.
    esp_log_level_set("*",       ESP_LOG_NONE);
    esp_log_level_set("wifi",    ESP_LOG_NONE);
    esp_log_level_set("wifi_init", ESP_LOG_NONE);

    pinMode(38, OUTPUT);
    digitalWrite(38, LOW);
    Wire.begin(19, 20);

    Out.reset();
    Out.setMode(IO_OUTPUT);
    Out.setState(IO0, IO_LOW);
    Out.setState(IO1, IO_LOW);
    delay(20);
    Out.setState(IO0, IO_HIGH);
    delay(100);
    Out.setMode(IO1, IO_INPUT);
    Serial.println("PCA9557 init ok");

    tft.begin();
    tft.fillScreen(TFT_BLACK);
    tft.setTextSize(1);                 // explicit default; PanelTest's old size-3 leak made
                                         // the dash's first frame draw labels at size 3.
    delay(200);

    ledcSetup(1, 300, 8);
    ledcAttachPin(TFT_BL, 1);
    ledcWrite(1, 0);
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, LOW);
    delay(500);
    digitalWrite(TFT_BL, HIGH);
    Serial.println("backlight on");

    loadSettings();
    Serial.printf("settings: rpm[%u..%u] alerts=%d a1=%uRPM/%uHz/%s aM=%uRPM/%uHz/%s\n",
                  s.rpm_min, s.rpm_max, (int)s.alerts_enabled,
                  s.alert1_rpm, s.alert1_hz, PALETTE_NAMES[s.alert1_color_idx],
                  s.alertmax_rpm, s.alertmax_hz, PALETTE_NAMES[s.alertmax_color_idx]);

    // Tell Teensy our active timezone so it has the info for SD filenames /
    // cloud metadata. Display still uses UTC from the wire and applies the
    // offset locally; the Teensy doesn't currently act on this.
    Serial.printf("TZ,%s\n", TIMEZONES[s.timezone_idx].id);

    // Push the cloud/record config so the Teensy knows where to POST etc.
    sendCfgToTeensy();

    pageJustEntered = true;
    // Ask Teensy for its firmware version. If Teensy booted earlier we missed
    // its VER,teensy,... line on its setup(). This nudge prompts a fresh emit.
    Serial.println("VER?");
    Serial.println("dash UI ready — listening on UART0");
}

void loop() {
    pumpUart();
    handleTouch();
    wifiTick();   // WiFi state machine + one-shot NTP push to Teensy (1 Hz tick)

    // Expire SD format arm state after 5 s so the button reverts to red.
    if (sd_format_armed && millis() - sd_format_arm_ms >= 5000) {
        sd_format_armed = false;
        settingsDirty   = true;
    }

    // Dash redraws at ~50 Hz for crisp alert flashes. Settings normally
    // redraws lazily (only when settingsDirty), but during a finger drag
    // we render every loop iteration to keep scroll smooth.
    static uint32_t lastDraw = 0;
    const uint32_t now = millis();
    if (currentPage == PAGE_DASH) {
        if (now - lastDraw >= 20) { lastDraw = now; drawDashPage(); }
    } else if (currentPage == PAGE_SETTINGS) {
        // Cap scroll redraws to ~30 Hz so the LCD has time to scan a full
        // clean frame between renders. Without this cap we re-render on
        // every loop iteration during a drag (~200 Hz), and each render's
        // strip-wipe + per-row redraw takes long enough that the LCD
        // catches half-rendered intermediate states.
        if ((settingsDirty && now - lastDraw >= 33) || now - lastDraw >= 200) {
            lastDraw = now;
            drawSettingsPage();
        }
    } else if (currentPage == PAGE_NUM_KB) {
        if (kb.dirty || pageJustEntered) { lastDraw = now; drawNumKeyboard(); }
    } else if (currentPage == PAGE_TEXT_KB) {
        if (kb.dirty || pageJustEntered) { lastDraw = now; drawTextKeyboard(); }
    } else if (currentPage == PAGE_TRACK_PICKER) {
        if ((tp.dirty && now - lastDraw >= 33) || pageJustEntered) {
            lastDraw = now;
            drawTrackPicker();
        }
    } else if (currentPage == PAGE_CONFIG_PICKER) {
        if (cp.dirty || pageJustEntered) { lastDraw = now; drawConfigPicker(); }
    } else if (currentPage == PAGE_STATUS) {
        if (pageJustEntered || now - lastDraw >= 200) { lastDraw = now; drawStatusPage(); }
    } else if (currentPage == PAGE_TIME_SET) {
        if (pageJustEntered || ts_dirty) { ts_dirty = false; lastDraw = now; drawTimeSetPage(); }
    } else if (currentPage == PAGE_WIFI_SCAN) {
        pollWifiScanner();   // checks WiFi.scanComplete(), flips state when done
        if (pageJustEntered || wifi_scan_dirty || now - lastDraw >= 250) {
            lastDraw = now;
            drawWifiScannerPage();
        }
    } else if (currentPage == PAGE_TOOLS) {
        // Redraw frequently enough that the SD-format armed timer counts down
        // visibly and the OTA gating subtext (WiFi connected? mode?) reflects
        // changes promptly. Cheap — only two buttons painted.
        if (pageJustEntered || now - lastDraw >= 250) { lastDraw = now; drawToolsPage(); }
    } else if (currentPage == PAGE_OTA) {
        otaTick();
        if (pageJustEntered || ota_modal_dirty || now - lastDraw >= 250) {
            lastDraw = now;
            drawOtaModal();
        }
    } else if (currentPage == PAGE_UPLOAD) {
        if (pageJustEntered || upload_modal_dirty || now - lastDraw >= 250) {
            lastDraw = now;
            drawUploadModal();
        }
        // If DONE landed, hold modal for 1 s with the status banner, then close.
        if (upload_result_msg[0] != '\0' &&
            (int32_t)(millis() - upload_last_draw_ms) >= 1000) {
            closeUploadModal();
        }
    }
}
