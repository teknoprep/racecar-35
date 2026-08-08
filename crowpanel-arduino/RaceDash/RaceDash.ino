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
#include <TAMC_GT911.h>   // GT911 touch on Wire/I2C_NUM_0 (vendor V3.0 approach)
#include <WiFi.h>          // ESP32-S3 built-in WiFi (NTP, OTA HTTPS, future cloud upload)
#include <WiFiClientSecure.h>   // HTTPS to GitHub for manifest + firmware download
#include <HTTPClient.h>         // wraps WiFiClientSecure with a simple GET/POST API
#include <Update.h>             // partition-swap OTA writer
#include <esp_log.h>       // for esp_log_level_set("wifi", ESP_LOG_NONE)
#include <esp_system.h>    // esp_reset_reason() for BLE crash forensics
#include <mbedtls/sha256.h>     // OTA artifact integrity check vs manifest sha256

// Compile-time firmware version. Bump via the release process when shipping
// a new build (eventually automated by scripts/release.sh + GitHub Action).
// Settings page displays it; "Check for updates" compares to manifest.json
// from https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/.
#define FIRMWARE_VERSION "0.1.138"

#include <Preferences.h>
#include <time.h>
#include <driver/i2c.h>           // I2C_NUM_1 for Touch_GT911 config
#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <lgfx/v1/platforms/esp32s3/Panel_RGB.hpp>
#include <lgfx/v1/platforms/esp32s3/Bus_RGB.hpp>
#include "board_config.h"   // per-panel RGB pin map + timing (DASH_BOARD 7|5)
#include "obd_ble.h"       // Bluetooth-LE OBD-II (ELM327) client for sensor_type==2
#include "zdeflate.h"      // tiny raw-DEFLATE for compressed uploads (v0.1.127 zblocks)

// ---------------------------------------------------------------------------
// Forward type decls — Arduino IDE auto-injects function prototypes at the
// top of the translation unit, so any type used in a function signature must
// be visible BEFORE the first function. Real definitions follow below.
// ---------------------------------------------------------------------------
struct SfGate;   // S/F crossing gate (v0.1.130) — used by buildSfGate/sfGateCross

enum SettingId : uint8_t {
    // Internet block at the very top — picks whether all internet-bound
    // operations route via Teensy/W5500 (Ethernet) or CrowPanel/ESP32-S3 WiFi.
    // SSID/pass/status rows are hidden when Mode=Ethernet.
    ST_BRIGHTNESS = 0,            // LCD backlight slider — top of the settings list
    ST_INET_MODE,
    ST_WIFI_SSID, ST_WIFI_PASS, ST_WIFI_STATUS,
    ST_RPM_MIN, ST_RPM_MAX, ST_RPM_DIV, ST_RPM_SMOOTH, ST_RPM_SPIKE, ST_GPS_FILTER, ST_LAP_OVERLAY, ST_ALERTS,
    ST_A1_RPM, ST_A1_COL, ST_A1_HZ,
    ST_AM_RPM, ST_AM_COL, ST_AM_HZ,
    // Coolant temp warn-color + oil PSI warn-color. Each has a master
    // show toggle, a threshold (°F or PSI), and the warning palette colour.
    ST_SHOW_TEMP, ST_TEMP_WARN_F, ST_TEMP_WARN_COL,
    ST_SHOW_PSI,  ST_PSI_WARN_PSI, ST_PSI_WARN_COL,
    // Battery/alternator voltage (BT dongle ATRV or MS3 CAN bat). Display +
    // warning are gated on the ENGINE RUNNING (parked ignition-on reads ~12.4 V
    // — that's normal, not a failing alternator).
    ST_SHOW_VOLT, ST_VOLT_WARN, ST_VOLT_WARN_COL,
    ST_COACH_SHOW,                // show the AI coach checklist button on the dash
    // Sensor data source (Direct / MegaSquirt) + AFR display (MS3 mode only).
    // AFR has both a "too rich" (low) and "too lean" (high) warn threshold;
    // either fires the same colour.
    ST_SENSOR_TYPE,
    ST_SHOW_AFR, ST_AFR_WARN_LO, ST_AFR_WARN_HI, ST_AFR_WARN_COL,
    ST_REC_SD, ST_REC_CLOUD,
    ST_CL_HOST, ST_CL_PORT, ST_CL_PROTO,
    ST_CL_AUTH_USER, ST_CL_AUTH_PASS,
    ST_AUTO_TRACK,
    ST_AUTO_START, ST_AUTO_START_MPH, ST_AUTO_START_SEC,   // auto-start recording + threshold + hold time
    ST_TIMEZONE,    // ENUM: cycle through TIMEZONES[]
    ST_GPS_BAUD,    // ENUM: GPS UART baud (sent to Teensy as CFG,gpsbaud,<n>)
    ST_GPS_STATUS,  // INFO: Teensy's GPSBAUD report (locked baud + OK/NO DATA)
    ST_DEBUG_LOG,   // TOGGLE: write on-SD .dbg diagnostic logs (CFG,dbg_on)
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

// Forward-declared so any auto-prototype generated by the Arduino IDE for
// functions that take a `const TimeZone&` parameter resolves cleanly. The
// full struct definition lives further down with the timezone tables.
struct TimeZone;

// WiFi state machine values (full definition lives next to its tick function
// further down). Forward-declared here because the Arduino IDE / arduino-cli
// auto-injects function prototypes at the top of the TU — anything used in
// a function signature must be visible before the first function.
enum WifiState : uint8_t { WS_OFF = 0, WS_CONNECTING, WS_CONNECTED, WS_FAILED };

// Forward decls for the dash-initiated upload flow. The enum / structs need to
// be visible to the auto-prototype scanner that runs on the first function in
// the TU. wifiConnectedNow() is a helper for ufDoPost so it doesn't have to
// reach into the file-scope `wifi_state` global from above its definition.
enum UploadFlowState : uint8_t {
    UF_IDLE = 0,
    UF_LISTING,         // Q,LIST sent, awaiting Q,FILE / Q,END
    UF_FETCH_HEAD,      // Q,GET sent, awaiting Q,DATA
    UF_STREAMING,       // receiving Q,L lines into the PSRAM file buffer
    UF_POSTING,         // whole file staged; writing it to the TLS socket
    UF_STREAM_FINISH,   // body sent, draining HTTP response from socket
    UF_DELETING,        // Q,DEL sent, awaiting Q,DEL,OK
    UF_RETRY_WAIT,      // stream failed once; brief pause, then re-Q,GET same file
    UF_DONE,
};
enum SessionsListState : uint8_t {
    SL_IDLE = 0, SL_LISTING, SL_DELETING,
};
static bool wifiConnectedNow();

// Single key descriptor for both numeric and text keyboards.
struct KbKey {
    int16_t  x, y, w, h;
    const char* label;
    // action: visible char (0..0x7F) is inserted as that char into editBuf.
    // Special codes: 0x01='\b' backspace, 0x02='C' clear, 0x03='D' done,
    //                0x04='X' cancel, 0x05=' ' space.
    char action;
};

#define TFT_BL DASH_PIN_BL   // backlight pin from board_config.h (GPIO 2 on all sizes)

PCA9557 Out;
Preferences prefs;

// GT911 capacitive touch — driven on Wire (I2C_NUM_0), exactly like the
// proven vendor V3.0 demo. We do NOT use LovyanGFX's built-in Touch_GT911:
// that runs on I2C_NUM_1 and its init steals the GPIO 19/20 matrix routing
// from Wire, killing all I2C reads. INT=-1, RST=-1 (board handles reset),
// address auto-detected (0x5D or 0x14) in setup().
static TAMC_GT911 ts(DASH_TOUCH_SDA, DASH_TOUCH_SCL, (uint8_t)-1, (uint8_t)-1, 800, 480);

// ---------------------------------------------------------------------------
// LovyanGFX driver — RGB display only. Panel pin map + timing come from
// board_config.h (selected by DASH_BOARD). Touch is GT911 on Wire, set up
// separately in setup() so LovyanGFX never initialises I2C_NUM_1.
// ---------------------------------------------------------------------------
class LGFX : public lgfx::LGFX_Device {
public:
    lgfx::Bus_RGB     _bus_instance;
    lgfx::Panel_RGB   _panel_instance;
    lgfx::Light_PWM   _light_instance;
    // No Touch_GT911 here — touch is handled separately by TAMC on Wire so
    // LovyanGFX never initialises I2C_NUM_1 and never steals the touch pins.

    LGFX(void) {
        {
            auto cfg = _panel_instance.config();
            cfg.memory_width  = 800; cfg.memory_height = 480;
            cfg.panel_width   = 800; cfg.panel_height  = 480;
            cfg.offset_x = 0;        cfg.offset_y = 0;
            _panel_instance.config(cfg);
        }
#if DASH_IS_ADVANCE
        {
            // Advance: explicitly place the RGB framebuffer in PSRAM (vendor sets
            // config_detail.use_psram = 1). 800x480x16bpp = 768 KB, far beyond the
            // ~512 KB internal SRAM, so it MUST live in the N16R8's OPI PSRAM.
            auto cfg = _panel_instance.config_detail();
            cfg.use_psram = 1;
            _panel_instance.config_detail(cfg);
        }
#endif
        {
            auto cfg = _bus_instance.config();
            cfg.panel = &_panel_instance;
            cfg.pin_d0  = DASH_PIN_D0;  cfg.pin_d1  = DASH_PIN_D1;  cfg.pin_d2  = DASH_PIN_D2;
            cfg.pin_d3  = DASH_PIN_D3;  cfg.pin_d4  = DASH_PIN_D4;
            cfg.pin_d5  = DASH_PIN_D5;  cfg.pin_d6  = DASH_PIN_D6;  cfg.pin_d7  = DASH_PIN_D7;
            cfg.pin_d8  = DASH_PIN_D8;  cfg.pin_d9  = DASH_PIN_D9;  cfg.pin_d10 = DASH_PIN_D10;
            cfg.pin_d11 = DASH_PIN_D11; cfg.pin_d12 = DASH_PIN_D12; cfg.pin_d13 = DASH_PIN_D13;
            cfg.pin_d14 = DASH_PIN_D14; cfg.pin_d15 = DASH_PIN_D15;
            cfg.pin_henable = DASH_PIN_HENABLE;
            cfg.pin_vsync   = DASH_PIN_VSYNC;
            cfg.pin_hsync   = DASH_PIN_HSYNC;
            cfg.pin_pclk    = DASH_PIN_PCLK;
            cfg.freq_write  = DASH_FREQ_WRITE;
            cfg.hsync_polarity    = DASH_HSYNC_POLARITY;
            cfg.hsync_front_porch = DASH_HSYNC_FRONT_PORCH;
            cfg.hsync_pulse_width = DASH_HSYNC_PULSE_WIDTH;
            cfg.hsync_back_porch  = DASH_HSYNC_BACK_PORCH;
            cfg.vsync_polarity    = DASH_VSYNC_POLARITY;
            cfg.vsync_front_porch = DASH_VSYNC_FRONT_PORCH;
            cfg.vsync_pulse_width = DASH_VSYNC_PULSE_WIDTH;
            cfg.vsync_back_porch  = DASH_VSYNC_BACK_PORCH;
            cfg.pclk_active_neg = DASH_PCLK_ACTIVE_NEG;
            cfg.de_idle_high    = DASH_DE_IDLE_HIGH;
            cfg.pclk_idle_high  = DASH_PCLK_IDLE_HIGH;
            _bus_instance.config(cfg);
            _panel_instance.setBus(&_bus_instance);
        }
#if !DASH_IS_ADVANCE
        {
            // PWM backlight on GPIO 2 (Basic V3.0 panels). The Advance has no
            // PWM backlight pin — it drives brightness over I2C (0x30), so we
            // attach no Light_PWM there (GPIO 2 may be used for something else).
            auto cfg = _light_instance.config();
            cfg.pin_bl = DASH_PIN_BL;
            _light_instance.config(cfg);
            _panel_instance.light(&_light_instance);
        }
#endif
        setPanel(&_panel_instance);
    }
};

LGFX tft;

// ---------------------------------------------------------------------------
// Sprite back-buffers for the dynamic dash page regions.
//
// Why: the CrowPanel V3.0 RGB Panel keeps a continuous parallel-RGB scan over
// its framebuffer in PSRAM. When we draw text/shapes directly into that
// framebuffer, the LCD can scan a half-written element and the user sees a
// tear. At 25 Hz telemetry that tear is visible 25 times per second on the
// big-area elements (speed digit, RPM bar, sensor lines).
//
// Fix: render each fast-changing region into its own PSRAM-backed sprite,
// then pushSprite() the whole sprite to the framebuffer in one DMA blit.
// The blit is fast (sub-ms for these sizes on the S3) so the LCD scan rarely
// catches a sprite mid-push, and the user-visible result is a clean atomic
// update of each element. Each sprite is allocated once at boot and reused
// for the life of the process.
//
// Sized generously so even Font7-size-4 "888" (full speed digit) or a long
// "TEMP: 250\xB0F" line fits without clipping.
// ---------------------------------------------------------------------------
namespace {
    LGFX_Sprite spr_rpm_bar(&tft);     // inside the RPM bar border
    LGFX_Sprite spr_rpm_text(&tft);    // RPM number under the bar
    LGFX_Sprite spr_speed(&tft);       // huge centred MPH digits
    LGFX_Sprite spr_temp(&tft);        // "TEMP: ..." line
    LGFX_Sprite spr_psi(&tft);         // "PSI:  ..." line
    LGFX_Sprite spr_afr(&tft);         // "AFR:  ..." line (MS3 mode only)
    LGFX_Sprite spr_rec_badge(&tft);   // REC ● N  /  queue: N  /  REC ? no ack
    LGFX_Sprite spr_fix(&tft);         // FIX value (right column)
    LGFX_Sprite spr_sats(&tft);        // SATS value (right column)
    LGFX_Sprite spr_gps(&tft);         // GPS status value (right column)
    LGFX_Sprite spr_pred(&tft);        // predictive lap time (middle column)
    LGFX_Sprite spr_lap(&tft);         // last lap time (middle column)
    LGFX_Sprite spr_delta(&tft);       // predictive delta vs best lap (middle column)
    LGFX_Sprite spr_track_name(&tft);  // active track name under TRACK btn
    LGFX_Sprite spr_recbtn(&tft);      // START/STOP button face
    LGFX_Sprite spr_upbtn(&tft);       // manual queue-upload button
    LGFX_Sprite spr_settings(&tft);    // full settings-body back-buffer (anti-flicker)
}
static bool   dash_sprites_ready = false;

static void setupDashSprites() {
    auto mk = [](LGFX_Sprite& s, int w, int h) -> bool {
        s.setPsram(true);          // request PSRAM backing store
        s.setColorDepth(16);       // match RGB565 panel format
        return s.createSprite(w, h) != nullptr;
    };
    // Inner-of-border for the RPM bar so the static white border outline
    // drawn directly on the framebuffer at pageJustEntered isn't clobbered.
    dash_sprites_ready =
        mk(spr_rpm_bar,    756, 76)  &&
        mk(spr_rpm_text,   110, 26)  &&
        mk(spr_speed,      360, 200) &&    // covers Font7-size-4 (≈192 px tall)
        mk(spr_temp,       240, 38)  &&
        mk(spr_psi,        240, 38)  &&
        mk(spr_afr,        240, 38)  &&
        mk(spr_rec_badge,  220, 22)  &&
        mk(spr_fix,        110, 24)  &&
        mk(spr_sats,        60, 24)  &&
        mk(spr_gps,        110, 24)  &&
        mk(spr_pred,       150, 28)  &&
        mk(spr_lap,        150, 28)  &&
        mk(spr_delta,      150, 28)  &&
        mk(spr_track_name, 320, 22)  &&
        mk(spr_recbtn,     160, 70)  &&
        mk(spr_upbtn,      180, 70);
    if (dash_sprites_ready) Serial.println("dash sprites: ok");
    else                    Serial.println("WARNING: dash sprite alloc failed \u2014 dash will fall back to direct draw");
}

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
static char     sd_err_hex[8]    = "";   // last SdFat err code (hex from SD,NONE,<errcode>)

// Active session state — updated from SD,REC,<0|1>,<file>,<samples> lines.
// The dash uses this to show a REC indicator + a live sample count.
static bool     sd_session_active  = false;
static char     sd_session_file[80] = "";
static uint32_t sd_session_samples = 0;

// CAN sniffer state — updated from CANSNIFF,<0|1>,<file>,<frames> lines.
// Toggled from the Tools page; the Teensy logs raw frames to SD.
static bool     cansniff_active    = false;
static char     cansniff_file[80]  = "";
static uint32_t cansniff_frames    = 0;

// CAN health — updated from CANDIAG,<frames/s>,<total>,<base_hits>,<dup%>,<ACK_ERR>
// emitted by the Teensy at 1 Hz. Shown live on the Tools page so the CAN bus
// can be diagnosed in the car without a laptop. A healthy MS3 link reads
// ~50 fps, dup~0%, ACK_ERR=0; a retransmit storm reads ~3600 fps, dup~100%,
// ACK_ERR=1; a dead bus reads 0 fps.
static uint32_t candiag_fps     = 0;
static uint32_t candiag_total   = 0;
static uint8_t  candiag_base    = 0;
static uint8_t  candiag_dup_pct = 0;
static bool     candiag_ack_err = false;
static uint8_t  candiag_tx_err  = 0;   // Teensy CAN TX error counter (128+ = TX dead)
static uint8_t  candiag_rx_err  = 0;   // Teensy CAN RX error counter
static uint8_t  candiag_txtest  = 0;   // TX self-test: 0=untested,1=PASS,2=FAIL
static uint32_t candiag_ms      = 0;   // millis() of last CANDIAG line

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
static char     ota_sha256[80]         = "";   // expected sha256 from manifest (crowpanel)
static char     ota_teensy_sha256[80]  = "";   // expected sha256 from manifest (teensy)
// Teensy OTA uses two visible phases: WiFi download into PSRAM, then
// ACK-paced UART transfer to the Teensy/FlasherX. Keep separate counters so
// the modal doesn't misleadingly show one bar that suddenly slows at 50%.
static uint32_t ota_t_dl_total         = 0;
static uint32_t ota_t_dl_done          = 0;
static uint32_t ota_t_tx_total         = 0;
static uint32_t ota_t_tx_done          = 0;
static uint8_t  ota_return_page        = 0;     // page to restore on Close
static bool     ota_modal_dirty        = false;
static bool     ota_cancel_requested   = false;
// Teensy-side update state (parallel manifest entry)
static char     ota_teensy_version[16] = "";
static char     ota_teensy_url[200]    = "";
static uint32_t ota_teensy_size        = 0;
static bool     ota_need_teensy        = false;
static bool     ota_need_crowpanel     = false;
static bool     ota_teensy_commit_seen = false;   // saw FW,COMMITTING from FlasherX

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
static char     upload_result_msg[180]   = "";       // banner after DONE: status + optional reason

// Cloud state — updated from CLD,<live_ok>,<queue_depth> lines.
static bool     cloud_live_ok      = false;
static uint32_t cloud_queue_depth  = 0;

// ---------------------------------------------------------------------------
// WiFi <-> BLE radio TIME-SHARING arbiter. WiFi and Bluetooth may NEVER run at
// the same time on this core (arduino-esp32 2.0.14 / IDF 4.4): enabling the BT
// controller with WiFi up aborts in coex_core_enable, and the WiFi ppTask can
// panic when BT flips coex state under it (both decoded from live backtraces;
// confirmed on the bench: WiFi off -> BT works). Ownership:
//   NET_WIFI    - WiFi may run (wifiTick free to connect). BLE fully down.
//   NET_TO_WIFI - BLE draining (disconnect + NimBLE deinit on the obd task);
//                 WiFi stays hard-off until obd::isDown(), then -> NET_WIFI.
//   NET_BT      - BLE owns the radio; wifiTick holds WiFi hard-off.
// POLICY (user-chosen): BT owns the radio ONLY WHILE RECORDING (that's when
// coolant matters), plus while the user is on the sensor/scan pages pairing.
// All paddock time = WiFi: uploads, OTA, NTP just work, no manual handover.
// netOwnerTick watches the `recording` edges: REC start -> radio to BT +
// dongle reconnect; REC stop -> BLE full shutdown, WiFi back within seconds.
// Tapping UPLOAD while BT somehow owns the radio still auto-hands to WiFi.
// ---------------------------------------------------------------------------
#define NET_WIFI    0
#define NET_TO_WIFI 1
#define NET_BT      2
static uint8_t  net_owner          = NET_WIFI;
static bool     net_pending_upload = false;   // start upload flow once WiFi connects
static bool     net_pending_sel    = false;   // pending upload is "selected files" (Sessions page)
static uint32_t net_owner_ms       = 0;

// Firmware version tracking. Dash version is compile-time (FIRMWARE_VERSION).
// Teensy version arrives via VER,teensy,<ver> on Teensy boot, and on demand
// when the dash sends "VER?\n" (dash boot + status page entry).
static char     teensy_fw_version[16] = "?";
static char     teensy_reset_reason[24] = "";   // Teensy's last reset cause (RST,teensy,..)

// Two-tap arming for the SD format action in settings.
static bool     sd_format_armed  = false;
static uint32_t sd_format_arm_ms = 0;

// Settings page dirty flag — declared here (before the UART parsers) so
// parseSdLine() can set it on status change. The settings page checks it
// each draw cycle to decide whether to repaint.
static bool settingsDirty = true;
static char gps_status_buf[48] = "";   // Teensy GPSBAUD report (settings INFO row);
                                       // declared early: parseLine() writes it.

// RTC epoch received from the Teensy's TIME, line (0 = not yet received).
static uint32_t rtc_epoch = 0;

// Time-set page editing state — populated from rtc_epoch on entry.
static int  ts_year  = 2025, ts_month = 1, ts_day  = 1;
static int  ts_hour  = 0,    ts_min   = 0, ts_sec  = 0;
static bool ts_dirty = false;   // set by +/- taps; triggers value redraw

// ---------------------------------------------------------------------------
// Lap timer — GPS-proximity start/finish detection + distance integration.
// ---------------------------------------------------------------------------
// Predictive / live-delta engine. We record the session's BEST lap as a
// "ghost": a table of elapsed-time-vs-distance-into-the-lap, bucketed by
// distance. The live delta then compares the current lap's elapsed time to the
// ghost at the SAME distance, and the predicted final = best_lap + that delta.
// This is position-anchored (not a crude time-ratio), so it tracks where you
// gained/lost time and converges to the real result as you cross the line.
constexpr float LAP_BUCKET_MI = 0.0050f;     // ~8 m delta resolution per bucket
constexpr int   LAP_BUCKETS   = 1200;        // 1200 * 0.005 mi = 6.0 mi max lap
// Bucket tables live OUTSIDE LapTimer so `lapTimer = LapTimer{}` stays a cheap
// scalar reset — a ~10 KB temporary on the loopTask stack would risk overflow.
static uint32_t lapCurBt[LAP_BUCKETS];       // elapsed ms at each dist bucket, current lap
static uint32_t lapRefBt[LAP_BUCKETS];       // same, for the reference (best) lap

struct LapTimer {
    bool     active          = false;
    int      track_idx       = -1;    // which TRACKS[] entry we're timing at
    uint32_t lap_start_ms    = 0;
    float    dist_miles      = 0.0f;  // odometer this lap (mph * dt)
    uint32_t prev_gps_ms     = 0;
    bool     left_start      = false; // true once car has moved away from S/F (radius fallback only)
    bool     timing_started  = false; // true after first clean S/F crossing (guards partial first lap)
    int      lap_number      = 0;     // current lap being driven (0 = out/before first crossing)
    float    prev_lat        = 0.0f;  // previous GPS point (for line-crossing test)
    float    prev_lon        = 0.0f;
    bool     have_prev       = false;
    uint32_t prev_fix_ms     = 0;     // millis() of the PREVIOUS fix (crossing interpolation)
    float    sf_dir          = 0.0f;  // learned direction of travel through the S/F plane
    bool     have_sf_dir     = false; // set at the first crossing

    uint32_t last_lap_ms     = 0;     // most recent completed lap time (0 = none)
    float    last_lap_dist   = 0.0f;  // distance of last completed lap (miles)
    uint32_t best_lap_ms     = 0;     // fastest completed lap this session (0 = none)
    float    best_lap_dist   = 0.0f;  // distance of the best lap (miles)

    int      cur_bucket      = 0;     // # of distance buckets filled this lap (index into lapCurBt)
    bool     ref_valid       = false; // a reference (best) ghost lap has been captured
    int      ref_buckets     = 0;     // # of valid buckets in lapRefBt[]
};
static LapTimer lapTimer;

// Finish-line lap-time popup: when a lap completes, its time is shown HUGE
// over everything below the RPM bar for s.lap_overlay_s seconds (0 = off).
// The RPM warning flash takes precedence (popup hides while the bg flashes).
static uint32_t lap_overlay_lap_ms   = 0;   // completed lap time to display
static uint32_t lap_overlay_until_ms = 0;   // popup deadline (0 = inactive)
static int      lap_overlay_lapn     = 0;   // completed lap number
static bool     lap_overlay_is_best  = false;

// ---------------------------------------------------------------------------
// S/F CROSSING GATE (v0.1.130). The old test asked "does the path segment
// prev->cur INTERSECT the stored S/F segment?" — but a hand-drawn line across
// the stripe is only ~10 m long, so passing 5 m wide of it (different racing
// line, track width, GPS error) MISSED the lap entirely. Measured with the
// real firmware on real Summit geometry: -4/-6/-8 m offset = 0 of 5 laps
// detected, +4 m at 60 mph = flaky ("sometimes it works").
//
// Replaced by the standard lap-timer construction: treat the S/F as an
// infinite PLANE through the line's midpoint, track the SIGNED DISTANCE to it
// every fix, and declare a crossing when that distance changes sign — gated
// laterally (|offset along the line| <= SF_GATE_HALF_M) so a parallel road or
// a distant part of the circuit can't trigger it. The crossing INSTANT is
// then INTERPOLATED between the two straddling fixes, so lap time no longer
// quantizes to the 40 ms sample grid or depends on where samples landed.
constexpr float    SF_GATE_HALF_M = 25.0f;   // lateral half-width of the gate (50 m total)
constexpr float    LAP_RADIUS_KM = 0.075f;   // 75 m start/finish detection radius
constexpr uint32_t MIN_LAP_MS    = 15000;    // minimum lap time before a crossing counts
constexpr int32_t  DELTA_SAME_MS = 50;       // |delta| <= this -> "same pace" (white)

// Recording state — toggled by the START/STOP button on the dash. Sent to
// the Teensy as `REC,<0|1>\n` on UART0 TX. The Teensy doesn't read this
// yet (one-way bytes harmlessly accumulate in its Serial3 RX buffer);
// when the SD-card / cloud-streaming feature is implemented Teensy-side
// it just starts consuming the line.
// Auto-start dwell: ms remaining before a held over-threshold speed opens a
// session (0 = not counting). Drives the START button's "AUTO n" countdown so
// the driver can SEE the arming instead of wondering why it hasn't fired.
static uint32_t autostart_pending_ms = 0;

static bool recording         = false;
static int  last_track_idx    = -1;        // TRACKS[] index of the last confirmed track (-1 = none saved)
static char active_track_name[52] = "";    // full display name (may include config, e.g. "Mid-Ohio Full Course")
static uint8_t active_cfg_idx     = 0;     // selected config of last_track_idx (NVS "lcfg"); 0 = primary

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
  // Manual queue-upload button. Shows only when there is a backlog of
  // sessions waiting in /queue/ AND we aren't currently recording. Sits in
  // the empty band between the START button and the big speed digit.
  constexpr int UPBTN_X = 210;
  constexpr int UPBTN_Y = 155;
  constexpr int UPBTN_W = 180;
  constexpr int UPBTN_H = 70;
  // Coach checklist button — directly under UPLOAD. Hidden while recording
  // (nothing to review mid-session) and whenever the list is empty.
  constexpr int CHBTN_X = UPBTN_X;
  constexpr int CHBTN_Y = UPBTN_Y + UPBTN_H + 10;
  constexpr int CHBTN_W = UPBTN_W;
  constexpr int CHBTN_H = 60;
  // Speed sits on the right side of the screen (out of the way of the
  // START/STOP button on the left). Drop the decimal at >=100 mph so a
  // 3-digit number stays narrow enough to fit the 400-px bg pad cleanly
  // without clipping at the right edge.
  // Speed is integer MPH only (no decimal), so the layout never has to budget
  // for a decimal point that vanishes at >= 100 mph. With 3 digits max we can
  // pull the centre slightly inward from the right panel edge for breathing
  // room around the largest values.
  constexpr int SPEED_CX    = 580;       // speed text middle-centre x
  constexpr int SPEED_PAD_W = 360;       // bg-fill pad width (spans 400..760)
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
    uint16_t cloud_port       = 443;   // HTTPS endpoint. Port 80 only 301-redirects
                                       // to https://.../upload, which the dash won't follow.
    uint8_t  cloud_protocol   = 1;     // 0=HTTP, 1=HTTPS, 2=FTP (default HTTPS; FTP NYI)
    // cloud_user repurposed as a free-text USER EMAIL tag for data ownership.
    // Not an auth credential — the Teensy forwards it as X-User-Email header.
    // Real auth (Google OAuth) lives in the cloud-side Docker image.
    bool     coach_show    = true;       // show the AI coach checklist button on the dash
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
    // Voltage display/warn — only meaningful while the engine is running
    // (alternator should hold ~13.5-14.5 V; at/below the warn level while
    // running = failing alternator/belt).
    bool     show_volt          = false;
    uint16_t volt_warn_x10      = 128;     // warn at/below 12.8 V (×10)
    uint8_t  volt_warn_col      = 0;       // RED

    // Sensor data source. Selects which UART line type the dash trusts for
    // engine telemetry that exists in both pipelines (coolant temp today; RPM
    // and others as MS3 CAN comes online):
    //   0 = Direct     — Teensy ADCs on A2/A3 (the wiring we built in §5b/5c)
    //   1 = MegaSquirt — MS3 CAN broadcast, forwarded by Teensy as ECU,...
    //   2 = Bluetooth  — BLE OBD-II (ELM327) dongle on the CrowPanel for SLOW
    //                    readings (coolant/IAT/voltage). RPM still comes from
    //                    the Teensy opto tach (BT is too slow for the RPM bar).
    // Oil PSI stays direct regardless (MS3 typically doesn't have an oil
    // pressure input wired). AFR is only available in MegaSquirt mode.
    uint8_t  sensor_type        = 0;       // default Direct

    // Bluetooth OBD-II (ELM327 BLE) dongle — the paired device we reconnect to
    // when sensor_type == 2. Empty until one is chosen on PAGE_BT_SCAN.
    char     bt_addr[18]        = {0};     // "aa:bb:cc:dd:ee:ff"
    uint8_t  bt_atype           = 0;       // BLE address type (public/random)
    char     bt_name[24]        = {0};     // friendly name for the UI
    uint8_t  bt_pid_clt         = 0x05;    // Mode-01 PID mapped to COOLANT (default 05 = std ECT)

    // GPS UART baud (Teensy<->u-blox). Default 38400 = the KNOWN-GOOD rate the
    // module ships at (do not auto-raise at boot). Higher rates are an explicit,
    // recoverable choice on the GPS settings page (CFG,gpsbaud,<value>).
    uint32_t gps_baud           = 38400;
    // GPS nav (PVT) rate in Hz. Lower = far less UART traffic (10 Hz needs only
    // ~26% of 38400 vs ~65% at 25 Hz) = more headroom against stalls.
    uint8_t  gps_nav_hz         = 25;

    // Tach pulses/rev (the Direct-mode RPM divider), stored x10 so 0.5 is
    // representable. The number = how many tach pulses per crank revolution =
    // how much the raw opto-tach frequency is divided to get RPM. 20 = 2.0
    // (typical wasted-spark / most ECU tach outputs). MegaSquirt mode ignores
    // this (RPM comes straight from CAN).
    uint16_t rpm_ppr_x10        = 20;

    // RPM display smoothing trim, -10..+10 (0 = current/baseline). Sent to the
    // Teensy as CFG,rpmsm,<level>; it maps the level to its EMA strength:
    // -ve = rawer/snappier (numbers jump), +ve = heavier smoothing.
    int8_t   rpm_smooth         = 0;
    uint8_t  rpm_spike          = 2;    // 0=Off 1=Mild 2=Normal 3=Strong (CFG,rpmspk)
    uint8_t  gps_filter         = 2;    // GPS drift filter, same levels (CFG,gpsflt, NVS gpsflt)
    uint8_t  lap_overlay_s      = 3;    // finish-line lap-time popup duration, 0-9 s (0 = off)

    // AFR (Air/Fuel Ratio) — only meaningful in MegaSquirt sensor mode.
    // Two-sided warn band: too rich (< afr_warn_lo) and too lean (> afr_warn_hi)
    // both flip the value to the warn colour. Values are AFR × 10 (so 145 = 14.5).
    bool     show_afr           = true;
    uint16_t afr_warn_lo_x10    = 115;     // 11.5 AFR — below this is dangerously rich
    uint16_t afr_warn_hi_x10    = 160;     // 16.0 AFR — above this is dangerously lean
    uint8_t  afr_warn_col       = 0;       // RED

    // Track selection
    bool     auto_select_track = true; // when on, skip picker if a clear closest match exists

    // Auto-start recording: when enabled AND a track is selected, the dash
    // sends REC,1 on its own once GPS speed has been AT OR ABOVE
    // auto_start_mph CONTINUOUSLY for auto_start_sec seconds. The dwell is
    // what makes it trustworthy: a single GPS speed spike, a blip over a bump,
    // or a quick squirt across the paddock no longer starts a session (the
    // old code fired on the very first sample above the threshold).
    bool     auto_start       = false;
    uint16_t auto_start_mph   = 25;
    uint16_t auto_start_sec   = 4;     // seconds the speed must be HELD (1-30)

    // Time zone — index into TIMEZONES[] (defined below). Display only;
    // the Teensy's RTC + the wire-format TIME line are always UTC.
    uint8_t  timezone_idx     = 0;     // default UTC

    // LCD backlight brightness, 0-100 %. Applied board-specifically by
    // applyBrightness() (Advance: I2C 0x30 coprocessor; Basic: GPIO 2 PWM).
    uint8_t  brightness       = 100;

    // Debug logging master switch. When ON the Teensy writes an on-SD
    // "<session>.dbg.ndjson" health log per recording (uploaded best-effort as
    // X-File-Kind: debug). OFF = no debug files at all. Sent as CFG,dbg_on.
    // DEFAULT OFF since v0.1.103 (GPS saga solved — the JST connector): debug
    // logging is a diagnostic tool now, turned on from Settings when needed.
    bool     debug_enabled    = false;
};
static Settings s;

// Names for ENUM-style settings (cycle-on-tap).
const char* const PROTOCOL_NAMES[] = { "HTTP", "HTTPS", "FTP" };
constexpr int N_PROTOCOL = 3;
const char* const SENSOR_TYPE_NAMES[] = { "Direct", "MegaSquirt", "Bluetooth" };
// RPM spike filter (Teensy-side slew gate): rejects tach pulses implying a
// physically impossible RPM jump from the current value (electrical noise).
const char* const SPIKE_FILTER_NAMES[] = { "Off", "Mild", "Normal", "Strong" };
constexpr int N_SPIKE_FILTER = 4;
constexpr int N_SENSOR_TYPE = 3;
// Tach pulses/rev choices (the Direct-mode RPM divider). Value shown = pulses
// per crank rev = divider. Stored x10 so 0.5 works. Sent as CFG,rpmppr,<x10>.
const char* const RPM_PPR_NAMES[] = { "0.5", "1", "2", "3", "4", "6", "8" };
const uint16_t    RPM_PPR_X10[]   = {   5,   10,  20,  30,  40,  60,  80 };
constexpr int N_RPM_PPR = 7;
static int rpmPprIndex() {
    for (int i = 0; i < N_RPM_PPR; ++i) if (RPM_PPR_X10[i] == s.rpm_ppr_x10) return i;
    return 2;  // default to "2"
}
const char* const INET_MODE_NAMES[]   = { "Ethernet", "WiFi" };
constexpr int N_INET_MODE   = 2;
// GPS UART baud choices. Higher = more headroom for 25 Hz PVT (25 kbit/s);
// 38400 is only ~65% util (chronic backlog after loop stalls -> GPS STALE).
// Sent to the Teensy as CFG,gpsbaud,<value>; it switches the module + reports
// back GPSBAUD,<baud>,<ok> so you can tell instantly if data comes back.
const char* const GPS_BAUD_NAMES[] = { "9600", "38400", "115200", "230400", "460800" };
const uint32_t    GPS_BAUD_VALS[]  = {  9600,   38400,   115200,   230400,   460800 };
constexpr int N_GPS_BAUD = 5;
static int gpsBaudIndex() {
    for (int i = 0; i < N_GPS_BAUD; ++i) if (GPS_BAUD_VALS[i] == s.gps_baud) return i;
    return 1;  // default 38400
}
const char* const GPS_HZ_NAMES[] = { "1", "5", "10", "25" };
const uint8_t     GPS_HZ_VALS[]  = {  1,   5,   10,   25 };
constexpr int N_GPS_HZ = 4;
static int gpsHzIndex() {
    for (int i = 0; i < N_GPS_HZ; ++i) if (GPS_HZ_VALS[i] == s.gps_nav_hz) return i;
    return 3;  // default 25
}

// Live GPS diagnostics from the Teensy (GPSDIAG line), shown on PAGE_GPS.
static uint32_t gps_diag_baud    = 0;
static uint8_t  gps_diag_libok   = 0;
static uint32_t gps_diag_age_ms  = 999999;
static uint32_t gps_diag_recover = 0;
static uint32_t gps_diag_reinit  = 0;
static uint8_t  gps_diag_hz      = 0;
static bool     gps_page_dirty   = true;
// Snapshot taken on entry to PAGE_GPS so Cancel can revert baud/Hz + the module.
static uint32_t gps_orig_baud    = 38400;
static uint8_t  gps_orig_hz      = 25;
// Sensor-source page + BT-scan page repaint flags.
static bool     sensor_page_dirty = true;
static bool     bt_scan_dirty     = true;
// PAGE_BT_SCAN layout + scroll state. Constants live up here (not in the
// page's anonymous namespace) because handleTouch() needs them for drag-scroll.
static constexpr int BT_ROW_Y0 = 92, BT_ROW_H = 52;
static constexpr int BT_RESCAN_X = 60, BT_BACK_X = 440, BT_FOOT_Y = 410, BT_FOOT_W = 300, BT_FOOT_H = 54;
static constexpr int BT_VIEW_H = BT_FOOT_Y - 10 - BT_ROW_Y0;   // scrollable body height
static int      bt_scan_scroll    = 0;   // px; active scan can return 16 rows > viewport
static void clampBtScanScroll() {
    int maxS = obd::scanCount() * BT_ROW_H - BT_VIEW_H;
    if (maxS < 0) maxS = 0;
    if (bt_scan_scroll < 0)    bt_scan_scroll = 0;
    if (bt_scan_scroll > maxS) bt_scan_scroll = maxS;
}
// PAGE_PID_SCAN layout + scroll (same up-top placement rationale as BT_*).
static constexpr int PS_ROW_Y0 = 92, PS_ROW_H = 40;
static constexpr int PS_VIEW_H = BT_FOOT_Y - 10 - PS_ROW_Y0;   // shares the BT footer geometry
static bool     pid_scan_dirty  = true;
static int      pid_scan_scroll = 0;
static void clampPidScanScroll() {
    int maxS = obd::pidCount() * PS_ROW_H - PS_VIEW_H;
    if (maxS < 0) maxS = 0;
    if (pid_scan_scroll < 0)    pid_scan_scroll = 0;
    if (pid_scan_scroll > maxS) pid_scan_scroll = maxS;
}
static uint8_t  sensor_orig_type  = 0;   // snapshot for CANCEL on PAGE_SENSOR
// Device health (heat/brownout diagnostics). Teensy temps + battery arrive via
// the HLTH line; our own ESP32-S3 temp we read locally and report via DTEMP.
static float    health_teensy_c   = NAN;
static float    health_mpu_c      = NAN;
static float    health_esp_c      = NAN;
static float    health_batt_v     = NAN;
static uint32_t health_last_hlth_ms = 0;   // millis() of last HLTH from Teensy
// If a prior BLE init reset the board, this holds the human-readable reason
// (captured on boot from esp_reset_reason() + the obd breadcrumb).
static char     ble_diag[96]      = "";
static const char* resetReasonStr(esp_reset_reason_t r) {
    switch (r) {
        case ESP_RST_POWERON:   return "power-on";
        case ESP_RST_SW:        return "sw-reset";
        case ESP_RST_PANIC:     return "PANIC (crash)";
        case ESP_RST_INT_WDT:   return "int-watchdog";
        case ESP_RST_TASK_WDT:  return "task-watchdog";
        case ESP_RST_WDT:       return "watchdog";
        case ESP_RST_BROWNOUT:  return "BROWNOUT (power)";
        case ESP_RST_DEEPSLEEP: return "deepsleep";
        case ESP_RST_EXT:       return "ext-reset";
        default:                return "unknown";
    }
}

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
// sf_from (v0.1.115): a config variant may BORROW its S/F (baked coords AND
// the SET START/FINISH override slot in the sf_ovr blob) from a hidden aux
// TRACKS[] entry named here — this is how one "Summit Point" picker entry
// carries three sub-tracks with genuinely different start/finish lines while
// the append-only override storage stays index-stable. nullptr = parent's S/F.
struct TrackConfig { const char* name; const char* sf_from; };

struct TrackInfo {
    const char* name;
    float       lat;                    // track centre (used for "are you at this track?")
    float       lon;
    float       radius_km;
    float       sf_lat;                 // start/finish line endpoint A (lap detection)
    float       sf_lon;                 // NOTE: all S/F coords are approximate — verify on-site
    const TrackConfig* configs;         // nullptr = single layout, no config sub-picker
    uint8_t            n_configs;       // 0 = single layout
    // APPENDED (no default initializers -> struct stays an aggregate; existing
    // positional initializers that omit these get 0 via aggregate zero-fill):
    float       sf_lat2;                // S/F endpoint B. (0,0) => point-only -> radius fallback.
    float       sf_lon2;                // Two endpoints => precise LINE-CROSSING lap detection.
    uint8_t     aux;                    // 1 = manual-select-only VARIANT (never auto-picked).
                                        //     For facilities with overlapping circuits (Summit
                                        //     Point): auto-select always lands on the primary;
                                        //     variants are chosen from the picker, and the
                                        //     SELECTED track then drives lap timing + SET S/F.
};
// Config arrays — tracks with multiple layouts that share the same S/F line.
// Layouts with genuinely different S/F locations get separate TRACKS[] entries.
static const TrackConfig MID_OHIO_CFGS[] = { {"Full + Chicane"}, {"Full Course"}, {"Short Course"} };
static const TrackConfig SONOMA_CFGS[]   = { {"Full Course"}, {"Short Course"} };
static const TrackConfig SUMMIT_CFGS[]   = {
    {"Main",       nullptr},
    {"Jefferson",  "Summit Point Jefferson"},    // hidden aux entries below
    {"Shenandoah", "Summit Point Shenandoah"},
};
static const TrackConfig WGL_CFGS[]      = { {"Grand Prix"}, {"Short Course"} };
static const TrackConfig VIR_CFGS[]      = { {"Full Course"}, {"Grand Course"}, {"North Course"} };

// osm-sf rows: S/F lines auto-derived 2026-07-18 from OSM centerlines via
// pit-lane adjacency (fallback: longest straight); midpoint on the racing
// line by construction, 16 m wide. 'VERIFY' = eyeball in /tools/sfpicker
// (config may not use that straight / fallback method / offset from the
// official stripe). Centers fixed for Mid-Ohio (was 37 km off!), Nelson
// Ledges (10.6 km), NJMP Thunderbolt + Lightning (~4.5 km) - auto-select
// could never have matched those four before.
static const TrackInfo TRACKS[] = {
    // name                     centre lat/lon           radius  S/F lat/lon               configs       n
    { "Barber", 33.5328f, -86.6181f, 2.5f, 33.531077f, -86.621706f, nullptr, 0, 33.530972f, -86.621588f },  // osm-sf
    { "CMP Full", 34.4884f, -80.5941f, 2.0f, 34.487516f, -80.596613f, nullptr, 0, 34.487412f, -80.596735f },  // osm-sf
    { "CMP East", 34.4870f, -80.5880f, 1.2f, 34.487516f, -80.596613f, nullptr, 0, 34.487412f, -80.596735f },  // osm-sf VERIFY
    { "CMP West", 34.4884f, -80.6000f, 1.0f, 34.487516f, -80.596613f, nullptr, 0, 34.487412f, -80.596735f },  // osm-sf VERIFY
    { "COTA", 30.1328f, -97.6411f, 3.0f, 30.132368f, -97.640393f, nullptr, 0, 30.132254f, -97.640496f },  // osm-sf
    { "Daytona", 29.1853f, -81.0697f, 3.0f, 29.184313f, -81.072454f, nullptr, 0, 29.184213f, -81.072335f },  // osm-sf VERIFY
    { "Laguna Seca", 36.5847f, -121.7494f, 2.5f, 36.584093f, -121.757188f, nullptr, 0, 36.584080f, -121.757010f },  // osm-sf
    { "Lime Rock", 41.9263f, -73.3856f, 2.0f, 41.928886f, -73.381634f, nullptr, 0, 41.928755f, -73.381713f },  // osm-sf
    { "Mid-Ohio", 40.6896f, -82.6364f, 2.5f, 40.689477f, -82.637036f, MID_OHIO_CFGS, 3, 40.689620f, -82.637027f },  // osm-sf
    { "Nelson Ledges", 41.3055f, -81.0180f, 1.5f, 41.303945f, -81.021750f, nullptr, 0, 41.303906f, -81.021566f },  // osm-sf VERIFY
    { "NHMS", 43.3628f, -71.4630f, 2.0f, 43.362739f, -71.462085f, nullptr, 0, 43.362786f, -71.462272f },  // osm-sf
    { "NJMP Thunderbolt", 39.3603f, -75.0687f, 2.0f, 39.360880f, -75.074045f, nullptr, 0, 39.361003f, -75.073949f },  // osm-sf
    { "NJMP Lightning", 39.3636f, -75.0559f, 1.5f, 39.363346f, -75.052412f, nullptr, 0, 39.363265f, -75.052258f },  // osm-sf VERIFY
    { "Pocono", 41.0561f, -75.5128f, 3.5f, 41.052446f, -75.510868f, nullptr, 0, 41.052313f, -75.510939f },  // osm-sf
    { "Road America", 43.7986f, -87.9956f, 3.0f, 43.798079f, -87.989534f, nullptr, 0, 43.798076f, -87.989733f },  // osm-sf
    { "Road Atlanta", 34.1469f, -83.8189f, 2.5f, 34.149649f, -83.813004f, nullptr, 0, 34.149770f, -83.812910f },  // osm-sf
    { "Sebring", 27.4570f, -81.3568f, 3.5f, 27.450302f, -81.352701f, nullptr, 0, 27.450158f, -81.352700f },  // osm-sf
    { "Sonoma", 38.1614f, -122.4544f, 2.5f, 38.160290f, -122.453223f, SONOMA_CFGS, 2, 38.160341f, -122.453052f },  // osm-sf
    // Summit Point: ONE picker entry, three sub-tracks (configs). The
    // Jefferson/Shenandoah rows below are aux=1 = HIDDEN storage tombstones:
    // never auto-picked, never listed — they exist so each sub-track keeps
    // its own baked S/F + its own SET START/FINISH override slot (indices
    // must stay stable for the sf_ovr blob — do NOT remove or reorder).
    // Main circuit S/F LINE picked by the user on satellite imagery
    // (2026-07-18, /tools/sfpicker; midpoint verified 1.7 m off the OSM
    // centerline). Facility centre/radius unchanged — they define "am I at
    // Summit Point", the line defines the lap crossing.
    { "Summit Point",            39.2415f, -77.9779f, 2.0f, 39.235214f, -77.969128f, SUMMIT_CFGS, 3, 39.235189f, -77.969019f },
    // Jefferson S/F LINE user-picked 2026-07-18 (/tools/sfpicker; midpoint
    // verified 1.7 m off the OSM Jefferson Circuit centerline). Old pin was
    // 118 m from any tarmac — lap detection could never fire there.
    { "Summit Point Jefferson",  39.231705f, -77.975314f, 1.2f, 39.234146f, -77.972436f, nullptr,  0, 39.234125f, -77.972320f, 1 },
    { "Summit Point Shenandoah", 39.2450f, -77.9650f, 1.5f, 39.241349f, -77.979632f, nullptr, 0, 39.241207f, -77.979657f, 1 },  // osm-sf VERIFY
    { "VIR", 36.5611f, -79.2103f, 2.5f, 36.568224f, -79.209125f, VIR_CFGS, 3, 36.568095f, -79.209045f },  // osm-sf
    { "VIR South", 36.5620f, -79.2100f, 1.2f, 36.558404f, -79.209554f, nullptr, 0, 36.558461f, -79.209390f },  // osm-sf VERIFY
    { "VIR Patriot", 36.5660f, -79.2120f, 1.0f, 36.557121f, -79.207920f, nullptr, 0, 36.557051f, -79.208077f },  // osm-sf VERIFY
    { "Watkins Glen", 42.3417f, -76.9272f, 2.5f, 42.340868f, -76.928941f, WGL_CFGS, 2, 42.340859f, -76.928746f },  // osm-sf
    // Appended (TRACKS[] is append-only — keeps NVS sf_ovr indices stable).
    // S/F pinned from satellite (tools/track_sf_picker.html); refine on-site via STATUS → SET START/FINISH.
    // Thompson S/F LINE user-picked 2026-07-18 (midpoint verified 0.3 m off
    // the OSM Road Course centerline). Facility centre kept (proven in the
    // field for auto-select); only the crossing line changed.
    { "Thompson",      41.979695f, -71.827086f, 1.5f, 41.979644f, -71.827130f, nullptr,    0, 41.979743f, -71.827036f },
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

// Do the path segment P0->P1 and the S/F segment A->B intersect? Planar test
// with longitude scaled by cos(lat) so it's accurate over the tiny distances
// involved. This is the precise lap trigger: a lap is logged the instant the
// car's track crosses the start/finish LINE (vs the old "within a radius").
static inline double sfCross_(double px,double py,double qx,double qy,double rx,double ry){
    return (qx-px)*(ry-py)-(qy-py)*(rx-px);
}
static bool segmentsCross(float p0Lat,float p0Lon,float p1Lat,float p1Lon,
                          float aLat,float aLon,float bLat,float bLon){
    const double k = cos(p0Lat * 0.017453292519943295);
    const double ax=p0Lon*k, ay=p0Lat, bx=p1Lon*k, by=p1Lat;
    const double cx=aLon*k, cy=aLat, dx=bLon*k, dy=bLat;
    const double d1=sfCross_(cx,cy,dx,dy,ax,ay), d2=sfCross_(cx,cy,dx,dy,bx,by);
    const double d3=sfCross_(ax,ay,bx,by,cx,cy), d4=sfCross_(ax,ay,bx,by,dx,dy);
    return ((d1>0)!=(d2>0)) && ((d3>0)!=(d4>0));
}

// Returns index of closest track within its radius, OR -1 if no GPS / no
// track in range. Used both for "auto select" and to highlight in the picker.
static int closestTrackIdx() {
    if (g.fix < 2) return -1;            // no usable fix
    int   best   = -1;
    float bestKm = 1e9f;
    for (int i = 0; i < N_TRACKS; ++i) {
        if (TRACKS[i].aux) continue;     // variants are manual-select only
        const float km = trackDistanceKm(g.lat_deg, g.lon_deg,
                                         TRACKS[i].lat, TRACKS[i].lon);
        if (km <= TRACKS[i].radius_km && km < bestKm) {
            bestKm = km; best = i;
        }
    }
    return best;
}

// The track that LAP TIMING and SET START/FINISH should operate on: the
// user's SELECTED track wins whenever the car is actually within its radius
// (so picking "Summit Point Jefferson" beats GPS guessing the main circuit
// from overlapping centres); only when nothing usable is selected do we fall
// back to the GPS-closest primary. This is the fix for "S/F set was stuck on
// Jefferson even though I picked Summit Point main".
static int lapTrackIdx() {
    if (g.fix < 2) return -1;
    if (last_track_idx >= 0 && last_track_idx < N_TRACKS) {
        const float km = trackDistanceKm(g.lat_deg, g.lon_deg,
                                         TRACKS[last_track_idx].lat,
                                         TRACKS[last_track_idx].lon);
        if (km <= TRACKS[last_track_idx].radius_km) return last_track_idx;
    }
    return closestTrackIdx();
}

// ---------------------------------------------------------------------------
// Per-track start/finish override. The baked sf_lat/sf_lon are approximate;
// this lets the user park on (or cross) the real line and capture it on-site.
// Stored in NVS as one blob keyed by TRACKS[] index (append-only: never insert
// a track in the middle or existing overrides shift onto the wrong track).
// ---------------------------------------------------------------------------
// Where a track's S/F (baked coords + SET START/FINISH override slot) LIVES.
// A config variant with sf_from (e.g. Summit Point / Jefferson) borrows a
// hidden aux TRACKS[] entry — resolved by name so array growth can't skew it.
// Everything S/F-related (lap timing, capture, clear, Teensy CFG,sf) routes
// through this.
static int sfStorageIdx(int tIdx);   // fwd (needs sfOverride below)

// used==0 => baked S/F. lat/lon = endpoint A; lat2/lon2 = endpoint B
// (0,0 => point-only, radius fallback). Blob size changed with the line fields,
// so any pre-line stored blob is length-mismatched and ignored (overrides reset
// once — acceptable; re-capture via SET START/FINISH).
struct SfOverride { uint8_t used; float lat; float lon; float lat2; float lon2; };
// ⚠️ v0.1.129 POLICY CHANGE: for KNOWN tracks the baked S/F (managed on the
// web via /tools/sfpicker) is the ONLY source — sfOverride[] is kept in NVS
// for back-compat but is NO LONGER CONSULTED. A stale on-device capture used
// to silently beat a freshly baked line and kill lap timing with no clue why
// (the Summit Point incident). The on-car SET S/F capture now exists ONLY for
// UNKNOWN tracks (nothing baked to beat), stored in its own slot below.
static SfOverride sfOverride[N_TRACKS];   // legacy known-track overrides (ignored since v0.1.129)
static SfOverride sf_unknown;             // UNKNOWN-track S/F (NVS "sf_unk")

// UI state for the STATUS-page "SET START/FINISH" capture button.
static bool     sf_set_armed  = false;
static uint32_t sf_set_arm_ms = 0;
static char     sf_set_msg[48] = "";
static uint32_t sf_set_msg_ms = 0;

// Resolve the start/finish line actually used for lap detection at track idx.
static void effectiveSf(int idx, float* lat, float* lon) {
    idx = sfStorageIdx(idx);   // config variants borrow their own S/F entry
    if (idx < 0 || idx >= N_TRACKS) {
        // UNKNOWN track: the user-captured slot is the only S/F there is.
        if (idx < 0 && sf_unknown.used) { *lat = sf_unknown.lat; *lon = sf_unknown.lon; return; }
        *lat = 0; *lon = 0; return;
    }
    // KNOWN track: baked ONLY (web-managed; overrides ignored since v0.1.129).
    *lat = TRACKS[idx].sf_lat;
    *lon = TRACKS[idx].sf_lon;
}

// Resolve the S/F as a LINE (endpoints A,B). hasLine=false => only a point is
// known (endpoint B is 0,0) and the caller should use the radius method.
static int sfStorageIdx(int tIdx) {
    if (tIdx < 0 || tIdx >= N_TRACKS) return tIdx;
    if (tIdx == last_track_idx && TRACKS[tIdx].configs
        && active_cfg_idx < TRACKS[tIdx].n_configs) {
        const char* from = TRACKS[tIdx].configs[active_cfg_idx].sf_from;
        if (from) {
            for (int i = 0; i < N_TRACKS; ++i)
                if (strcmp(TRACKS[i].name, from) == 0) return i;
        }
    }
    return tIdx;
}

static void effectiveSfLine(int idx, float* aLat, float* aLon,
                            float* bLat, float* bLon, bool* hasLine) {
    idx = sfStorageIdx(idx);   // config variants borrow their own S/F entry
    if (idx < 0 || idx >= N_TRACKS) {
        if (idx < 0 && sf_unknown.used) {   // UNKNOWN track: captured slot
            *aLat=sf_unknown.lat;  *aLon=sf_unknown.lon;
            *bLat=sf_unknown.lat2; *bLon=sf_unknown.lon2;
            *hasLine = (*bLat != 0.0f || *bLon != 0.0f);
            return;
        }
        *aLat=*aLon=*bLat=*bLon=0; *hasLine=false; return;
    }
    // KNOWN track: baked ONLY (web-managed; overrides ignored since v0.1.129).
    *aLat=TRACKS[idx].sf_lat;  *aLon=TRACKS[idx].sf_lon;
    *bLat=TRACKS[idx].sf_lat2; *bLon=TRACKS[idx].sf_lon2;
    *hasLine = (*bLat != 0.0f || *bLon != 0.0f);
}

// Send the active track's S/F line to the Teensy so IT can stamp lap numbers
// into the recorded NDJSON (CFG,sf,aLat,aLon,bLat,bLon; all zero => none).
// Local ENU frame at the S/F midpoint: normal = across the line (direction of
// travel through it), tangent = along the line.
struct SfGate {
    bool  valid  = false;
    float mLat   = 0, mLon = 0;   // midpoint
    float k      = 1.0f;          // cos(lat): lon-degrees -> metres
    float nE     = 0, nN = 0;     // unit normal (across)
    float tE     = 0, tN = 0;     // unit tangent (along)
    float half_m = SF_GATE_HALF_M;
};

static void buildSfGate(int idx, SfGate* gt) {
    *gt = SfGate{};
    float aLat, aLon, bLat, bLon; bool hasLine;
    effectiveSfLine(idx, &aLat, &aLon, &bLat, &bLon, &hasLine);
    if (!hasLine) return;                       // point-only -> radius fallback
    gt->mLat = (aLat + bLat) * 0.5f;
    gt->mLon = (aLon + bLon) * 0.5f;
    gt->k    = cosf(gt->mLat * (float)M_PI / 180.0f);
    if (gt->k < 0.05f) gt->k = 0.05f;
    const float dE = (bLon - aLon) * 111320.0f * gt->k;   // tangent, metres
    const float dN = (bLat - aLat) * 111320.0f;
    const float len = sqrtf(dE * dE + dN * dN);
    if (len < 0.5f) return;                     // degenerate line
    gt->tE = dE / len;  gt->tN = dN / len;
    gt->nE = -gt->tN;   gt->nN =  gt->tE;       // perpendicular
    // A deliberately WIDE hand-drawn line is honoured; never narrower than
    // SF_GATE_HALF_M (which is what fixes the missed laps).
    gt->half_m = fmaxf(SF_GATE_HALF_M, len * 0.5f);
    gt->valid  = true;
}

// Signed distance (m) from a fix to the S/F plane + lateral offset along it.
static inline void sfGateProject(const SfGate& gt, float lat, float lon,
                                 float* s, float* u) {
    const float dE = (lon - gt.mLon) * 111320.0f * gt.k;
    const float dN = (lat - gt.mLat) * 111320.0f;
    *s = dE * gt.nE + dN * gt.nN;    // across  (sign flips when we cross)
    *u = dE * gt.tE + dN * gt.tN;    // along   (must stay inside the gate)
}

// True when prev->cur crosses the plane INSIDE the gate. *frac = where in the
// interval (0..1) -> interpolated crossing time; *dir = +1/-1 travel direction
// through the plane (direction gate: a wide gate must not count a wrong-way or
// pit-lane pass as a lap).
static bool sfGateCross(const SfGate& gt, float pLat, float pLon,
                        float cLat, float cLon, float* frac, float* dir) {
    if (!gt.valid) return false;
    float sp, up, sc, uc;
    sfGateProject(gt, pLat, pLon, &sp, &up);
    sfGateProject(gt, cLat, cLon, &sc, &uc);
    if ((sp < 0.0f) == (sc < 0.0f)) return false;      // no sign change
    const float denom = sp - sc;
    const float f  = (fabsf(denom) < 1e-6f) ? 0.5f : (sp / denom);
    const float fc = f < 0.0f ? 0.0f : (f > 1.0f ? 1.0f : f);
    const float u_cross = up + fc * (uc - up);
    if (fabsf(u_cross) > gt.half_m) return false;      // passed outside the gate
    *frac = fc;
    *dir  = (sc > sp) ? 1.0f : -1.0f;
    return true;
}

static void sendSfToTeensy(int idx) {
    float aLat=0,aLon=0,bLat=0,bLon=0; bool hasLine=false;
    effectiveSfLine(idx, &aLat,&aLon,&bLat,&bLon,&hasLine);   // idx<0 = UNKNOWN slot
    if (!hasLine) { Serial.printf("CFG,sf,0,0,0,0\n"); return; }
    Serial.printf("CFG,sf,%.6f,%.6f,%.6f,%.6f\n", aLat,aLon,bLat,bLon);
}

static void saveSfOverrides() {
    prefs.begin("dash", false);
    prefs.putBytes("sf_ovr", sfOverride, sizeof(sfOverride));   // legacy (unused)
    prefs.putBytes("sf_unk", &sf_unknown, sizeof(sf_unknown));
    prefs.end();
}

// Capture the CURRENT GPS position as the active track's custom S/F — a
// POINT when parked (<5 mph; heading is garbage at rest), a perpendicular
// LINE when rolling. Shared by the STATUS-page two-tap and the dash-page
// SET S/F button that replaces TRACK while recording (v0.1.115). Stores into
// the active config's S/F slot (sfStorageIdx) and pushes the new line to the
// Teensy so NDJSON lap stamping follows immediately.
static bool captureSfHere() {
    const int tIdx = lapTrackIdx();
    if (tIdx >= 0) {
        // v0.1.129: KNOWN tracks are web-managed (/tools/sfpicker) — no
        // on-car capture, so a stale override can never beat the baked line.
        snprintf(sf_set_msg, sizeof(sf_set_msg), "known track: set S/F on the web");
        sf_set_msg_ms = millis();
        return false;
    }
    if (g.fix < 2) {
        snprintf(sf_set_msg, sizeof(sf_set_msg), "no GPS fix");
        sf_set_msg_ms = millis();
        return false;
    }
    if (g.mph < 5.0f) {
        sf_unknown.used = 1;
        sf_unknown.lat  = g.lat_deg;
        sf_unknown.lon  = g.lon_deg;
        sf_unknown.lat2 = 0.0f;
        sf_unknown.lon2 = 0.0f;
        snprintf(sf_set_msg, sizeof(sf_set_msg), "S/F POINT SET (parked)");
    } else {
        const float D2R = (float)M_PI / 180.0f;
        const float H  = g.hdg_deg * D2R;
        const float lE = -cosf(H), lN = sinf(H);      // left-of-travel (E,N)
        const float half_m = 30.0f;
        const float dLat = half_m / 111320.0f;
        const float dLon = half_m / (111320.0f * cosf(g.lat_deg * D2R));
        sf_unknown.used = 1;
        sf_unknown.lat  = g.lat_deg + lN * dLat;
        sf_unknown.lon  = g.lon_deg + lE * dLon;
        sf_unknown.lat2 = g.lat_deg - lN * dLat;
        sf_unknown.lon2 = g.lon_deg - lE * dLon;
        snprintf(sf_set_msg, sizeof(sf_set_msg), "S/F LINE SET (unknown track)");
    }
    saveSfOverrides();
    sendSfToTeensy(-1);          // Teensy stamps lap #s into the NDJSON
    sf_set_msg_ms = millis();
    return true;
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
    s.auto_start         = prefs.getBool  ("autost",   s.auto_start);
    s.auto_start_mph     = prefs.getUShort("astmph",   s.auto_start_mph);
    s.auto_start_sec     = prefs.getUShort("astsec",   s.auto_start_sec);
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
    s.show_volt          = prefs.getBool  ("s_volt",   s.show_volt);
    s.coach_show         = prefs.getBool  ("coach",    s.coach_show);
    s.volt_warn_x10      = prefs.getUShort("v_warn",   s.volt_warn_x10);
    s.volt_warn_col      = prefs.getUChar ("v_col",    s.volt_warn_col);
    s.sensor_type        = prefs.getUChar ("srctyp",   s.sensor_type);
    // NVS key renamed dbg_on -> dbg2 in v0.1.103 to force the new OFF default
    // onto already-deployed units (old key had ON persisted; keys are
    // append-only — the orphaned dbg_on is ignored, never repurposed).
    s.debug_enabled      = prefs.getBool  ("dbg2",     s.debug_enabled);
    prefs.getString      ("bt_addr",  s.bt_addr, sizeof(s.bt_addr));
    s.bt_atype           = prefs.getUChar ("bt_atype", s.bt_atype);
    s.bt_pid_clt         = prefs.getUChar ("btpid",    s.bt_pid_clt);
    if (!s.bt_pid_clt) s.bt_pid_clt = 0x05;
    obd::setCoolantPid(s.bt_pid_clt);
    prefs.getString      ("bt_name",  s.bt_name, sizeof(s.bt_name));
    s.rpm_ppr_x10        = prefs.getUShort("rpmppr",   s.rpm_ppr_x10);
    s.gps_baud           = prefs.getULong ("gpsbaud",  s.gps_baud);
    s.gps_nav_hz         = prefs.getUChar ("gpshz",    s.gps_nav_hz);
    s.rpm_smooth         = prefs.getChar  ("rpmsm",    s.rpm_smooth);
    if (s.rpm_smooth < -10) s.rpm_smooth = -10;
    if (s.rpm_smooth >  10) s.rpm_smooth =  10;
    s.rpm_spike          = prefs.getUChar ("rpmspk",   s.rpm_spike) % N_SPIKE_FILTER;
    s.gps_filter         = prefs.getUChar ("gpsflt",   s.gps_filter) % N_SPIKE_FILTER;
    s.lap_overlay_s      = prefs.getUChar ("lapov",    s.lap_overlay_s);
    if (s.lap_overlay_s > 9) s.lap_overlay_s = 9;
    s.show_afr           = prefs.getBool  ("s_afr",    s.show_afr);
    s.afr_warn_lo_x10    = prefs.getUShort("afr_lo",   s.afr_warn_lo_x10);
    s.afr_warn_hi_x10    = prefs.getUShort("afr_hi",   s.afr_warn_hi_x10);
    s.afr_warn_col       = prefs.getUChar ("afr_col",  s.afr_warn_col);
    s.brightness         = prefs.getUChar ("bright",   s.brightness);
    if (s.timezone_idx >= N_TIMEZONES) s.timezone_idx = 0;   // sanitise stale NVS
    if (s.brightness < 10) s.brightness = 10;               // never fully dark
    {
        char ltrk[64] = "";
        prefs.getString("last_trk",   ltrk,               sizeof(ltrk));
        prefs.getString("last_trk_d", active_track_name,  sizeof(active_track_name));
        last_track_idx = -1;
        for (int i = 0; i < N_TRACKS; ++i) {
            if (strcmp(TRACKS[i].name, ltrk) == 0) { last_track_idx = i; break; }
        }
        active_cfg_idx = prefs.getUChar("lcfg", 0);
        if (last_track_idx < 0 || active_cfg_idx >= TRACKS[last_track_idx].n_configs)
            active_cfg_idx = 0;
        // If no display name was saved yet, fall back to the base track name.
        if (active_track_name[0] == '\0' && last_track_idx >= 0)
            strncpy(active_track_name, TRACKS[last_track_idx].name, sizeof(active_track_name) - 1);
    }
    // Per-track start/finish overrides (one blob). Only restore if the stored
    // size matches the current TRACKS[] count — a size change means tracks were
    // added/removed, so the old index map can't be trusted; ignore it then.
    if (prefs.getBytesLength("sf_ovr") == sizeof(sfOverride))
        prefs.getBytes("sf_ovr", sfOverride, sizeof(sfOverride));
    if (prefs.getBytesLength("sf_unk") == sizeof(sf_unknown))
        prefs.getBytes("sf_unk", &sf_unknown, sizeof(sf_unknown));
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
    prefs.putString("cl_email", s.cloud_auth_user);   // user email
    prefs.putString("cl_key",   s.cloud_auth_pass);   // API key
    // Clean up the renamed legacy keys so they don't shadow the new ones on
    // next boot's loadSettings() (the load path prefers cl_email/cl_key if
    // present, but stale cl_user/cl_pass values are confusing in debug dumps).
    if (prefs.isKey("cl_user")) prefs.remove("cl_user");
    if (prefs.isKey("cl_pass")) prefs.remove("cl_pass");
    prefs.putBool  ("auto_trk", s.auto_select_track);
    prefs.putBool  ("autost",   s.auto_start);
    prefs.putUShort("astmph",   s.auto_start_mph);
    prefs.putUShort("astsec",   s.auto_start_sec);
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
    prefs.putBool  ("s_volt",   s.show_volt);
    prefs.putBool  ("coach",    s.coach_show);
    prefs.putUShort("v_warn",   s.volt_warn_x10);
    prefs.putUChar ("v_col",    s.volt_warn_col);
    prefs.putUChar ("srctyp",   s.sensor_type);
    prefs.putBool  ("dbg2",     s.debug_enabled);
    prefs.putString("bt_addr",  s.bt_addr);
    prefs.putUChar ("bt_atype", s.bt_atype);
    prefs.putUChar ("btpid",    s.bt_pid_clt);
    prefs.putString("bt_name",  s.bt_name);
    prefs.putUShort("rpmppr",   s.rpm_ppr_x10);
    prefs.putULong ("gpsbaud",  s.gps_baud);
    prefs.putUChar ("gpshz",    s.gps_nav_hz);
    prefs.putChar  ("rpmsm",    s.rpm_smooth);
    prefs.putUChar ("rpmspk",   s.rpm_spike);
    prefs.putUChar ("gpsflt",   s.gps_filter);
    prefs.putUChar ("lapov",    s.lap_overlay_s);
    prefs.putBool  ("s_afr",    s.show_afr);
    prefs.putUShort("afr_lo",   s.afr_warn_lo_x10);
    prefs.putUShort("afr_hi",   s.afr_warn_hi_x10);
    prefs.putUChar ("afr_col",  s.afr_warn_col);
    prefs.putUChar ("bright",   s.brightness);
    prefs.end();
    sendCfgToTeensy();   // keep Teensy in sync after every settings save
}

// Apply LCD brightness (0-100 %). Board-aware via board_config.h:
//   Advance (DASH_IS_ADVANCE): backlight coprocessor at I2C 0x30. It takes
//     DISCRETE command codes, NOT a linear value — valid backlight levels (from
//     the vendor factory firmware's PWM ladder) are exactly these 7 codes,
//     brightest -> dimmest. Sending arbitrary bytes makes the coprocessor
//     misbehave (visible backlight flicker), so we snap to the ladder.
//   Basic 7"/5":              GPIO 2 PWM on ledc channel 1 (duty 0-255).
// Called at boot (after loadSettings) and live while the brightness slider drags.
static void applyBrightness(uint8_t pct) {
    if (pct > 100) pct = 100;
#if DASH_IS_ADVANCE
    // The 0x30 coprocessor takes a backlight level 0..245 (0 = brightest,
    // 245 = off) per the vendor example. Map the 10..100 % slider linearly
    // across that range (100 % -> 0 brightest, 10 % -> ~221 dim-but-on).
    const uint8_t v = (uint8_t)(245 - (uint32_t)pct * 245 / 100);
    Wire.beginTransmission(0x30);
    Wire.write(v);
    Wire.endTransmission();
#else
    ledcWrite(1, (uint32_t)pct * 255 / 100);
#endif
}

// Push the runtime config the Teensy needs to do cloud uploads. Format:
//   CFG,<key>,<value>\n   (one line per setting; Teensy stores in g_cfg)
// Call after loadSettings() at boot and at the end of saveSettings(). The
// Teensy parser tolerates unknown keys, so we can grow this list freely.
static void sendCfgToTeensy() {
    Serial.printf("CFG,cl_host,%s\n",   s.cloud_host);
    Serial.printf("CFG,cl_port,%u\n",   (unsigned)s.cloud_port);
    Serial.printf("CFG,cl_proto,%u\n",  (unsigned)s.cloud_protocol);
    Serial.printf("CFG,cl_email,%s\n",  s.cloud_auth_user);
    Serial.printf("CFG,cl_key,%s\n",    s.cloud_auth_pass);
    Serial.printf("CFG,rec_sd,%d\n",    (int)s.record_sd);
    Serial.printf("CFG,rec_cl,%d\n",    (int)s.record_cloud);
    Serial.printf("CFG,inet,%u\n",      (unsigned)s.internet_mode);
    Serial.printf("CFG,srctyp,%u\n",    (unsigned)s.sensor_type);
    Serial.printf("CFG,rpmppr,%u\n",    (unsigned)s.rpm_ppr_x10);
    Serial.printf("CFG,gpsbaud,%lu\n",  (unsigned long)s.gps_baud);
    Serial.printf("CFG,gpshz,%u\n",     (unsigned)s.gps_nav_hz);
    Serial.printf("CFG,rpmsm,%d\n",     (int)s.rpm_smooth);
    Serial.printf("CFG,rpmspk,%u\n",    (unsigned)s.rpm_spike);
    Serial.printf("CFG,gpsflt,%u\n",    (unsigned)s.gps_filter);
    Serial.printf("CFG,dbg_on,%d\n",    (int)s.debug_enabled);
    sendSfToTeensy(last_track_idx);     // active track's S/F line for lap stamping
}

static void saveLastTrack(int idx, const char* display_name = nullptr) {
    if (idx < 0 || idx >= N_TRACKS) return;
    last_track_idx = idx;
    // Plain track select (no config display name) resets to the primary
    // config; confirmConfigAndStart sets active_cfg_idx BEFORE calling us.
    if (!display_name) active_cfg_idx = 0;
    const char* dn = display_name ? display_name : TRACKS[idx].name;
    strncpy(active_track_name, dn, sizeof(active_track_name) - 1);
    active_track_name[sizeof(active_track_name) - 1] = '\0';
    prefs.begin("dash", false);
    prefs.putString("last_trk",   TRACKS[idx].name);
    prefs.putString("last_trk_d", active_track_name);
    prefs.putUChar ("lcfg",       active_cfg_idx);
    prefs.end();
    sendSfToTeensy(idx);   // Teensy needs the S/F line to stamp lap #s in NDJSON
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
    PAGE_SESSIONS      = 12,  // queued NDJSON sessions: select + delete + delete all
    PAGE_GPS           = 13,  // GPS baud selector + live GPS diagnostics
    PAGE_SENSOR        = 14,  // Sensor source picker (Direct/MegaSquirt/Bluetooth) + BT status
    PAGE_BT_SCAN       = 15,  // BLE OBD-II device scan + select
    PAGE_PID_SCAN      = 16,  // Mode-01 PID scan + map one to the COOLANT function
    PAGE_COACH         = 17,  // AI coach checklist: tap an item to tick it off
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

// Longest UART lines are Q,L/WUP,L upload records: "Q,L," plus one
// NDJSON sample. The Teensy-side line buffer is 320 bytes, so 256 here was
// too small and could drop perfectly valid samples mid-upload, leading to
// Teensy-side ack_timeout failures. Keep this comfortably above that.
static constexpr size_t UART_LINE_MAX = 512;
static String rxBuf;

// ---------------------------------------------------------------------------
// AI COACH CHECKLIST (v0.1.137)
// The server reviews every uploaded session and files 1-3 short actionable
// items. The dash fetches ONLY the open ones (GET /coach/<user>/open — the
// server never returns ticked items, so a cleared item can't reappear here by
// construction) and ticking one POSTs it back as done. All HTTP happens on a
// short-lived core-0 task so the 60 fps loop and the UART pump never block.
// ---------------------------------------------------------------------------
static constexpr int COACH_MAX = 3;
static char     coach_id[COACH_MAX][20]  = {{0}};
static char     coach_txt[COACH_MAX][108] = {{0}};
static volatile int  coach_n        = 0;
static volatile bool coach_busy     = false;   // a task is in flight
static volatile bool coach_dirty    = false;   // redraw the coach page
static char     coach_tick_id[20]   = "";      // id to POST done ("" = fetch)
static uint32_t coach_last_fetch_ms = 0;
static uint32_t coach_refetch_at_ms = 0;   // scheduled re-fetch (0 = none)

// Teensy-link hygiene (v0.1.135). q_activity_ms = millis() of the last Q,*
// line SEEN FROM the Teensy — the only reliable signal that it is mid-ARQ
// (the dash's own uf/sl state lies: it returns to idle on failure while the
// Teensy retransmits for up to 120 s). cfg_resend_req is set when the Teensy
// announces a reboot (RST,teensy) so its config is restored event-driven
// instead of by blind periodic bursts.
static uint32_t q_activity_ms  = 0;
static bool     cfg_resend_req = false;

static bool parseGpsLine(const String& line) {
    int idx[8], n = 0;
    for (int i = 0; i < (int)line.length() && n < 8; ++i) {
        if (line[i] == ',') idx[n++] = i;
    }
    if (n < 6) return false;
    idx[n] = line.length();
    auto field = [&](int k) { return line.substring(idx[k] + 1, idx[k + 1]); };
    // Parse into LOCALS first — a corrupted line must not be able to poison
    // g.* before it has been sanity-checked (v0.1.133).
    const uint8_t p_fix  = (uint8_t)field(0).toInt();
    const uint8_t p_sats = (uint8_t)field(1).toInt();
    const float   p_lat  = field(2).toFloat();
    const float   p_lon  = field(3).toFloat();
    const float   p_mph  = field(4).toFloat();
    const float   p_hdg  = field(5).toFloat();
    const uint8_t p_stat = (n >= 7) ? (uint8_t)field(6).toInt() : 0;

    // ---- GPS SANITY GATE (v0.1.133) — THE lap-timer killer ----
    // A single dropped/overflowed UART byte mangles one GPS line, and
    // String::toFloat() returns 0.0 for anything unparseable. The resulting
    // (0,0) "null island" fix sits outside every track radius, which used to
    // WIPE the whole lap timer — so a lap only completed if an ENTIRE ~85 s
    // lap passed with zero corrupted samples. Measured against a real
    // Thompson trace with the real firmware: one bad line per ~100 s took
    // 4 detected laps -> 0. (Server/SD data looked perfect because the Teensy
    // logs locally and never crosses the UART hop.)
    // Drop the bad sample and KEEP the last good position instead.
    if (p_fix >= 2) {
        if (fabsf(p_lat) < 0.001f && fabsf(p_lon) < 0.001f) return false;  // null island
        if (fabsf(p_lat) > 90.0f || fabsf(p_lon) > 180.0f)  return false;  // impossible
        // Teleport check: >500 m between consecutive fixes isn't physics at
        // 25 Hz (45 000 km/h). Only applied while the last good fix is RECENT
        // — after a dropout/stale a genuine large jump is expected, and the
        // 2 s window makes this self-healing (it can never latch onto a dead
        // position and reject reality forever).
        if (g.last_ms != 0 && g.fix >= 2 && (millis() - g.last_ms) < 2000) {
            if (trackDistanceKm(g.lat_deg, g.lon_deg, p_lat, p_lon) > 0.5f) return false;
        }
    }

    g.fix     = p_fix;
    g.sats    = p_sats;
    g.lat_deg = p_lat;
    g.lon_deg = p_lon;
    g.mph     = p_mph;
    g.hdg_deg = p_hdg;
    g.status  = p_stat;
    g.last_ms = millis();

    // Auto-start recording: once enabled with a track selected, kick off the
    // session when speed crosses the threshold. Latched so a manual stop (or
    // holding above the threshold) doesn't re-trigger; re-arms only after speed
    // drops back below the threshold.
    {
        static bool     autostart_fired = false;
        static uint32_t above_since_ms  = 0;   // when speed FIRST reached the threshold
        const uint32_t hold_ms = (uint32_t)s.auto_start_sec * 1000UL;
        if (!s.auto_start || active_track_name[0] == '\0') {
            autostart_fired = false;
            above_since_ms  = 0;
        } else if (g.mph < (float)s.auto_start_mph) {
            autostart_fired = false;                      // slow -> re-arm
            above_since_ms  = 0;                          // ANY dip restarts the dwell
        } else {
            // At/above the threshold: start (or continue) the dwell timer and
            // only fire once the speed has been HELD for auto_start_sec. A
            // single spike can no longer open a session.
            if (above_since_ms == 0) above_since_ms = millis();
            if (!autostart_fired && !recording
                && (millis() - above_since_ms) >= hold_ms) {
                Serial.printf("TRACK,%s\n", active_track_name);
                Serial.printf("REC,1\n");
                recording = true; rec_start_ms = millis();
                autostart_fired = true;
            }
        }
        if (recording) autostart_fired = true;            // any session spends the latch
        autostart_pending_ms = (!recording && s.auto_start && above_since_ms != 0
                                && hold_ms > (millis() - above_since_ms))
                             ? (hold_ms - (millis() - above_since_ms)) : 0;
    }
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
    // Lap timing only runs WHILE RECORDING (v0.1.105): PRED/DELTA ticking on
    // the cooldown lap / in the paddock is noise. STOP freezes the timer (the
    // LAP row keeps the last time as a static fact); START begins a fresh
    // session — resetting on the rising edge also avoids a stale prev_gps_ms
    // producing a huge dt/distance jump on the first update after a restart.
    static bool     was_recording = false;
    static uint16_t offtrack_n    = 0;   // consecutive "not at any track" fixes
    if (!recording) { was_recording = false; return; }
    if (!was_recording) { lapTimer = LapTimer{}; was_recording = true; offtrack_n = 0; }
    if (g.fix < 2) return;    // no usable fix — pause, don't reset

    // SELECTED track wins (see lapTrackIdx): a picked variant (e.g. Summit
    // Point Jefferson) times against ITS S/F, and a stable index means the
    // overlapping-circuit flap can no longer reset the timer mid-lap.
    const int tIdx = lapTrackIdx();
    const uint32_t now = millis();

    if (tIdx < 0 && !sf_unknown.used) {
        // Not at any known track and no user-captured S/F — nothing to time
        // against. DEBOUNCED (v0.1.133): ONE bad fix must never destroy lap
        // state. The old code wiped the timer on the FIRST out-of-range
        // sample, so a corrupted GPS line mid-lap cost the entire lap; at ~1
        // bad line per lap that meant EVERY lap ("40 laps before 1
        // registered"). Require ~2 s of CONTINUOUS "nowhere" before clearing,
        // which is far longer than any corruption burst but still instant on
        // the scale of actually leaving a circuit.
        if (++offtrack_n >= 50) {
            if (lapTimer.active) lapTimer = LapTimer{};
            offtrack_n = 50;                  // saturate, never wrap
        }
        return;
    }
    offtrack_n = 0;                           // back on track

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
    const uint32_t t_prev_fix = lapTimer.prev_fix_ms;   // for crossing interpolation
    const float dt_h = (float)(now - lapTimer.prev_gps_ms) * (1.0f / 3600000.0f);
    lapTimer.dist_miles += g.mph * dt_h;
    lapTimer.prev_gps_ms = now;
    lapTimer.prev_fix_ms = now;

    const uint32_t elapsed = now - lapTimer.lap_start_ms;

    // Record this lap's elapsed-time-vs-distance into the ghost table. Each
    // bucket holds the elapsed time at which cumulative distance first reached
    // that bucket boundary; cur_bucket is how many we've filled.
    {
        int b = (int)(lapTimer.dist_miles / LAP_BUCKET_MI);
        if (b >= LAP_BUCKETS) b = LAP_BUCKETS - 1;
        while (lapTimer.cur_bucket <= b) lapCurBt[lapTimer.cur_bucket++] = elapsed;
    }

    // ---- Start/finish crossing detection ----
    // Precise LINE crossing when a 2-point S/F line is known; otherwise fall
    // back to the legacy radius method (point-only tracks).
    float aLat, aLon, bLat, bLon; bool hasLine;
    effectiveSfLine(tIdx, &aLat, &aLon, &bLat, &bLon, &hasLine);
    // Once per 20 s: "why aren't laps ticking" breadcrumb (echoed to the
    // Teensy USB console). d_sf should sweep down near 0 every lap; if it
    // never does, the S/F (override?) is in the wrong place — STATUS page
    // shows the same distance live, CLR S/F resets it.
    {
        static uint32_t last_lap_dbg = 0;
        if (now - last_lap_dbg >= 20000) {
            last_lap_dbg = now;
            const int dm = (int)(trackDistanceKm(g.lat_deg, g.lon_deg, aLat, aLon) * 1000.0f);
            const int sdi = sfStorageIdx(tIdx);
            Serial.printf("DBG,lap trk=%s sf=%s ovr=%d line=%d d_sf=%dm armed=%d laps=%d\n",
                          TRACKS[tIdx].name, TRACKS[sdi].name,
                          (int)sfOverride[sdi].used, (int)hasLine,
                          dm, (int)lapTimer.timing_started, (int)lapTimer.lap_number);
        }
    }
    bool     crossed = false;
    float    frac    = 0.0f;       // where in the sample interval we crossed
    float    xdir    = 0.0f;       // direction through the plane
    uint32_t t_cross = now;        // INTERPOLATED crossing instant
    if (hasLine) {
        SfGate gt; buildSfGate(tIdx, &gt);
        if (gt.valid && lapTimer.have_prev) {
            crossed = sfGateCross(gt, lapTimer.prev_lat, lapTimer.prev_lon,
                                  g.lat_deg, g.lon_deg, &frac, &xdir);
            if (crossed) {
                // Direction gate: once armed, only count passes the SAME way
                // through the plane (a 50 m gate can otherwise reach the pit
                // lane or an antiparallel piece of tarmac).
                if (lapTimer.timing_started && lapTimer.have_sf_dir
                    && xdir * lapTimer.sf_dir < 0.0f) {
                    crossed = false;
                } else if (t_prev_fix != 0 && now > t_prev_fix) {
                    t_cross = t_prev_fix + (uint32_t)(frac * (float)(now - t_prev_fix));
                }
            }
        }
        lapTimer.prev_lat = g.lat_deg;
        lapTimer.prev_lon = g.lon_deg;
        lapTimer.have_prev = true;
    } else {
        const float sfKm = trackDistanceKm(g.lat_deg, g.lon_deg, aLat, aLon);
        if (!lapTimer.left_start && sfKm > LAP_RADIUS_KM * 2.0f) lapTimer.left_start = true;
        if (lapTimer.left_start && sfKm <= LAP_RADIUS_KM) crossed = true;
    }

    // Lap time is measured between INTERPOLATED crossing instants, so it no
    // longer quantizes to the sample grid or depends on sample phase.
    const uint32_t elapsed_x = t_cross - lapTimer.lap_start_ms;
    // MIN_LAP_MS guards double-triggers on COMPLETED laps only — the FIRST
    // crossing merely arms the timer and must never be rejected (starting a
    // recording shortly before the line used to silently cost a whole lap).
    if (crossed && (!lapTimer.timing_started || elapsed_x >= MIN_LAP_MS)) {
        if (lapTimer.timing_started) {
            lapTimer.lap_number++;            // completed a lap -> now driving the next
            // Clean completed lap — record it (interpolated, sub-sample exact).
            const uint32_t elapsed = elapsed_x;
            lapTimer.last_lap_ms   = elapsed;
            lapTimer.last_lap_dist = lapTimer.dist_miles;
            // Track the session's fastest lap and snapshot it as the predictive
            // reference ("ghost") the live delta compares against.
            if (lapTimer.best_lap_ms == 0 || elapsed < lapTimer.best_lap_ms) {
                lapTimer.best_lap_ms   = elapsed;
                lapTimer.best_lap_dist = lapTimer.dist_miles;
                int nb = lapTimer.cur_bucket;
                if (nb > LAP_BUCKETS) nb = LAP_BUCKETS;
                memcpy(lapRefBt, lapCurBt, sizeof(uint32_t) * (size_t)nb);
                lapTimer.ref_buckets = nb;
                lapTimer.ref_valid   = true;
            }
            // Arm the finish-line lap-time popup (drawn by drawDashPage).
            if (s.lap_overlay_s > 0) {
                lap_overlay_lap_ms   = elapsed;
                lap_overlay_lapn     = lapTimer.lap_number - 1;   // just-completed lap
                lap_overlay_is_best  = (lapTimer.best_lap_ms == elapsed);
                lap_overlay_until_ms = now + (uint32_t)s.lap_overlay_s * 1000UL;
            }
        } else {
            lapTimer.lap_number = 1;          // first crossing -> begin lap 1
        }
        // First crossing just arms the timer; subsequent ones record lap times.
        if (!lapTimer.have_sf_dir && xdir != 0.0f) {   // learn the racing direction
            lapTimer.sf_dir      = xdir;
            lapTimer.have_sf_dir = true;
        }
        lapTimer.timing_started = true;
        lapTimer.lap_start_ms   = t_cross;   // interpolated, not the sample time
        lapTimer.dist_miles     = 0.0f;
        lapTimer.left_start     = false;
        lapTimer.prev_gps_ms    = now;
        lapTimer.cur_bucket     = 0;   // start a fresh ghost trace for the new lap
    }
}

// Live delta (ms) vs the reference/best lap at the SAME distance into the lap
// (the "ghost car" gap). Negative = ahead of best pace, positive = behind.
// Returns INT32_MIN when there's no reference lap yet or the current lap hasn't
// started timing. Linear-interpolates between ghost buckets for a smooth value.
static int32_t liveDeltaMs() {
    if (!lapTimer.active || !lapTimer.ref_valid) return INT32_MIN;
    if (!lapTimer.timing_started)                return INT32_MIN;
    if (lapTimer.ref_buckets < 2)                return INT32_MIN;
    const uint32_t elapsed = millis() - lapTimer.lap_start_ms;

    const float fb = lapTimer.dist_miles / LAP_BUCKET_MI;
    int k = (int)fb;
    uint32_t refE;
    if (k <= 0) {
        refE = lapRefBt[0];
    } else if (k >= lapTimer.ref_buckets - 1) {
        refE = lapRefBt[lapTimer.ref_buckets - 1];   // past the ghost's end — clamp
    } else {
        const float frac = fb - (float)k;
        refE = lapRefBt[k] + (uint32_t)((float)(lapRefBt[k + 1] - lapRefBt[k]) * frac);
    }
    return (int32_t)elapsed - (int32_t)refE;
}

// Predicted final lap time = best lap + the live delta. As you drive, the delta
// converges to the true difference, so PRED converges to the real lap time.
// Returns 0 when there's no reference lap / not enough data yet.
static uint32_t predictiveLapMs() {
    if (lapTimer.best_lap_ms == 0) return 0;
    const int32_t d = liveDeltaMs();
    if (d == INT32_MIN) return 0;
    int32_t p = (int32_t)lapTimer.best_lap_ms + d;
    if (p < 0) p = 0;
    return (uint32_t)p;
}

// Signed predictive lap delta vs the session's BEST lap, live. INT32_MIN when
// there's no best lap yet or the current lap hasn't begun timing.
static int32_t predictiveDeltaMs() {
    return liveDeltaMs();
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
        // Optional 3rd field: hex error code from the Teensy SdFat probe.
        // Helps the user see *why* the card didn't mount (eg 0x14 == init
        // timeout, 0x06 == CMD8 fail). Empty when no extra info.
        if (c2 >= 0) {
            String e = line.substring(c2 + 1);
            e.trim();
            e.toCharArray(sd_err_hex, sizeof(sd_err_hex));
        } else {
            sd_err_hex[0] = '\0';
        }
    } else if (tag == "FMT") {
        sd_card_status = 1; sd_free_mb = 0;
        sd_total_mb = (c2 >= 0) ? line.substring(c2 + 1, c3 >= 0 ? c3 : (int)line.length()).toInt() : 0;
    } else if (tag == "READY") {
        sd_card_status = 2;
        sd_total_mb = (c2 >= 0) ? line.substring(c2 + 1, c3 >= 0 ? c3 : (int)line.length()).toInt() : 0;
        sd_free_mb  = (c3 >= 0) ? line.substring(c3 + 1).toInt() : 0;
        sd_err_hex[0] = '\0';   // clear stale error
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

// CANSNIFF,<0|1>,<filename>,<frames> — CAN sniffer status from the Teensy.
static bool parseCanSniffLine(const String& line) {
    const int c1 = line.indexOf(',');
    if (c1 < 0) return false;
    const int c2 = line.indexOf(',', c1 + 1);
    const int c3 = (c2 >= 0) ? line.indexOf(',', c2 + 1) : -1;
    cansniff_active = (line.substring(c1 + 1, c2 >= 0 ? c2 : (int)line.length()).toInt() != 0);
    if (c2 >= 0) {
        const int end = (c3 >= 0) ? c3 : (int)line.length();
        line.substring(c2 + 1, end).toCharArray(cansniff_file, sizeof(cansniff_file));
    } else {
        cansniff_file[0] = '\0';
    }
    cansniff_frames = (c3 >= 0) ? line.substring(c3 + 1).toInt() : 0;
    if (currentPage == PAGE_TOOLS) pageJustEntered = true;   // repaint tools button
    return true;
}

// CANDIAG,<frames/s>,<total>,<base_hits>,<dup%>,<ACK_ERR> — CAN health, 1 Hz.
static bool parseCanDiagLine(const String& line) {
    int idx[6], n = 0;
    for (int i = 0; i < (int)line.length() && n < 6; ++i)
        if (line[i] == ',') idx[n++] = i;
    if (n < 1) return false;
    idx[n] = line.length();
    auto field = [&](int k) { return line.substring(idx[k] + 1, idx[k + 1]); };
    candiag_fps   = (uint32_t)field(0).toInt();
    if (n >= 2) candiag_total   = (uint32_t)field(1).toInt();
    if (n >= 3) candiag_base    = (uint8_t)field(2).toInt();
    if (n >= 4) candiag_dup_pct = (uint8_t)field(3).toInt();
    if (n >= 5) candiag_ack_err = (field(4).toInt() != 0);
    if (n >= 6) candiag_tx_err  = (uint8_t)field(5).toInt();
    if (n >= 7) candiag_rx_err  = (uint8_t)field(6).toInt();
    if (n >= 8) candiag_txtest  = (uint8_t)field(7).toInt();
    candiag_ms = millis();
    return true;
}

// Pop the modal full-screen, save the current page so we can restore it after.
static void btReleaseRadio();   // defined in the radio-arbiter section below
static void openUploadModal(const char* filename, uint32_t total) {
    // Uploads need every scrap of WiFi throughput — a live BLE link (or its
    // 1.5 s-cadence reconnect attempts) time-slices the single 2.4 GHz radio
    // and stretches TCP writes past the Teensy's UART-ack patience. BLE off.
    btReleaseRadio();
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

// ---------------------------------------------------------------------------
// Dash-initiated upload flow (replaces the old Teensy-initiated WUP push).
//
// Flow:
//   user taps UPLOAD button on PAGE_DASH
//     -> openUploadModal('...', 0)
//     -> uf state = UF_LISTING; emit 'Q,LIST\n' to Teensy
//     -> Teensy responds: Q,FILE,<name>,<size> per file, then Q,END
//     -> for each file:
//          state = UF_FETCH_HEAD; emit 'Q,GET,<name>\n'
//          Teensy responds: Q,DATA,<name>,<size>
//            dash opens a raw TCP (or TLS) socket to s.cloud_host:port and
//            writes the HTTP request headers (Content-Length = file size).
//            state = UF_STREAMING
//          Teensy streams Q,L,<line> ... we forward each line + '\n' to the
//            open socket, incrementing bytes_written. No PSRAM staging.
//          Q,EOF arrives: verify bytes_written == expected_size for integrity,
//            flush socket, state = UF_STREAM_FINISH
//        uploadTick drains the response progressively, parses HTTP status
//          on 2xx: emit 'Q,DEL,<name>\n'; state = UF_DELETING
//          on fail: skip; advance to next file (file stays on SD)
//        on Q,DEL,OK: advance to next file
//     -> when all files processed: state = UF_DONE; show summary banner
// ---------------------------------------------------------------------------
struct UfPendingFile {
    char     name[80];
    uint32_t size;
};

struct UploadFlow {
    UploadFlowState state;
    uint32_t state_entered_ms;
    UfPendingFile files[16];
    int      files_n;
    int      files_idx;
    int      uploaded;
    int      failed;
    uint16_t del_mask;          // bit i = files[i] uploaded OK, delete at batch end (v0.1.136)
    // Streaming TCP socket. Owned. Polymorphic across WiFiClient (HTTP) and
    // WiFiClientSecure (HTTPS); deleted via base virtual destructor.
    WiFiClient* tcp;
    bool       tcp_secure;
    uint32_t   expected_size;   // Content-Length value (from Q,DATA)
    uint32_t   bytes_written;   // bytes written to socket so far (line + '\n' per Q,L)
    uint32_t   lines_recv;      // Q,L count for current file (debug + DBG progress)
    uint32_t   last_rx_ms;      // most recent UART or socket activity (for stall watchdog)
    char       response[1024];  // accumulated HTTP response (headers + body)
    size_t     response_len;
    char       last_err[180];
    uint8_t    file_retries;    // per-file retry count (2 automatic retries max)
    uint8_t    list_tries;      // Q,LIST re-asks after a 6 s silence (v0.1.121)
    uint32_t   retry_at_ms;     // when UF_RETRY_WAIT re-sends Q,GET / UF_POSTING re-opens
    // Segmented store-and-forward (v0.1.113). The file is pulled over UART
    // into a PSRAM segment FIRST (network completely idle), then POSTed as a
    // plain Content-Length body (UART completely idle), then the next segment
    // is pulled (Q,GET,<name>,<skip_lines>). The two links never wait on each
    // other — which is what killed the old chunked-at-UART-pace design.
    uint32_t   seg_start_line;  // (legacy, unused since v0.1.114)
    uint32_t   bytes_done;      // (legacy, unused since v0.1.114)
    bool       seg_eof;         // (legacy, unused since v0.1.114)
    uint8_t    post_tries;      // (legacy, unused since v0.1.114)
    // TRUE STREAMING uploader (v0.1.114). The loop (core 1) pushes UART lines
    // into a PSRAM ring; a dedicated writer task (core 0) owns the socket
    // end-to-end (TLS handshake, headers, chunked body, response) and drains
    // the ring at network speed. Neither side ever waits on the other — the
    // file streams car -> cloud in ONE continuous pass, and a slow network
    // shows up only as the ring filling (UART ARQ backpressure), never as a
    // blocked UI loop or a starved ACK stream.
    volatile uint32_t ring_head;   // total bytes queued by the UART producer
    volatile uint32_t ring_tail;   // total bytes sent by the net task
    volatile bool     net_eof;     // producer: file fully queued
    volatile bool     net_abort;   // loop asks the task to bail + close
    volatile uint8_t  net_state;   // 0 idle / 1 running / 2 done / 3 failed
    char              net_err[96]; // task-written failure reason
    uint32_t          fin_tail;    // FINISH stall watchdog: last seen tail
    uint32_t          fin_ms;      //   ...and when it last moved
    // Whole-file staging buffer (PSRAM). The complete session file is pulled
    // off the Teensy/SD into here FIRST, then POSTed to the cloud in one clean
    // request (see UF_POSTING). Staging fully decouples the UART transfer from
    // the network, so a slow/stalled TLS write can no longer desync the Teensy
    // stream and kill the upload mid-flight (the old per-line-to-TLS design).
    uint8_t*   buf;             // ps_malloc'd, freed by ufFreeBuf()
    uint32_t   bufcap;          // advertised file size from Q,DATA
    uint32_t   buflen;          // bytes staged so far
    uint32_t   post_off;        // body bytes written to the socket so far
    bool       post_started;    // request headers written, body in progress
    uint32_t   next_seq;        // next expected Q,L sequence number (ARQ dedup)
};
static UploadFlow uf = {};
static constexpr uint32_t UF_CHUNK_BUF = 128UL * 1024;       // streaming flush buffer

static void ufCloseTcp() {
    if (uf.tcp) {
        uf.tcp->stop();
        delete uf.tcp;
        uf.tcp = nullptr;
    }
    uf.tcp_secure   = false;
    uf.expected_size = 0;
    uf.bytes_written = 0;
    uf.lines_recv   = 0;
    uf.response_len = 0;
    uf.response[0]  = '\0';
    uf.post_off     = 0;
    uf.post_started = false;
}

// Free the PSRAM chunk buffer between files.
static void ufFreeBuf() {
    if (uf.buf) { free(uf.buf); uf.buf = nullptr; }
    uf.bufcap       = 0;
    uf.buflen       = 0;
    uf.post_off     = 0;
    uf.post_started = false;
    zbFree();   // compressor buffers live only while a stream is running
                // (callers already guarantee the net task is stopped here)
}

// (ufWriteAll / ufFlushChunk deleted in v0.1.113 — the chunked mid-stream
// socket writes were the coupling that killed uploads; the POST now happens
// from staged PSRAM inside uploadTick()'s UF_POSTING driver.)

// Ask the net task to bail and wait for it to exit (bounded). MUST be called
// before ufCloseTcp()/ufFreeBuf() whenever a stream might be live — the task
// owns uf.tcp and reads uf.buf, so tearing those down under it is a crash.
static void ufStopNetTask() {
    if (uf.net_state == 1) {
        uf.net_abort = true;
        const uint32_t t0 = millis();
        while (uf.net_state == 1 && millis() - t0 < 3000) delay(5);
    }
    uf.net_state = 0;
    uf.net_abort = false;
}

static void ufReset() {
    // Free a possibly-wedged Teensy sender FIRST (v0.1.121): if a previous
    // stream died without a clean abort, the Teensy sits in its patient
    // retransmit loop and eats our next request as a stray line.
    Serial.printf("Q,ABORT\n");
    Serial.flush();
    ufStopNetTask();
    ufCloseTcp();
    ufFreeBuf();
    memset(&uf, 0, sizeof(uf));
    uf.state = UF_IDLE;
}

static void ufEnter(UploadFlowState s) {
    uf.state = s;
    uf.state_entered_ms = millis();
    uf.last_rx_ms       = millis();
}

static void ufStartListing() {
    ufReset();
    ufEnter(UF_LISTING);
    Serial.println("Q,LIST");
    Serial.flush();
}


// Fire the deferred Q,DEL for every file that uploaded OK this batch (see the
// del_mask note in the UF_STREAM_FINISH success path). Fire-and-forget: the
// Q,DEL,OK replies are ignored, because a lost delete only costs one harmless
// re-upload next time, whereas doing these mid-batch costs 90 s of SD stall.
static void ufFlushPendingDeletes() {
    if (!uf.del_mask) return;
    for (int i = 0; i < uf.files_n && i < 16; ++i) {
        if (uf.del_mask & (uint16_t)(1u << i)) {
            Serial.printf("Q,DEL,%s\n", uf.files[i].name);
            Serial.flush();
            delay(2);   // don't blast 16 deletes into one UART burst
        }
    }
    uf.del_mask = 0;
}

static void ufStartCurrentFile() {
    if (uf.files_idx >= uf.files_n) {
        ufFlushPendingDeletes();   // batch done — now it's safe to delete
        ufEnter(UF_DONE);
        return;
    }
    uf.seg_start_line = 0;
    uf.bytes_done     = 0;
    uf.seg_eof        = false;
    uf.post_tries     = 0;
    Serial.printf("Q,GET,%s\n", uf.files[uf.files_idx].name);
    Serial.flush();
    ufEnter(UF_FETCH_HEAD);
}

static void ufNextFile() {
    ufStopNetTask();
    ufCloseTcp();
    ufFreeBuf();
    uf.files_idx++;
    uf.file_retries = 0;
    ufStartCurrentFile();
}

// Fail the current file OR schedule ONE automatic retry of it (fresh Q,GET +
// fresh TCP). v0.1.112 — before this, ANY mid-stream hiccup (WiFi stall, ack
// timeout) burned the file until the next manual drain, which is why uploads
// felt spotty. sendAbort=true also tells the Teensy to bail out of its (now
// 60 s patient) go-back-N retransmit loop FIRST — otherwise our retry's Q,GET
// line would be eaten as a stray inside its ack-pump and the retry would hang.
static void ufDiagReport(const char* why);   // defined near the net task

static void ufFailOrRetry(bool sendAbort) {
    ufDiagReport("fail");   // snapshot BEFORE teardown mutates the state
    ufStopNetTask();
    ufCloseTcp();
    if (sendAbort) { Serial.printf("Q,ABORT\n"); Serial.flush(); }
    if (uf.file_retries < 2 && uf.files_idx < uf.files_n) {
        uf.file_retries++;
        Serial.printf("DBG,uf_retry file=%s err=%s\n",
                      uf.files[uf.files_idx].name, uf.last_err);
        uf.retry_at_ms = millis() + 800;   // let the Teensy exit its loop + settle
        ufEnter(UF_RETRY_WAIT);
    } else {
        uf.failed++;
        ufNextFile();
    }
}

// (ufPostFailRetry removed in v0.1.114 — there is no staged-segment POST any
// more; a network failure is a whole-file retry via ufFailOrRetry.)

// Parse the unix-epoch session id out of a 'session_<epoch>_<track>.ndjson'
// filename; falls back to millis() when the filename doesn't match.
static long ufExtractSessionId(const char* filename) {
    if (filename && strncmp(filename, "session_", 8) == 0) {
        const long v = strtol(filename + 8, nullptr, 10);
        if (v > 0) return v;
    }
    return (long)millis();
}

// Extract the clean track name from a session filename. Files are named
//   session_<unix>_<track>.ndjson      (or session_nortc_<ms>_<track>.ndjson)
// so we skip the "session_" prefix + the numeric id, take what's between the
// next '_' and the trailing ".ndjson". Writes into out (always NUL-terminated).
// Falls back to "UNKNOWN" if the name doesn't match the expected shape.
static void ufExtractTrack(const char* filename, char* out, size_t outsize) {
    if (outsize == 0) return;
    out[0] = '\0';
    if (!filename) { strncpy(out, "UNKNOWN", outsize); out[outsize-1] = '\0'; return; }

    const char* p = filename;
    if (strncmp(p, "session_", 8) == 0) p += 8;        // skip "session_"
    if (strncmp(p, "nortc_", 6) == 0)  p += 6;         // skip "nortc_" if present
    // p now points at the numeric id; skip digits, then the separating '_'.
    while (*p >= '0' && *p <= '9') ++p;
    if (*p == '_') ++p;
    // p now points at the track portion. Copy until ".ndjson" / end.
    size_t n = 0;
    while (p[n] && n + 1 < outsize) {
        // stop at the ".ndjson" extension
        if (p[n] == '.' && strcmp(p + n, ".ndjson") == 0) break;
        out[n] = p[n];
        ++n;
    }
    out[n] = '\0';
    if (n == 0) { strncpy(out, "UNKNOWN", outsize); out[outsize-1] = '\0'; }
}

// Open the TCP/TLS socket to the cloud and write the HTTP request head with a
// fixed Content-Length (v0.1.113: chunked encoding removed — plain sized POSTs
// are the shape that measured ~850 KB/s where chunked died). path selects the
// server endpoint: "/upload" (mode=w, first segment) or "/stream" (mode=a,
// append — subsequent segments of the same file).
// ---------------------------------------------------------------------------
// zblocks upload compression (v0.1.127). Session NDJSON is ~75-90% redundant;
// the dash→cloud socket is hard-capped ~70 KB/s by lwIP's 5744 B send buffer
// ÷ path RTT (verified by the two-pass speed test + sdkconfig). Compressing
// the body makes that socket carry ~4x more RAW telemetry, moving the upload
// bottleneck back to the UART wire. Framing: the chunked HTTP body becomes a
// sequence of frames ['Z','B', u32le raw_len, u32le comp_len, <raw-deflate>]
// — each frame an INDEPENDENT ≤32 KB deflate stream (server: zlib -15).
// Negotiated per boot via GET /caps ("zblocks":true); an old server without
// it gets the legacy raw body, so dash/server update order doesn't matter.
// All of this runs on the NET TASK only — no Serial prints, no loop stalls.
// ---------------------------------------------------------------------------
static bool ufTaskWrite(const uint8_t* d, size_t len);   // fwd (net task writer)

// (declared here, used by the Tools page far below: zbFree() must not free
// the compressor buffers while the WIFI SPEED TEST task is using them)
static volatile uint8_t nettest_state = 0;   // 0=idle 1=running 2=done

static int8_t   srv_zblocks = -1;      // /caps probe: -1 unknown, 0 no, 1 yes
static ZDeflWS* zb_ws  = nullptr;      // 72 KB PSRAM workspace (lazy, kept)
static uint8_t* zb_raw = nullptr;      // 32 KB staging block
static uint8_t* zb_out = nullptr;      // compressed output (bound: raw+16)
static uint32_t zb_fill = 0;           // bytes staged in zb_raw
static bool     zb_stream_on = false;  // this stream is compressed
static volatile uint32_t zb_wire = 0;  // compressed bytes sent (diagnostics)

// ⚠️ INTERNAL RAM first (v0.1.128): the workspace access pattern is random
// (hash probes + chain walks per byte); in PSRAM every probe is an OPI-bus
// cache miss fought over with the RGB scanout, and v0.1.127 measured the
// compressor at ~25 KB/s that way (slower than the raw path!). Internal RAM
// runs it at hundreds of KB/s. ~73 KB total, allocated only while a stream/
// speed test needs it and freed by zbFree() so BLE's ~64 KB heap guard still
// clears when recording starts. PSRAM fallback keeps it functional (slow) if
// internal heap is tight.
static bool zbEnsure() {
    if (!zb_ws)  zb_ws  = (ZDeflWS*)heap_caps_malloc(sizeof(ZDeflWS),
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!zb_ws)  zb_ws  = (ZDeflWS*)ps_malloc(sizeof(ZDeflWS));
    if (!zb_raw) zb_raw = (uint8_t*)heap_caps_malloc(ZDEF_BLOCK_MAX,
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!zb_raw) zb_raw = (uint8_t*)ps_malloc(ZDEF_BLOCK_MAX);
    if (!zb_out) zb_out = (uint8_t*)heap_caps_malloc(ZDEF_BOUND(ZDEF_BLOCK_MAX),
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!zb_out) zb_out = (uint8_t*)ps_malloc(ZDEF_BOUND(ZDEF_BLOCK_MAX));
    return zb_ws && zb_raw && zb_out;
}

static void zbFree() {
    if (nettest_state == 1) return;   // speed test owns the buffers right now
    if (zb_ws)  { free(zb_ws);  zb_ws  = nullptr; }
    if (zb_raw) { free(zb_raw); zb_raw = nullptr; }
    if (zb_out) { free(zb_out); zb_out = nullptr; }
    zb_fill = 0;
    zb_stream_on = false;
}

// One-time (per boot) server capability probe. Runs on the net/nettest task
// (blocking TLS is fine there). Only DEFINITIVE answers are cached: a dead
// connection leaves -1 so the next attempt re-probes.
static void zbProbeCaps() {
    if (srv_zblocks != -1) return;
    if (s.internet_mode != 1 || !wifiConnectedNow()) return;
    WiFiClient* c = nullptr;
    if (s.cloud_protocol == 1) {
        WiFiClientSecure* sec = new WiFiClientSecure();
        sec->setInsecure();
        sec->setTimeout(8);
        c = sec;
    } else {
        c = new WiFiClient();
        c->setTimeout(8);
    }
    if (c->connect(s.cloud_host, s.cloud_port)) {
        c->printf("GET /caps HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n",
                  s.cloud_host);
        char resp[512]; size_t rn = 0;
        const uint32_t t0 = millis();
        while (millis() - t0 < 5000) {
            while (c->available() && rn + 1 < sizeof(resp)) {
                const int ch = c->read();
                if (ch < 0) break;
                resp[rn++] = (char)ch;
            }
            resp[rn] = '\0';
            if (rn + 1 >= sizeof(resp)) break;
            if (!c->connected() && !c->available()) break;
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        resp[rn] = '\0';
        if (strstr(resp, "HTTP/1.")) {   // got a real answer -> cache it
            srv_zblocks = (strstr(resp, "\"zblocks\":true") ||
                           strstr(resp, "\"zblocks\": true")) ? 1 : 0;
        }
    }
    c->stop();
    delete c;
}

// Compress + send the staged block as one HTTP chunk. NET TASK ONLY.
static bool zbFlushBlock() {
    if (zb_fill == 0) return true;
    const size_t cl = zdeflate(zb_ws, zb_raw, zb_fill, zb_out,
                               ZDEF_BOUND(ZDEF_BLOCK_MAX));
    if (cl == 0) {
        snprintf(uf.net_err, sizeof(uf.net_err), "zb compress fail");
        return false;
    }
    uint8_t fh[10] = {'Z', 'B'};
    const uint32_t rl = zb_fill, cl32 = (uint32_t)cl;
    memcpy(fh + 2, &rl,   4);
    memcpy(fh + 6, &cl32, 4);
    char hdr[12];
    const int hn = snprintf(hdr, sizeof(hdr), "%x\r\n", (unsigned)(10 + cl));
    if (!ufTaskWrite((const uint8_t*)hdr, (size_t)hn) ||
        !ufTaskWrite(fh, 10) ||
        !ufTaskWrite(zb_out, cl) ||
        !ufTaskWrite((const uint8_t*)"\r\n", 2)) return false;
    zb_wire += 10 + (uint32_t)cl;
    zb_fill = 0;
    return true;
}

static bool ufOpenStream(uint32_t content_length, const char* path) {
    if (s.cloud_host[0] == '\0' || s.cloud_port == 0) {
        snprintf(uf.last_err, sizeof(uf.last_err), "cloud host/port unset");
        return false;
    }
    if (s.internet_mode != 1 || !wifiConnectedNow()) {
        snprintf(uf.last_err, sizeof(uf.last_err), "WiFi not connected");
        return false;
    }
    ufCloseTcp();
    // The TLS handshake (WiFiClientSecure + setInsecure, full server cert
    // chain) can take several seconds over the car's WiFi while the RGB
    // display + UART streaming keep the loop busy. The old 8 s cap was too
    // tight and the handshake was timing out "almost every time" — the OTA
    // HTTPS path uses 15 s and connects reliably, so match it. Per-line TCP
    // writes during streaming almost never block this long; if one does, the
    // stall watchdog in uploadTick() still catches it.
    constexpr int CLOUD_TCP_TIMEOUT_S = 15;
    // NOTE: only ESP.getFreeHeap() here (a cheap maintained counter). Do NOT
    // call heap_caps_get_largest_free_block() — it walks the whole heap inside
    // a critical section (interrupts off) and on the Advance's large heap that
    // walk trips the Interrupt WDT -> panic/reboot mid-upload (v0.1.70/0.1.71).
    // ⚠️ NO Serial prints in here since v0.1.114: this runs on the NET TASK,
    // and UART0 is the Teensy link — an interleaved print would corrupt the
    // Q,A ack stream the loop is emitting concurrently.
    //
    // v0.1.134: CONNECT WITH RETRIES, and record the free INTERNAL heap in the
    // error. The TLS handshake needs tens of KB of INTERNAL heap (mbedtls
    // buffers + cert parse); a single failed attempt used to abort the whole
    // file, which is what produced "TCP connect failed" -> ring fills ->
    // dash stops acking -> Teensy stalls -> 90 s watchdog -> repeat forever.
    // A failed WiFiClientSecure can also be left in a bad state, so the client
    // is destroyed and recreated on each attempt.
    bool connected = false;
    for (int attempt = 0; attempt < 3 && !connected; ++attempt) {
        if (attempt) vTaskDelay(pdMS_TO_TICKS(500));
        ufCloseTcp();                       // fresh client every attempt
        if (s.cloud_protocol == 1) {
            WiFiClientSecure* sec = new WiFiClientSecure();
            sec->setInsecure();   // TODO: pin server cert when going public
            sec->setTimeout(CLOUD_TCP_TIMEOUT_S);
            uf.tcp = sec;
            uf.tcp_secure = true;
        } else {
            WiFiClient* plain = new WiFiClient();
            plain->setTimeout(CLOUD_TCP_TIMEOUT_S);
            uf.tcp = plain;
            uf.tcp_secure = false;
        }
        if (!uf.tcp) continue;
        connected = uf.tcp->connect(s.cloud_host, s.cloud_port);
    }
    if (!connected) {
        snprintf(uf.last_err, sizeof(uf.last_err), "TCP connect failed (heap %u)",
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        ufCloseTcp();
        return false;
    }
    // Disable Nagle so per-line writes go out immediately rather than waiting
    // for the TCP coalescer. The Teensy is paced by Q,A acks anyway, so we
    // never benefit from coalescing and Nagle just adds latency to each ACK
    // round trip — which directly slows the upload.
    uf.tcp->setNoDelay(true);
    const long sid = ufExtractSessionId(uf.files[uf.files_idx].name);
    char track[52];
    ufExtractTrack(uf.files[uf.files_idx].name, track, sizeof(track));
    uf.tcp->printf("POST %s HTTP/1.1\r\n", path);
    uf.tcp->printf("Host: %s\r\n",            s.cloud_host);
    uf.tcp->printf("Content-Type: application/x-ndjson\r\n");
    // ⚠️ CHUNKED, not Content-Length (v0.1.118 fix). The net task writes the
    // body with chunked FRAMING ("<hex>\r\n...\r\n" + "0\r\n\r\n") — v0.1.114
    // accidentally dropped this header while keeping the framing, so the
    // server read Content-Length bytes of chunk-framed data, the counts never
    // lined up, and EVERY streamed upload died ~30 s in (ClientDisconnect).
    // Chunked is also the only honest choice for a stream: the exact body
    // size isn't knowable up front (UART lines are re-framed with '\n').
    (void)content_length;   // used for the progress bar by the caller only
    uf.tcp->printf("Transfer-Encoding: chunked\r\n");

    uf.tcp->printf("X-API-Key: %s\r\n",       s.cloud_auth_pass);
    uf.tcp->printf("X-User-Email: %s\r\n",    s.cloud_auth_user);
    uf.tcp->printf("X-Session-Id: %ld\r\n",   sid);
    uf.tcp->printf("X-Track-Name: %s\r\n",    track);
    // Companion debug logs ("<session>.dbg.ndjson") get filed under debug/ on
    // the server instead of overwriting the real session (same id+track).
    if (strstr(uf.files[uf.files_idx].name, ".dbg."))
        uf.tcp->printf("X-File-Kind: debug\r\n");
    uf.tcp->printf("Connection: close\r\n");
    // ⚠️ Headers deliberately NOT terminated here (v0.1.134). The caller
    // (ufNetTask) decides about zblocks compression only AFTER this handshake
    // succeeds — so it appends X-Body-Format (if any) and the final blank
    // line. Nothing else calls this function.
    uf.response_len  = 0;   // expected_size/bytes_written managed by the caller:
    return true;             // total file size for the modal, not this segment's
}

// ---------------------------------------------------------------------------
// Streaming net task (v0.1.114). Core 0. Owns uf.tcp for the whole stream:
// connect + TLS handshake + headers happen HERE (the ~15 s handshake no longer
// stalls the UI or the UART pump), then it drains the PSRAM ring as HTTP
// chunks at network speed, writes the chunked terminator once the producer
// flags EOF, and reads the HTTP response. It NEVER touches Serial (UART0 is
// the Teensy link) — all diagnostics go through uf.net_err / uf.net_state.
// ---------------------------------------------------------------------------
static bool ufTaskWrite(const uint8_t* d, size_t len) {
    size_t off = 0;
    uint32_t last = millis();
    while (off < len) {
        if (uf.net_abort) { snprintf(uf.net_err, sizeof(uf.net_err), "aborted"); return false; }
        if (!uf.tcp || !uf.tcp->connected()) {
            snprintf(uf.net_err, sizeof(uf.net_err),
                     "TCP closed at %lu B", (unsigned long)uf.ring_tail);
            return false;
        }
        const int w = uf.tcp->write(d + off, len - off);
        if (w > 0) { off += (size_t)w; last = millis(); }
        else if (millis() - last > 90000) {
            // 90 s (v0.1.115, was 20 s): paddock WiFi (hotspots, congestion)
            // measurably drops to ZERO throughput for 30 s+ windows — ride
            // them out on the ring + the Teensy's 120 s ARQ patience instead
            // of aborting the file (server log 07-17: uploads on the same
            // AP went 66 KB/s -> 0 -> fine again minutes later).
            snprintf(uf.net_err, sizeof(uf.net_err),
                     "TCP write stalled at %lu B", (unsigned long)uf.ring_tail);
            return false;
        } else vTaskDelay(1);
    }
    return true;
}

// Fire-and-forget diagnostics: POST a 0-byte /nettest whose X-Note carries a
// compact snapshot of the uploader (state, net task state, ring head/tail,
// lines, last error, file). Lands in the server's upload event log
// (ev=nettest, note=ufdiag…) so failures in the field are inspectable
// remotely without USB access (v0.1.119).
static char          ufdiag_note[176];
static volatile bool ufdiag_busy = false;

// One short-lived task doing EITHER a fetch (coach_tick_id empty) or a tick.
// Never prints to Serial (UART0 is the Teensy link).
static void coachTask(void*) {
    WiFiClient* c = nullptr;
    if (s.cloud_protocol == 1) {
        WiFiClientSecure* sec = new WiFiClientSecure();
        sec->setInsecure(); sec->setTimeout(10); c = sec;
    } else {
        c = new WiFiClient(); c->setTimeout(10);
    }
    const bool ticking = (coach_tick_id[0] != '\0');
    if (c->connect(s.cloud_host, s.cloud_port)) {
        c->setNoDelay(true);
        if (ticking) {
            char body[64];
            const int bn = snprintf(body, sizeof(body),
                                    "{\"id\":\"%s\",\"by\":\"display\"}", coach_tick_id);
            c->printf("POST /coach/%s/done HTTP/1.1\r\nHost: %s\r\n"
                      "Content-Type: application/json\r\nX-API-Key: %s\r\n"
                      "Content-Length: %d\r\nConnection: close\r\n\r\n%s",
                      s.cloud_auth_user, s.cloud_host, s.cloud_auth_pass, bn, body);
        } else {
            c->printf("GET /coach/%s/open HTTP/1.1\r\nHost: %s\r\n"
                      "X-API-Key: %s\r\nConnection: close\r\n\r\n",
                      s.cloud_auth_user, s.cloud_host, s.cloud_auth_pass);
        }
        // Collect the response (small JSON).
        static char resp[2048];
        size_t rn = 0; const uint32_t t0 = millis();
        while (millis() - t0 < 10000) {
            while (c->available() && rn + 1 < sizeof(resp)) resp[rn++] = (char)c->read();
            resp[rn] = '\0';
            if (!c->connected() && !c->available()) break;
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        resp[rn] = '\0';
        if (!ticking && strstr(resp, "\"ok\"")) {
            // Minimal scrape: walk "id":".." / "text":".." pairs in order. A real
            // JSON parser isn't worth the flash for a fixed 3-field shape.
            int n = 0;
            const char* p = resp;
            while (n < COACH_MAX) {
                const char* idp = strstr(p, "\"id\":\"");
                if (!idp) break;
                idp += 6;
                const char* ide = strchr(idp, '"');
                const char* txp = strstr(ide ? ide : idp, "\"text\":\"");
                if (!ide || !txp) break;
                txp += 8;
                const char* txe = strchr(txp, '"');
                if (!txe) break;
                size_t il = (size_t)(ide - idp), tl = (size_t)(txe - txp);
                if (il >= sizeof(coach_id[0]))  il = sizeof(coach_id[0]) - 1;
                if (tl >= sizeof(coach_txt[0])) tl = sizeof(coach_txt[0]) - 1;
                memcpy(coach_id[n], idp, il);  coach_id[n][il] = '\0';
                memcpy(coach_txt[n], txp, tl); coach_txt[n][tl] = '\0';
                n++;
                p = txe;
            }
            coach_n = n;
            coach_dirty = true;
        } else if (ticking && strstr(resp, "\"ok\"")) {
            // Drop the ticked item locally so the UI updates instantly; the
            // next fetch is authoritative anyway.
            for (int i = 0; i < coach_n; ++i) {
                if (strcmp(coach_id[i], coach_tick_id) == 0) {
                    for (int j = i; j < coach_n - 1; ++j) {
                        strcpy(coach_id[j],  coach_id[j + 1]);
                        strcpy(coach_txt[j], coach_txt[j + 1]);
                    }
                    coach_n--;
                    break;
                }
            }
            coach_dirty = true;
        }
    }
    c->stop();
    delete c;
    coach_tick_id[0] = '\0';
    coach_busy = false;
    vTaskDelete(NULL);
}

// Kick a fetch (id=nullptr) or a tick. No-op if one is already running, if the
// feature is off, or if there's no WiFi/account configured.
static void coachKick(const char* tick_id) {
    if (coach_busy) return;
    if (!s.coach_show) return;
    if (s.internet_mode != 1 || !wifiConnectedNow()) return;
    if (s.cloud_auth_user[0] == '\0') return;
    // ⚠️ NEVER while the uploader or the sessions list owns the link (v0.1.138).
    // This crashed and rebooted the board in 0.1.137: an upload already holds a
    // TLS socket AND ~73 KB of internal RAM for the zblocks compressor, and the
    // periodic coach fetch would open a SECOND TLS session on top of it — two
    // mbedtls contexts in the internal heap at once is straight out of memory.
    // The Teensy link is also mid-ARQ, and a second task adds latency to acks.
    if (uf.state != UF_IDLE || sl.state != SL_IDLE) return;
    if (upload_active) return;
    if (tick_id && tick_id[0]) {
        strncpy(coach_tick_id, tick_id, sizeof(coach_tick_id) - 1);
        coach_tick_id[sizeof(coach_tick_id) - 1] = '\0';
    } else {
        coach_tick_id[0] = '\0';
        coach_last_fetch_ms = millis();
    }
    coach_busy = true;
    // 16 KB stack, NOT 8 KB (v0.1.138). An mbedtls handshake needs well over
    // 8 KB of stack; 0.1.137 gave this task 8192 and it overflowed the moment
    // it did TLS — instant crash + reboot. Every other TLS task here uses
    // 12-16 KB; match the uploader's 16 KB.
    if (xTaskCreatePinnedToCore(coachTask, "coach", 16384, nullptr, 1, nullptr, 0) != pdPASS)
        coach_busy = false;
}

static void ufDiagTask(void*) {
    WiFiClient* c = nullptr;
    if (s.cloud_protocol == 1) {
        WiFiClientSecure* sec = new WiFiClientSecure();
        sec->setInsecure();
        sec->setTimeout(8);
        c = sec;
    } else {
        c = new WiFiClient();
        c->setTimeout(8);
    }
    if (c->connect(s.cloud_host, s.cloud_port)) {
        c->printf("POST /nettest HTTP/1.1\r\nHost: %s\r\nContent-Length: 0\r\n"
                  "X-Rssi: %d\r\nX-Fw: %s\r\nX-Note: %s\r\nConnection: close\r\n\r\n",
                  s.cloud_host, (int)WiFi.RSSI(), FIRMWARE_VERSION, ufdiag_note);
        const uint32_t t0 = millis();
        while (millis() - t0 < 4000 && c->connected()) {
            while (c->available()) (void)c->read();
            vTaskDelay(10);
        }
    }
    c->stop();
    delete c;
    ufdiag_busy = false;
    vTaskDelete(NULL);
}

static void ufDiagReport(const char* why) {
    if (ufdiag_busy) return;                                    // one in flight
    if (s.internet_mode != 1 || !wifiConnectedNow()) return;
    snprintf(ufdiag_note, sizeof(ufdiag_note),
             "ufdiag %s st=%u net=%u rh=%lu rt=%lu lr=%lu ih=%u er=%.40s f=%.28s",
             why, (unsigned)uf.state, (unsigned)uf.net_state,
             (unsigned long)uf.ring_head, (unsigned long)uf.ring_tail,
             (unsigned long)uf.lines_recv,
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             uf.last_err[0] ? uf.last_err : uf.net_err,   // cause before symptom
             (uf.files_idx >= 0 && uf.files_idx < uf.files_n)
                 ? uf.files[uf.files_idx].name : "-");
    ufdiag_busy = true;
    if (xTaskCreatePinnedToCore(ufDiagTask, "ufdiag", 12288, nullptr, 1,
                                nullptr, 0) != pdPASS)
        ufdiag_busy = false;
}

// Minimum INTERNAL heap that must remain free AFTER the TLS handshake before
// we're willing to spend 73 KB of it on the compressor. Below this, stream
// RAW — slower on the wire but it always works, and the server accepts both.
static constexpr uint32_t ZB_MIN_FREE_INTERNAL = 96 * 1024;

static void ufNetTask(void*) {
    bool ok = false;
    zb_stream_on = false;
    zb_fill = 0;
    zb_wire = 0;
    do {
        // ⚠️ ORDER IS THE WHOLE FIX (v0.1.134). Previously zbProbeCaps() +
        // zbEnsure() ran FIRST, taking ~73 KB of INTERNAL RAM (and a TLS
        // connection for /caps) BEFORE this handshake — so mbedtls regularly
        // couldn't get the internal heap it needs and the upload died with
        // "TCP connect failed" before a single body byte moved (proven in the
        // field: rh=524234 rt=0 at RSSI -43). Socket first, compressor second.
        if (!ufOpenStream(uf.expected_size, "/upload")) {
            snprintf(uf.net_err, sizeof(uf.net_err), "%s",
                     uf.last_err[0] ? uf.last_err : "connect failed");
            break;
        }
        // Socket is up. NOW decide about compression, and only if there's
        // comfortable internal heap left over.
        if (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) >= ZB_MIN_FREE_INTERNAL) {
            zbProbeCaps();                      // no-op once answered (per boot)
            if (srv_zblocks == 1) zb_stream_on = zbEnsure();
        }
        if (zb_stream_on) uf.tcp->printf("X-Body-Format: zblocks\r\n");
        uf.tcp->printf("\r\n");                 // end of headers (see ufOpenStream)
        bool werr = false;
        while (!werr) {
            if (uf.net_abort) { snprintf(uf.net_err, sizeof(uf.net_err), "aborted"); werr = true; break; }
            const uint32_t avail = uf.ring_head - uf.ring_tail;
            if (avail == 0) {
                if (uf.net_eof) break;             // drained + nothing more coming
                vTaskDelay(pdMS_TO_TICKS(5));       // producer will catch up
                continue;
            }
            uint32_t n = avail;
            if (n > 16384) n = 16384;
            const uint32_t off = uf.ring_tail % uf.bufcap;
            if (n > uf.bufcap - off) n = uf.bufcap - off;   // stay contiguous
            // BOUNCE through internal RAM (v0.1.117): the ring lives in PSRAM,
            // but on this board the RGB panel scans the framebuffer out of the
            // SAME OPI PSRAM bus the WiFi/TLS stack contends for — sending
            // straight from PSRAM measured ~70 KB/s at RSSI -55 (nettest
            // 07-17). One memcpy into .bss internal RAM decouples the radio
            // path from the display's PSRAM traffic.
            static uint8_t bounce[16384];   // net task only — single user
            memcpy(bounce, uf.buf + off, n);
            if (zb_stream_on) {
                // Stage into the 32 KB compression block; flush each full
                // block as one zblocks frame. Compression (~10-30 ms/block)
                // runs HERE on core 0 between socket writes — the socket
                // sits idle far longer than that waiting on its send window.
                uint32_t done = 0;
                while (done < n) {
                    uint32_t take = ZDEF_BLOCK_MAX - zb_fill;
                    if (take > n - done) take = n - done;
                    memcpy(zb_raw + zb_fill, bounce + done, take);
                    zb_fill += take;
                    done    += take;
                    if (zb_fill == ZDEF_BLOCK_MAX && !zbFlushBlock()) { werr = true; break; }
                }
                if (werr) break;
            } else {
                char hdr[12];
                const int hn = snprintf(hdr, sizeof(hdr), "%x\r\n", (unsigned)n);
                if (!ufTaskWrite((const uint8_t*)hdr, (size_t)hn) ||
                    !ufTaskWrite(bounce, n) ||
                    !ufTaskWrite((const uint8_t*)"\r\n", 2)) { werr = true; break; }
            }
            uf.ring_tail    += n;
            uf.bytes_written = uf.ring_tail;        // modal progress = RAW bytes staged
        }
        if (werr) break;
        if (zb_stream_on && !zbFlushBlock()) break;   // tail partial block
        if (!ufTaskWrite((const uint8_t*)"0\r\n\r\n", 5)) break;
        uf.tcp->flush();
        // Response: complete headers (or disconnect), bounded. Parse as soon
        // as headers are in — some stacks ignore Connection: close.
        uint32_t last = millis();
        while (millis() - last < 20000) {
            if (uf.net_abort) break;
            while (uf.tcp->available()) {
                const int c = uf.tcp->read();
                if (c < 0) break;
                if (uf.response_len + 1 < sizeof(uf.response)) {
                    uf.response[uf.response_len++] = (char)c;
                    uf.response[uf.response_len]   = '\0';
                }
                last = millis();
            }
            if (strstr(uf.response, "\r\n\r\n")) break;
            if (!uf.tcp->connected() && !uf.tcp->available()) break;
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        ok = true;
    } while (false);
    // Hand the 73 KB of internal RAM back IMMEDIATELY (v0.1.134) rather than
    // holding it until ufFreeBuf() — the next file's TLS handshake needs it.
    zbFree();
    uf.net_state = ok ? 2 : 3;
    vTaskDelete(NULL);
}

// ---------------------------------------------------------------------------
// PAGE_SESSIONS state. Reuses the same Q,LIST / Q,DEL wire protocol that the
// upload flow uses but keeps its own data so the user can still tap UPLOAD
// on PAGE_DASH while browsing the queue here. The SessionsListState enum is
// forward-declared at the top of the file (see uploads section) so the auto
// prototyper can see slEnter(SessionsListState) signatures.
// ---------------------------------------------------------------------------
static constexpr int SESSIONS_MAX_FILES = 24;   // bounded to keep RAM use predictable

struct SessionsList {
    SessionsListState state;
    uint32_t state_entered_ms;
    char     files[SESSIONS_MAX_FILES][80];
    uint32_t sizes[SESSIONS_MAX_FILES];
    bool     selected[SESSIONS_MAX_FILES];
    int      count;
    // Delete batch progress.
    int      del_idx;       // currently-being-deleted index in files[]
    int      del_done;      // total OKs in this batch
    int      del_fail;      // total fails in this batch
    char     last_err[64];
    bool     delete_all_mode;
    bool     dirty;
};
static SessionsList sl = {};
static int sessions_scroll_y = 0;

static void slReset() {
    memset(&sl, 0, sizeof(sl));
    sl.state = SL_IDLE;
    sessions_scroll_y = 0;
}

static void slEnter(SessionsListState st) {
    sl.state = st;
    sl.state_entered_ms = millis();
    sl.dirty = true;
}

// Upload ONLY the files selected on the Sessions page (skip the Q,LIST phase
// entirely — we already know the names/sizes). Lets the user push a single
// small .dbg file up without waiting behind a 4 MB session file. Defined here
// (not next to ufStartListing) because it needs the `sl` global above.
static void ufStartSelected() {
    ufReset();
    for (int i = 0; i < sl.count && uf.files_n < (int)(sizeof(uf.files) / sizeof(uf.files[0])); ++i) {
        if (!sl.selected[i]) continue;
        strncpy(uf.files[uf.files_n].name, sl.files[i], sizeof(uf.files[0].name) - 1);
        uf.files[uf.files_n].name[sizeof(uf.files[0].name) - 1] = '\0';
        uf.files[uf.files_n].size = sl.sizes[i];
        uf.files_n++;
    }
    if (uf.files_n == 0) { ufEnter(UF_DONE); return; }
    uf.files_idx = 0;
    ufStartCurrentFile();   // sends Q,GET for the first selected file
}

static void sessionsRequestList() {
    slReset();
    slEnter(SL_LISTING);
    Serial.println("Q,LIST");
    Serial.flush();
}

// Walk sl.files looking for the next selected file (or any file in delete-all
// mode). Returns the index, or -1 if there are none left to delete.
static int sl_next_to_delete(int from_idx) {
    for (int i = from_idx; i < sl.count; ++i) {
        if (sl.delete_all_mode || sl.selected[i]) return i;
    }
    return -1;
}

static void slStartDelete(bool delete_all) {
    if (sl.state == SL_DELETING) return;       // already running
    if (sl.count == 0) return;
    sl.delete_all_mode = delete_all;
    sl.del_done = sl.del_fail = 0;
    sl.last_err[0] = '\0';
    sl.del_idx = sl_next_to_delete(0);
    if (sl.del_idx < 0) return;                // nothing selected
    slEnter(SL_DELETING);
    Serial.printf("Q,DEL,%s\n", sl.files[sl.del_idx]);
    Serial.flush();
}

static bool parseQLine(const String& line) {
    // Q,FILE,<name>,<size>
    // Q,END[,<reason>]
    // Q,DATA,<name>,<size>
    // Q,L,<ndjson>
    // Q,EOF,<line_count>
    // Q,ERR,<reason>
    // Q,DEL,OK | Q,DEL,FAIL,<reason>
    const char* p = line.c_str() + 2;   // skip 'Q,'

    // PAGE_SESSIONS consumer (preferred when it's actively listing/deleting).
    // Yields to an active UPLOAD listing so a stuck/lingering SL_LISTING can't
    // hijack the upload's Q,FILE/Q,END responses (-> upload sees an empty queue).
    if (strncmp(p, "FILE,", 5) == 0 && sl.state == SL_LISTING && uf.state != UF_LISTING) {
        const char* rest = p + 5;
        const char* comma = strchr(rest, ',');
        if (!comma || sl.count >= SESSIONS_MAX_FILES) return true;
        const size_t name_len = (size_t)(comma - rest);
        if (name_len >= sizeof(sl.files[0])) return true;
        memcpy(sl.files[sl.count], rest, name_len);
        sl.files[sl.count][name_len] = '\0';
        sl.sizes[sl.count] = (uint32_t)strtoul(comma + 1, nullptr, 10);
        sl.selected[sl.count] = false;
        sl.count++;
        sl.dirty = true;
        return true;
    }
    if (strncmp(p, "END", 3) == 0 && sl.state == SL_LISTING && uf.state != UF_LISTING) {
        slEnter(SL_IDLE);
        return true;
    }
    if (strncmp(p, "DEL,", 4) == 0 && sl.state == SL_DELETING) {
        if (strncmp(p + 4, "OK", 2) == 0) sl.del_done++;
        else {
            sl.del_fail++;
            snprintf(sl.last_err, sizeof(sl.last_err), "%s", p + 4);
        }
        // advance to next file to delete
        const int next = sl_next_to_delete(sl.del_idx + 1);
        if (next < 0) {
            // Done; refresh list to drop the deleted entries.
            sessionsRequestList();
        } else {
            sl.del_idx = next;
            Serial.printf("Q,DEL,%s\n", sl.files[sl.del_idx]);
            Serial.flush();
            sl.state_entered_ms = millis();
        }
        return true;
    }

    // Upload-flow consumer.
    if (strncmp(p, "FILE,", 5) == 0 && uf.state == UF_LISTING) {
        const char* rest = p + 5;
        const char* comma = strchr(rest, ',');
        if (!comma || uf.files_n >= (int)(sizeof(uf.files)/sizeof(uf.files[0]))) return true;
        const size_t name_len = (size_t)(comma - rest);
        if (name_len >= sizeof(uf.files[0].name)) return true;
        memcpy(uf.files[uf.files_n].name, rest, name_len);
        uf.files[uf.files_n].name[name_len] = '\0';
        uf.files[uf.files_n].size = (uint32_t)strtoul(comma + 1, nullptr, 10);
        uf.files_n++;
        return true;
    }
    if (strncmp(p, "END", 3) == 0 && uf.state == UF_LISTING) {
        if (uf.files_n == 0) {
            ufEnter(UF_DONE);
        } else {
            ufStartCurrentFile();
        }
        return true;
    }
    if (strncmp(p, "DATA,", 5) == 0 && uf.state == UF_FETCH_HEAD) {
        const char* rest = p + 5;
        const char* comma = strchr(rest, ',');
        if (!comma) return true;
        const uint32_t sz = (uint32_t)strtoul(comma + 1, nullptr, 10);
        // TRUE STREAMING (v0.1.114): allocate the PSRAM ring and spawn the
        // net task — it owns connect/TLS/headers/body/response on core 0 while
        // the loop only ever touches the UART + ring. The file flows car ->
        // cloud in one continuous pass at min(UART, network) speed.
        ufStopNetTask();
        ufFreeBuf();
        if (sz == 0) {
            snprintf(uf.last_err, sizeof(uf.last_err), "zero file size");
            uf.failed++;
            ufNextFile();
            return true;
        }
        uint32_t cap = 512UL * 1024;   // ~6 s of UART at full rate; halve on alloc fail
        while (!(uf.buf = (uint8_t*)ps_malloc(cap)) && cap > 64UL * 1024) cap /= 2;
        if (!uf.buf) {
            snprintf(uf.last_err, sizeof(uf.last_err), "PSRAM ring alloc failed");
            uf.failed++;
            ufNextFile();
            return true;
        }
        uf.bufcap        = cap;
        uf.buflen        = 0;
        uf.lines_recv    = 0;
        uf.next_seq      = 1;
        uf.expected_size = sz;      // modal progress (total file bytes)
        uf.bytes_written = 0;
        uf.ring_head     = 0;
        uf.ring_tail     = 0;
        uf.net_eof       = false;
        uf.net_abort     = false;
        uf.net_err[0]    = '\0';
        uf.last_err[0]   = '\0';   // stale errors from a previous file confused the modal
        uf.response_len  = 0;
        uf.response[0]   = '\0';
        uf.net_state     = 1;
        Serial.printf("DBG,uf_stream_start size=%lu ring=%lu heap=%u rssi=%d\n",
                      (unsigned long)sz, (unsigned long)cap,
                      (unsigned)ESP.getFreeHeap(), (int)WiFi.RSSI());
        if (xTaskCreatePinnedToCore(ufNetTask, "ufnet", 16384, nullptr, 1,
                                    nullptr, 0) != pdPASS) {
            uf.net_state = 0;
            snprintf(uf.last_err, sizeof(uf.last_err), "net task spawn failed");
            uf.failed++;
            ufNextFile();
            return true;
        }
        ufEnter(UF_STREAMING);
        return true;
    }
    if (strncmp(p, "L,", 2) == 0 && uf.state == UF_STREAMING) {
        // Sequence-numbered line: L,<seq>,<data>. Stop-and-wait ARQ so a
        // dropped/corrupted byte on the UART wire costs one retransmit, not the
        // whole upload. We only APPLY seq == next_seq; a duplicate (seq <
        // next_seq, from a lost ACK) is ignored but still re-ACKed; a corrupt
        // line that fails to parse is ignored (Teensy retransmits).
        if (!uf.buf) {
            snprintf(uf.last_err, sizeof(uf.last_err), "no buffer for Q,L");
            uf.failed++;
            ufNextFile();
            return true;
        }
        const char* s = p + 2;
        char* endp = nullptr;
        const unsigned long seq = strtoul(s, &endp, 10);
        if (!endp || *endp != ',') return true;   // malformed; Teensy will resend
        const char* data = endp + 1;
        const size_t n = strlen(data);

        if (seq == uf.next_seq) {
            const size_t need = n + 1;   // line + '\n'
            if (need > uf.bufcap) {
                snprintf(uf.last_err, sizeof(uf.last_err), "line too long %u", (unsigned)need);
                uf.failed++; ufNextFile();
                return true;
            }
            // Ring full = the network is momentarily slower than the UART.
            // Do NOT apply/ack — the Teensy's ARQ retries in 2 s while the net
            // task keeps draining; the stream self-paces to the slower link.
            // (If the net is DEAD the task flags net_state=3 and uploadTick
            // aborts+retries — we never sit here forever.)
            const uint32_t free_b = uf.bufcap - (uf.ring_head - uf.ring_tail);
            if (free_b < need) return true;
            {   // copy line + '\n' into the ring (with wrap)
                const uint32_t off = uf.ring_head % uf.bufcap;
                size_t c1 = uf.bufcap - off;
                if (c1 > n) c1 = n;
                memcpy(uf.buf + off, data, c1);
                if (n > c1) memcpy(uf.buf, data + c1, n - c1);
                uf.buf[(off + n) % uf.bufcap] = '\n';
            }
            uf.ring_head += need;
            uf.next_seq++;
            uf.lines_recv++;
            if ((uf.lines_recv % 1000) == 0) {
                Serial.printf("DBG,uf_stream lines=%lu bytes=%lu/%lu\n",
                              (unsigned long)uf.lines_recv,
                              (unsigned long)uf.bytes_written,
                              (unsigned long)uf.expected_size);
            }
        }
        uf.last_rx_ms = millis();
        // ACK the highest contiguous seq applied (next_seq-1); dedups resends.
        Serial.printf("Q,A,%lu\n", (unsigned long)(uf.next_seq - 1));
        Serial.flush();
        return true;
    }
    if (strncmp(p, "EOF", 3) == 0 && uf.state == UF_STREAMING) {
        // File fully queued — the net task drains the ring tail, writes the
        // chunked terminator, and reads the response. We just watch it.
        uf.net_eof  = true;
        uf.fin_tail = uf.ring_tail;
        uf.fin_ms   = millis();
        Serial.printf("DBG,uf_eof lines=%lu queued=%lu sent=%lu\n",
                      (unsigned long)uf.lines_recv,
                      (unsigned long)uf.ring_head, (unsigned long)uf.ring_tail);
        ufEnter(UF_STREAM_FINISH);
        return true;
    }
    if (strncmp(p, "ERR,", 4) == 0) {
        // Stale errors (e.g. the Teensy's ack_timeout arriving after WE already
        // aborted and scheduled a retry) must not kill the retry — only act on
        // an error for a stream we're actively in.
        if (uf.state != UF_STREAMING && uf.state != UF_FETCH_HEAD) return true;
        snprintf(uf.last_err, sizeof(uf.last_err), "%s", p + 4);
        Serial.printf("DBG,uf_err from_teensy=%s at buflen=%lu lines=%lu\n",
                      p + 4, (unsigned long)uf.buflen, (unsigned long)uf.lines_recv);
        ufFailOrRetry(false);   // Teensy already exited its loop — no abort needed
        return true;
    }
    if (strncmp(p, "DEL,", 4) == 0 && uf.state == UF_DELETING) {
        if (strncmp(p + 4, "OK", 2) == 0) {
            uf.uploaded++;
        } else {
            snprintf(uf.last_err, sizeof(uf.last_err), "DEL: %s", p + 4);
        }
        ufNextFile();
        return true;
    }
    return false;
}

// Parse the accumulated HTTP response in uf.response and return the status
// code. body_snip is filled with a one-line snippet of the response body for
// display in the modal banner. Returns 0 if no parseable status line.
static int ufParseResponse(char* body_snip, size_t body_snip_sz) {
    body_snip[0] = '\0';
    if (uf.response_len == 0) return 0;
    int code = 0;
    const char* sp = strchr(uf.response, ' ');
    if (sp) code = atoi(sp + 1);
    const char* bs = strstr(uf.response, "\r\n\r\n");
    const char* body = bs ? bs + 4 : "";
    // Strip CR/LF for display; collapse runs of whitespace into single spaces.
    size_t k = 0;
    bool   prev_space = false;
    for (size_t i = 0; body[i] && k < body_snip_sz - 1; ++i) {
        const char c = body[i];
        if (c == '\n' || c == '\r' || c == '\t' || c == ' ') {
            if (!prev_space && k > 0) {
                body_snip[k++] = ' ';
                prev_space = true;
            }
        } else {
            body_snip[k++] = c;
            prev_space = false;
        }
    }
    body_snip[k] = '\0';
    return code;
}

static void uploadTick() {
    // Scheduled single-retry of the current file (see ufFailOrRetry).
    if (uf.state == UF_RETRY_WAIT) {
        if ((int32_t)(millis() - uf.retry_at_ms) >= 0) ufStartCurrentFile();
        return;
    }
    if (uf.state == UF_IDLE) return;
    const uint32_t now = millis();

    // Drive modal display whenever state has moved.
    if (upload_active) {
        char fname[80] = "";
        uint32_t total = 1, done = 0;
        if (uf.files_idx < uf.files_n) {
            snprintf(fname, sizeof(fname), "%s", uf.files[uf.files_idx].name);
            total = uf.expected_size > 0 ? uf.expected_size
                                          : uf.files[uf.files_idx].size > 0
                                              ? uf.files[uf.files_idx].size
                                              : 1;
            // Progress = bytes PULLED OFF THE CAR (ring_head), not bytes
            // already ack'd by the socket (ring_tail). v0.1.134: ring_tail
            // sits at 0 for the whole TLS handshake (and stays 0 forever if
            // the connect fails), which is exactly the "bar stuck at 0 then
            // jumps" the driver sees. ring_head advances from the very first
            // Q,L line and can never be more than one ring (512 KB) ahead of
            // what's been sent, so it's both smooth and honest.
            done = (uf.state == UF_STREAMING || uf.state == UF_STREAM_FINISH)
                   ? uf.ring_head : uf.bytes_written;
        }
        // Redraw throttle (v0.1.136). `done` advances on EVERY Q,L line
        // (~250/s measured), and each modal redraw repaints several
        // full-width padded strings — at that rate the drawing steals loop
        // time from the UART pump, which IS the upload bottleneck. Cap to
        // ~5 Hz, but never delay a name/total change or a reset.
        static uint32_t last_ui_ms = 0;
        const bool ui_force = (strcmp(upload_file, fname) != 0)
                              || (total != upload_total) || (done < upload_done);
        if (ui_force || (done != upload_done && now - last_ui_ms >= 200)) {
            last_ui_ms   = now;
            strncpy(upload_file, fname, sizeof(upload_file) - 1);
            upload_file[sizeof(upload_file) - 1] = '\0';
            upload_total = total;
            upload_done  = done;
            upload_modal_dirty = true;
        }
    }

    // Net-task failure surfaces here regardless of sub-state (the task never
    // touches Serial or the state machine — it just flags net_state=3).
    if ((uf.state == UF_STREAMING || uf.state == UF_STREAM_FINISH) &&
        uf.net_state == 3) {
        snprintf(uf.last_err, sizeof(uf.last_err), "%s",
                 uf.net_err[0] ? uf.net_err : "network task failed");
        Serial.printf("DBG,uf_net_fail %s\n", uf.last_err);
        ufFailOrRetry(true);   // stops the (already dead) task, aborts Teensy, retries once
        return;
    }

    // Per-state housekeeping.
    if (uf.state == UF_STREAM_FINISH) {
        if (uf.net_state == 2) {
            // Task finished cleanly: body sent + response captured.
            char body[140];
            const int code = ufParseResponse(body, sizeof(body));
            Serial.printf("DBG,uf_response code=%d body=%s\n", code, body);
            ufDiagReport((code >= 200 && code < 300) ? "ok" : "httpfail");
            ufStopNetTask();
            ufCloseTcp();
            if (code >= 200 && code < 300) {
                // v0.1.136: DO NOT delete now. Deleting a multi-MB file makes
                // the SD card run internal garbage collection, which then
                // silences the NEXT file's reads for 30-90 s (bench-proven:
                // first attempt dies ~26 lines, retry clean). Deletes are
                // therefore deferred to the END of the batch, off the critical
                // path. Losing a delete is harmless — the server keys on
                // session id with mode='w', so a re-upload just overwrites.
                uf.del_mask |= (uint16_t)(1u << uf.files_idx);
                uf.uploaded++;
                ufNextFile();
            } else {
                if (code > 0 && body[0]) {
                    snprintf(uf.last_err, sizeof(uf.last_err),
                             "http %d: %s", code, body);
                } else if (code > 0) {
                    snprintf(uf.last_err, sizeof(uf.last_err), "http %d", code);
                } else {
                    snprintf(uf.last_err, sizeof(uf.last_err), "no http response");
                }
                uf.failed++;
                // Debug logs are BEST-EFFORT: if the server rejects one, drop
                // it (fire-and-forget delete) so a rejected .dbg file can NEVER
                // clog the queue and block session uploads. Sessions are kept
                // for retry. The Q,DEL,OK reply is ignored (state != DELETING).
                if (strstr(uf.files[uf.files_idx].name, ".dbg.")) {
                    Serial.printf("Q,DEL,%s\n", uf.files[uf.files_idx].name);
                    Serial.flush();
                }
                ufNextFile();
            }
            return;
        }
        // Task still draining the ring tail / waiting for the response:
        // watchdog on ITS progress (ring_tail), not UART activity.
        if (uf.ring_tail != uf.fin_tail) {
            uf.fin_tail = uf.ring_tail;
            uf.fin_ms   = now;
        } else if (now - uf.fin_ms > 100000) {   // > task's 90 s write-stall cap
            snprintf(uf.last_err, sizeof(uf.last_err),
                     "finish stalled at %lu/%lu B",
                     (unsigned long)uf.ring_tail, (unsigned long)uf.ring_head);
            ufFailOrRetry(true);
        }
        return;
    }
    // Upload batch finished — the server is AI-reviewing what just landed on a
    // BACKGROUND thread, so don't fetch instantly: schedule it ~25 s out, by
    // which time the review has normally filed its items (v0.1.137).
    if (uf.state == UF_DONE && coach_refetch_at_ms == 0)
        coach_refetch_at_ms = millis() + 25000;
    if (uf.state == UF_DONE) {
        // Build summary banner once.
        if (upload_result_msg[0] == '\0') {
            if (uf.failed == 0 && uf.uploaded > 0) {
                snprintf(upload_result_msg, sizeof(upload_result_msg),
                         "OK: %d uploaded", uf.uploaded);
            } else if (uf.uploaded == 0 && uf.failed > 0) {
                snprintf(upload_result_msg, sizeof(upload_result_msg),
                         "FAIL: %s", uf.last_err[0] ? uf.last_err : "all uploads failed");
            } else if (uf.failed > 0) {
                snprintf(upload_result_msg, sizeof(upload_result_msg),
                         "OK: %d up, %d failed", uf.uploaded, uf.failed);
            } else {
                snprintf(upload_result_msg, sizeof(upload_result_msg), "OK: empty queue");
            }
            upload_modal_dirty  = true;
            upload_last_draw_ms = now;
        }
        return;
    }

    // Ring-full means WE stopped acking (network slower than the wire) — UART
    // silence is then EXPECTED, not a Teensy failure. Keep the watchdog fed
    // while the net task is alive so a long outage rides on the ring + the
    // Teensy's ARQ patience instead of aborting the file (v0.1.115).
    if (uf.state == UF_STREAMING && uf.net_state == 1 && uf.buf) {
        const uint32_t free_b = uf.bufcap - (uf.ring_head - uf.ring_tail);
        if (free_b < 512) uf.last_rx_ms = now;
    }

    // Timeout watchdogs. For wait-for-first-response states the clock starts
    // when we entered the state. For UF_STREAMING and UF_STREAM_FINISH the
    // clock is activity-based: we only fail if there's a sustained stall
    // (last_rx_ms aged out), so a slow but progressing transfer succeeds.
    uint32_t timeout_ms = 0;
    uint32_t since      = 0;
    switch (uf.state) {
        case UF_LISTING:        timeout_ms = 6000;   since = now - uf.state_entered_ms; break;
        // 30 s (was 6 s, v0.1.121): the Q,DATA reply needs the Teensy to OPEN
        // the file — which can block behind the same SD-card garbage
        // collection that stalls mid-stream reads. Failure routes through the
        // whole-file retry (Q,ABORT + fresh Q,GET), which also un-wedges a
        // Teensy stuck in a zombie stream.
        case UF_FETCH_HEAD:     timeout_ms = 30000;  since = now - uf.state_entered_ms; break;
        // 90 s (was 30 s, v0.1.120): an SD card's internal garbage collection
        // (triggered by the previous file's delete) can silence the Teensy for
        // 30+ s MID-STREAM — ufdiag proved the stream was healthy on both
        // sides and the retry ran clean. Wait out the pause instead of
        // shooting a stream that's merely stalled; genuinely dead links still
        // die (Teensy patience 120 s > our 90 s).
        // v0.1.136: the 90 s blanket was paying for the WRONG stall. Two very
        // different things look like "no UART data":
        //  - ring DRAINED + net task healthy => the TEENSY is silent, i.e. SD
        //    garbage collection (triggered by the previous file's delete).
        //    Waiting does NOT help — the RETRY is what clears it. Measured on
        //    the bench: first attempt died at 26 lines / 5,679 B, then the
        //    retry streamed 15,000 lines at 250/s without a pause. So burning
        //    90 s here is pure dead time; 20 s is plenty to be sure it's stuck.
        //  - ring FULL => the NETWORK is the bottleneck (paddock WiFi can drop
        //    to zero for 30 s+). Keep the full 90 s patience there (v0.1.115).
        case UF_STREAMING: {
            const bool ring_drained = (uf.ring_head == uf.ring_tail);
            const bool net_ok       = (uf.net_state == 1);
            timeout_ms = (ring_drained && net_ok) ? 20000 : 90000;
            since      = now - uf.last_rx_ms;
            break;
        }
        // (UF_POSTING gone; UF_STREAM_FINISH watches net-task progress in its
        // own block above, not UART activity — no entry here.)
        case UF_DELETING:       timeout_ms = 6000;   since = now - uf.state_entered_ms; break;
        default: return;
    }
    if (since > timeout_ms) {
        if (uf.state == UF_STREAMING) {
            snprintf(uf.last_err, sizeof(uf.last_err),
                     "stalled at %lu/%lu B (no data for %lus)",
                     (unsigned long)uf.bytes_written,
                     (unsigned long)uf.expected_size,
                     (unsigned long)(since / 1000));
        } else if (uf.state == UF_STREAM_FINISH) {
            snprintf(uf.last_err, sizeof(uf.last_err),
                     "no server response in %lus", (unsigned long)(since / 1000));
        } else {
            snprintf(uf.last_err, sizeof(uf.last_err),
                     "timeout in state %u after %lus",
                     (unsigned)uf.state, (unsigned long)(since / 1000));
        }
        Serial.printf("DBG,uf_timeout state=%u %s\n",
                      (unsigned)uf.state, uf.last_err);
        if (uf.state == UF_LISTING) {
            // Re-ask before giving up (v0.1.121): our Q,LIST may have been
            // eaten by a Teensy wedged in a zombie stream — the first re-ask
            // un-wedges it (implicit abort), the next one gets answered.
            if (uf.list_tries < 2) {
                uf.list_tries++;
                Serial.println("Q,LIST");
                Serial.flush();
                ufEnter(UF_LISTING);
            } else {
                ufCloseTcp();
                ufEnter(UF_DONE);
            }
        }
        else if (uf.state == UF_FETCH_HEAD) {
            // No Q,DATA: eaten request or SD-stalled open — whole-file retry
            // (sends Q,ABORT first, then a fresh Q,GET after the settle).
            ufFailOrRetry(true);
        }
        else if (uf.state == UF_STREAMING) {
            // UART went quiet for 30 s. Either the Teensy died, or the ring
            // has been full that long (network-dead — the net task usually
            // flags itself first). Abort the sender + retry the file once.
            ufFailOrRetry(true);
        }
        else { ufCloseTcp(); uf.failed++; ufNextFile(); }
    }
}

static void closeUploadModal() {
    upload_active      = false;
    upload_result_msg[0] = '\0';
    currentPage        = (Page)upload_return_page;
    pageJustEntered    = true;
    settingsDirty      = true;
    invalidateAll();
    ufReset();   // ensure UPLOAD button next tap starts a fresh flow
    // If a session is RECORDING (auto-start can begin one mid-upload), BT
    // needs the radio back for coolant; otherwise WiFi keeps it (paddock).
    if (recording && s.sensor_type == 2 && s.bt_addr[0] && !obd::blocked()) {
        btAcquireRadio();
        obd::begin();
        obd::connectTo(s.bt_addr, s.bt_atype, s.bt_name);
    }
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

// Local mirror of the Teensy test_mode_active flag. Updated by TEST,<0|1>
// lines so the Tools page button shows the correct label even across reboots.
static bool test_mode_active = false;
static bool parseTestLine(const String& line) {
    // TEST,<0|1>
    const int c1 = line.indexOf(',');
    if (c1 < 0) return false;
    test_mode_active = (line.substring(c1 + 1).toInt() != 0);
    return true;
}

// parseWupLine() is defined far below — it touches wifi_state which lives in
// the WiFi section. Just a forward decl up here so parseLine() can dispatch
// to it from this top-of-file UART parsing block.
static bool parseWupLine(const String& line);

static bool parseLine(const String& line) {
    // Track ANY Q,* traffic from the Teensy (v0.1.135). The dash's own
    // uf/sl state is NOT sufficient to know the link is free: when an upload
    // fails the dash returns to UF_IDLE while the Teensy is still grinding
    // through its 120 s ARQ retransmit loop, happily sending Q,L lines. The
    // periodic CFG resend used that stale "idle" to inject a 17-line burst
    // straight into the middle of the ARQ exchange.
    if (line.startsWith("Q,")) q_activity_ms = millis();

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
    if (line.startsWith("CANSNIFF,")) return parseCanSniffLine(line);
    if (line.startsWith("CANDIAG,"))  return parseCanDiagLine(line);
    if (line.startsWith("CLD,"))  return parseCldLine(line);
    if (line.startsWith("TIME,")) return parseTimeLine(line);
    if (line.startsWith("UPLOAD,")) return parseUploadLine(line);
    if (line.startsWith("VER,"))    return parseVerLine(line);
    if (line.startsWith("TEST,"))   return parseTestLine(line);
    if (line.startsWith("WUP,"))    return parseWupLine(line);
    if (line.startsWith("Q,"))     return parseQLine(line);
    if (line.startsWith("GPSBAUD,")) {
        // GPSBAUD,<baud>,<ok>  (ok=1 => module locked + PVT flowing at that baud)
        int c1 = line.indexOf(',');
        int c2 = line.indexOf(',', c1 + 1);
        if (c1 > 0 && c2 > c1) {
            long baud = line.substring(c1 + 1, c2).toInt();
            int  ok   = line.substring(c2 + 1).toInt();
            snprintf(gps_status_buf, sizeof(gps_status_buf), "%ld %s",
                     baud, ok ? "OK" : "NO DATA");
            gps_diag_baud = (uint32_t)baud; gps_diag_libok = ok ? 1 : 0;
            settingsDirty = true;   // refresh the INFO row
            gps_page_dirty = true;
        }
        return true;
    }
    if (line.startsWith("GPSDIAG,")) {
        // GPSDIAG,<baud>,<lib_ok>,<pvt_age_ms>,<light_recover>,<heavy_rebegin>,<hz>
        int p = 8, c;
        long v[6] = {0,0,0,0,0,0}; int idx = 0;
        while (idx < 6) {
            c = line.indexOf(',', p);
            String tok = (c < 0) ? line.substring(p) : line.substring(p, c);
            v[idx++] = tok.toInt();
            if (c < 0) break; p = c + 1;
        }
        gps_diag_baud    = (uint32_t)v[0];
        gps_diag_libok   = (uint8_t)v[1];
        gps_diag_age_ms  = (uint32_t)v[2];
        gps_diag_recover = (uint32_t)v[3];
        gps_diag_reinit  = (uint32_t)v[4];
        gps_diag_hz      = (uint8_t)v[5];
        if (currentPage == PAGE_GPS) gps_page_dirty = true;
        return true;
    }
    if (line.startsWith("HLTH,")) {
        // HLTH,<t_die_x10>,<t_mpu_x10>,<t_esp_x10>,<batt_x10> — device temps + batt
        // (-9999 = n/a temp; -1 = n/a battery). Used for heat/brownout diagnosis.
        int p = 5, c; long v[4] = {0,0,0,0}; int idx = 0;
        while (idx < 4) {
            c = line.indexOf(',', p);
            String tok = (c < 0) ? line.substring(p) : line.substring(p, c);
            v[idx++] = tok.toInt();
            if (c < 0) break; p = c + 1;
        }
        health_teensy_c = (v[0] > -9000) ? v[0] / 10.0f : NAN;
        health_mpu_c    = (v[1] > -9000) ? v[1] / 10.0f : NAN;
        health_batt_v   = (v[3] >= 0)    ? v[3] / 10.0f : NAN;
        health_last_hlth_ms = millis();
        return true;
    }
    if (line.startsWith("RST,teensy,")) {
        // Teensy's last reset cause (POR/brownout/watchdog/lockup/overtemp/swrst)
        // — shown on the STATUS HEALTH bar for the comms-death diagnosis.
        strncpy(teensy_reset_reason, line.substring(11).c_str(), sizeof(teensy_reset_reason) - 1);
        teensy_reset_reason[sizeof(teensy_reset_reason) - 1] = 0;
        // The Teensy just booted, so its config is default — push ours ONCE,
        // now. This is what the old 5 s CFG carpet-bombing was really for
        // (v0.1.135); event-driven, so the link stays quiet the rest of the time.
        cfg_resend_req = true;
        return true;
    }
    return false;
}
static uint32_t uart_last_ok_ms = 0;   // last successfully PARSED line from the Teensy
static uint32_t uart_reinit_ms  = 0;
static uint16_t uart_reinits    = 0;

// Does this line prove the Teensy is running its normal loop()? Only real
// telemetry does (v0.1.136). Stray Q,* lines do NOT: when the dash reboots
// mid-upload the Teensy is still inside its BLOCKING ARQ retransmit loop,
// spraying Q,L lines and running no loop() at all — so no GPS/ENG/IMU ever
// arrives. Those Q,L lines still "parse" (they're recognised and discarded),
// which used to keep uart_last_ok_ms permanently fresh and the recovery
// watchdog permanently asleep. That is the "reboot the screen and it never
// re-talks to the Teensy" failure.
static bool uartLineIsTelemetry(const String& s) {
    return s.startsWith("GPS,")  || s.startsWith("ENG,")  || s.startsWith("ECU,")
        || s.startsWith("IMU,")  || s.startsWith("TIME,") || s.startsWith("HLTH,")
        || s.startsWith("SD,")   || s.startsWith("CLD,")  || s.startsWith("ETH,")
        || s.startsWith("VER,")  || s.startsWith("RST,");
}

static void pumpUart() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            const bool handled = parseLine(rxBuf);
            if (handled && uartLineIsTelemetry(rxBuf)) uart_last_ok_ms = millis();
            rxBuf = "";
        }
        else if (rxBuf.length() < UART_LINE_MAX) { rxBuf += c; }
        else { rxBuf = ""; }
    }
}

// UART link watchdog (v0.1.122). Field case: the dash showed no speed for a
// whole session while the Teensy recorded perfectly — the wire glitched
// (loose connector) and the dash's UART/parser never came back. If NO valid
// line has parsed for 15 s, hard-reinit UART0 (flush ring, reset the line
// accumulator, end+begin) and poke the Teensy; repeat every 15 s until lines
// flow again. Skipped while an upload is active (streams have their own
// recovery and a reinit would drop in-flight ARQ bytes).
static void uartLinkTick() {
    const uint32_t now = millis();
    // Recovery cadence (v0.1.136). The old 20 s boot grace + 15 s retry is why
    // "unplug and replug the display and it never reconnects to the Teensy...
    // then after a minute or two it comes back" — it was never permanent, just
    // 3-6 slow cycles. If we have NEVER seen telemetry (fresh boot, confused
    // UART, or a Teensy wedged in a zombie stream), probe at 4 s and then every
    // 5 s; once the link has been healthy, fall back to the lazy 15 s cadence.
    const bool     never_ok = (uart_last_ok_ms == 0);
    const uint32_t grace    = never_ok ? 4000 : 15000;
    const uint32_t period   = never_ok ? 5000 : 15000;
    if (now < grace) return;
    if (!never_ok && now - uart_last_ok_ms < 15000) return;
    if (now - uart_reinit_ms  < period) return;
    if (uf.state != UF_IDLE || sl.state != SL_IDLE) return;   // mid-transfer: leave it alone
    uart_reinit_ms = now;
    uart_reinits++;
    rxBuf = "";
    while (Serial.available()) (void)Serial.read();
    Serial.flush();
    Serial.end();
    delay(5);
    Serial.setRxBufferSize(32768);           // must precede begin() (ESP32 core)
    Serial.begin(921600);
    // Q,ABORT FIRST (v0.1.136): the usual reason we hear nothing is that the
    // Teensy is wedged in a zombie Q,GET retransmit loop (dash rebooted
    // mid-upload) where it runs no loop() and emits no telemetry. Its ack pump
    // DOES match Q,ABORT, so this frees it in ~15 s instead of waiting out its
    // 120 s ARQ patience — or forever, if it keeps getting re-triggered.
    Serial.printf("Q,ABORT\n");
    Serial.printf("VER?\n");                 // poke — any reply revives uart_last_ok_ms
    Serial.printf("DBG,uart_reinit n=%u\n", (unsigned)uart_reinits);
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
static void snapSettingsScroll();

enum Gesture : uint8_t {
    GESTURE_NONE = 0,             // not yet classified
    GESTURE_DRAG_V,               // active vertical drag (settings scroll)
    GESTURE_SWIPE_H,              // tentative horizontal swipe — confirmed on release
    GESTURE_SLIDER,               // dragging the brightness slider (settings page)
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
static SettingId    active_slider = ST_COUNT;   // which SLIDER row is being dragged (ST_COUNT = none)

constexpr int  SWIPE_DX_MIN     = DASH_SWIPE_DX_MIN;   // per-board (board_config.h)
constexpr int  SWIPE_DY_MAX     = 100;
constexpr uint32_t SWIPE_MS_MAX = DASH_SWIPE_MS_MAX;   // per-board (board_config.h)
constexpr int  TAP_DXY_MAX      = 30;
constexpr uint32_t TAP_MS_MAX   = 600;
constexpr int  GESTURE_THRESH   = 30;        // movement before we classify

// Forward decls.
static void handleSettingsTap(int x, int y);
static void handleDashTap(int x, int y);
static SettingId settingsSliderHit(int x, int y);    // which SLIDER row is at (x,y), or ST_COUNT
static void      sliderSetFromX(SettingId id, int x);// set a SLIDER row's value from screen-x
static void      applyBrightness(uint8_t pct);
static void handleTimeSetTap(int x, int y);
static void openTrackPicker(bool for_recording = false);
static void openConfigPicker(int track_idx, bool from_auto, bool for_recording = false);
static void handleConfigPickerTap(int x, int y);
static void handleWifiScannerTap(int x, int y);
static void drawWifiScannerPage();
static void drawGpsPage();
static void handleGpsPageTap(int x, int y);
static void openGpsPage();
static void drawSensorPage();
static void handleSensorPageTap(int x, int y);
static void openSensorPage();
static void drawBtScanPage();
static void handleBtScanTap(int x, int y);
static void openBtScan();
static void drawPidScanPage();
static void handlePidScanTap(int x, int y);
static void openPidScan();
static void drawUploadModal();
static void handleUploadModalTap(int x, int y);
static bool parseUploadLine(const String& line);
static bool parseQLine(const String& line);
static void ufStartListing();
static void uploadTick();
static void handleSessionsTap(int x, int y);
static void drawSessionsPage();
static void sessionsRequestList();
static void drawOtaModal();
static void handleOtaModalTap(int x, int y);
static void otaTick();
static void otaStart();
static void drawToolsPage();
static void handleToolsTap(int x, int y);
static bool parseVerLine(const String& line);
static void handleStatusTap(int x, int y);

static void handleTouch() {
    // Throttle GT911 reads. The main loop spins at ~950 Hz, and TAMC's read()
    // clears the controller's data-ready register on every call. Polling that
    // fast saturates the GT911's I2C servicing and (on the 5" Basic) collapses
    // its effective report rate to ~7-12 Hz -> swipes never accumulate enough
    // samples and are mis-read as taps. Reading at ~75 Hz lets the GT911 sample
    // at its native rate (the vendor LVGL build polls at only ~33 Hz).
    static uint32_t lastReadMs = 0;
    constexpr uint32_t TOUCH_POLL_MS = 13;          // ~77 Hz
    const uint32_t pollNow = millis();
    if (pollNow - lastReadMs >= TOUCH_POLL_MS) {
        lastReadMs = pollNow;
        ts.read();
        // (v0.1.135: removed a leftover "TCHrate touched/s=.. reads/s=.." debug
        // printf that fired EVERY SECOND onto UART0 — UART0 is the Teensy link,
        // so this was permanent noise in the middle of the Q,* ARQ exchange.)
    }
    const bool raw = ts.isTouched;

    // GT911 release debounce. TAMC clears the GT911 data-ready flag after each
    // read, so isTouched flickers false between the controller's sample periods
    // (~5-10 ms) even while a finger is held down. Without this, every flicker
    // looks like a release and the swipe/drag state machine never accumulates
    // movement (taps work, swipes don't). We hold the touch "down" for up to
    // TOUCH_RELEASE_MS after the last real sample, retaining the last position.
    static int32_t  heldX = 0, heldY = 0;
    static uint32_t lastTouchMs = 0;
    constexpr uint32_t TOUCH_RELEASE_MS = DASH_TOUCH_RELEASE_MS;   // per-board (board_config.h)
    if (raw) {
        heldX = (int32_t)ts.points[0].x;
        heldY = (int32_t)ts.points[0].y;
        lastTouchMs = millis();
    }
    const bool    now = raw || (tt.active && (millis() - lastTouchMs) < TOUCH_RELEASE_MS);
    const int32_t x   = heldX;
    const int32_t y   = heldY;

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
    if (currentPage == PAGE_BT_SCAN) {
        // Tap to pair + vertical drag to scroll (active scan returns up to 16
        // devices — more than fit the viewport).
        if (now && !tt.active) {
            tt.startX = x; tt.startY = y;
            tt.lastX  = x; tt.lastY  = y;
            tt.startMs = millis();
            tt.active  = true;
            tt.gesture = GESTURE_NONE;
            tt.scrollAtStart = bt_scan_scroll;
        } else if (now && tt.active) {
            const int dx = x - tt.startX;
            const int dy = y - tt.startY;
            if (tt.gesture == GESTURE_NONE
                && (abs(dx) > GESTURE_THRESH || abs(dy) > GESTURE_THRESH)) {
                tt.gesture = (abs(dy) > abs(dx)) ? GESTURE_DRAG_V : GESTURE_SWIPE_H;
            }
            if (tt.gesture == GESTURE_DRAG_V) {
                bt_scan_scroll = tt.scrollAtStart - dy;
                clampBtScanScroll();
                bt_scan_dirty = true;
            }
            tt.lastX = x; tt.lastY = y;
        } else if (!now && tt.active) {
            if (tt.gesture == GESTURE_NONE) handleBtScanTap(tt.startX, tt.startY);
            tt.active = false;
        }
        return;
    }
    if (currentPage == PAGE_PID_SCAN) {   // tap to map + vertical drag to scroll
        if (now && !tt.active) {
            tt.startX = x; tt.startY = y;
            tt.lastX  = x; tt.lastY  = y;
            tt.startMs = millis();
            tt.active  = true;
            tt.gesture = GESTURE_NONE;
            tt.scrollAtStart = pid_scan_scroll;
        } else if (now && tt.active) {
            const int dx = x - tt.startX;
            const int dy = y - tt.startY;
            if (tt.gesture == GESTURE_NONE
                && (abs(dx) > GESTURE_THRESH || abs(dy) > GESTURE_THRESH)) {
                tt.gesture = (abs(dy) > abs(dx)) ? GESTURE_DRAG_V : GESTURE_SWIPE_H;
            }
            if (tt.gesture == GESTURE_DRAG_V) {
                pid_scan_scroll = tt.scrollAtStart - dy;
                clampPidScanScroll();
                pid_scan_dirty = true;
            }
            tt.lastX = x; tt.lastY = y;
        } else if (!now && tt.active) {
            if (tt.gesture == GESTURE_NONE) handlePidScanTap(tt.startX, tt.startY);
            tt.active = false;
        }
        return;
    }
    if (currentPage == PAGE_NUM_KB || currentPage == PAGE_TEXT_KB ||
        currentPage == PAGE_CONFIG_PICKER || currentPage == PAGE_TIME_SET ||
        currentPage == PAGE_WIFI_SCAN || currentPage == PAGE_GPS ||
        currentPage == PAGE_SENSOR) {
        if (now && !tt.active) {
            tt.startX = x; tt.startY = y;
            tt.lastX  = x; tt.lastY  = y;
            tt.startMs = millis();
            tt.active  = true;
        } else if (!now && tt.active) {
            if      (currentPage == PAGE_CONFIG_PICKER) handleConfigPickerTap(tt.startX, tt.startY);
            else if (currentPage == PAGE_TIME_SET)      handleTimeSetTap(tt.startX, tt.startY);
            else if (currentPage == PAGE_WIFI_SCAN)     handleWifiScannerTap(tt.startX, tt.startY);
            else if (currentPage == PAGE_GPS)           handleGpsPageTap(tt.startX, tt.startY);
            else if (currentPage == PAGE_SENSOR)        handleSensorPageTap(tt.startX, tt.startY);
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
        Serial.printf("TS x=%ld y=%ld pg=%d\n", (long)x, (long)y, (int)currentPage);  // TEMP touch debug
        // Slider grab: claim the gesture so swipe/scroll can't steal it, and
        // apply immediately so a plain tap also sets the value.
        {
            const SettingId sid = settingsSliderHit(x, y);
            if (sid != ST_COUNT) {
                tt.gesture    = GESTURE_SLIDER;
                active_slider = sid;
                sliderSetFromX(sid, x);
                settingsDirty = true;
            }
        }
        return;
    }

    if (now && tt.active) {
        // ---- Touch dragging ----
        const int dxFromStart = x - tt.startX;
        const int dyFromStart = y - tt.startY;

        // Slider drag: live-update the active slider from current X.
        if (tt.gesture == GESTURE_SLIDER) {
            sliderSetFromX(active_slider, x);
            settingsDirty = true;
            tt.lastX = x; tt.lastY = y;
            return;
        }

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
        Serial.printf("TR g=%d dx=%d dy=%d dur=%lu\n", (int)tt.gesture, dx, dy, (unsigned long)dur);  // TEMP touch debug

        if (tt.gesture == GESTURE_DRAG_V) {
            // Snap the settings scroll to a row boundary on release so every
            // visible row sits fully inside the tappable body band (otherwise a
            // row's -/+ buttons can straddle the y>=BODY_BOTTOM dead zone and
            // refuse taps at certain scroll offsets).
            if (currentPage == PAGE_SETTINGS) {
                snapSettingsScroll();
                settingsDirty = true;
            }
        } else if (tt.gesture == GESTURE_SLIDER) {
            // RPM smoothing must reach the Teensy on the fly -> push it the moment
            // the finger lifts (it also rides the full re-send on exit + the 5 s
            // periodic re-send). Brightness was already applied live to the panel.
            if (active_slider == ST_RPM_SMOOTH)
                Serial.printf("CFG,rpmsm,%d\n", (int)s.rpm_smooth);
            active_slider = ST_COUNT;
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
                } else if (dx < 0 && currentPage == PAGE_TOOLS) {
                    currentPage = PAGE_SESSIONS;
                    pageJustEntered = true;
                } else if (dx > 0 && currentPage == PAGE_SESSIONS) {
                    currentPage = PAGE_TOOLS;
                    pageJustEntered = true;
                }
            }
        } else {  // GESTURE_NONE — never moved much, treat as tap
            if (abs(dx) < TAP_DXY_MAX && abs(dy) < TAP_DXY_MAX && dur < TAP_MS_MAX) {
                if (currentPage == PAGE_DASH)          handleDashTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_SETTINGS) handleSettingsTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_COACH)   handleCoachTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_TOOLS)    handleToolsTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_STATUS)   handleStatusTap(tt.startX, tt.startY);
                else if (currentPage == PAGE_SESSIONS) handleSessionsTap(tt.startX, tt.startY);
            }
        }
        tt.active = false;
    }
}

static void handleDashTap(int x, int y) {
    // TRACK button — opens picker in select-only mode when idle. While
    // RECORDING it becomes the SET S/F button (v0.1.115): two-tap (arm, then
    // confirm within 5 s) captures the current position/heading as the active
    // track's custom start/finish — settable whenever you want, mid-session.
    if (x >= TRKBTN_X && x < TRKBTN_X + TRKBTN_W &&
        y >= TRKBTN_Y && y < TRKBTN_Y + TRKBTN_H) {
        if (!recording) {
            openTrackPicker(false);
            return;
        }
        if (lapTrackIdx() >= 0) return;   // known track: S/F is web-managed (v0.1.129)
        const bool wasArmed = sf_set_armed && (millis() - sf_set_arm_ms < 5000);
        if (wasArmed) {
            sf_set_armed = false;
            captureSfHere();
        } else {
            sf_set_armed  = true;
            sf_set_arm_ms = millis();
        }
        return;
    }

    // Manual UPLOAD button (only acts when visible: idle + queue > 0).
    // Coach checklist button (only when actually shown — same predicate as the
    // renderer, so an invisible button can never be tapped).
    if (s.coach_show && !recording && coach_n > 0 &&
        x >= CHBTN_X && x < CHBTN_X + CHBTN_W &&
        y >= CHBTN_Y && y < CHBTN_Y + CHBTN_H) {
        currentPage     = PAGE_COACH;
        pageJustEntered = true;
        coach_dirty     = true;
        return;
    }
    if (x >= UPBTN_X && x < UPBTN_X + UPBTN_W &&
        y >= UPBTN_Y && y < UPBTN_Y + UPBTN_H) {
        if (!recording && cloud_queue_depth > 0) {
            // Dash-initiated upload (v0.1.34+). Pop the modal locally and
            // kick the state machine, which sends Q,LIST to the Teensy and
            // drives the rest of the upload flow.
            if (net_owner != NET_WIFI) {
                // Bluetooth owns the radio — hand it to WiFi first (running
                // both crashes the core). netOwnerTick starts the upload once
                // WiFi connects; closeUploadModal hands the radio back to BT.
                openUploadModal("waiting for WiFi (BT paused)...", 0);
                net_pending_upload = true;
                btReleaseRadio();
            } else {
                openUploadModal("", 0);
                ufStartListing();
            }
        }
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
                // AUTO flow never prompts (v0.1.115): a config track defaults
                // to its PRIMARY sub-track (config 0 — e.g. Summit Point Main).
                // Variants are chosen deliberately from the track picker.
                saveLastTrack(idx);   // also resets active_cfg_idx to 0
                Serial.printf("TRACK,%s\n", active_track_name);
                Serial.printf("REC,1\n");
                recording = true; rec_start_ms = millis();
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
// Engine-running threshold for the voltage display/warning: below this RPM
// the alternator isn't spinning meaningfully and 12.x V is normal, not a fault.
static constexpr uint16_t ENGINE_RUNNING_RPM = 500;

// Highest-priority ACTIVE sensor warning — drives the full-screen warning
// flash with the warning NAME (v0.1.110). Priority: OIL (engine-killing) >
// TEMP > VOLT > AFR. Mirrors the per-line warn_active conditions in
// drawDashPage; returns false when nothing is warning.
static bool activeSensorWarning(const char** label, uint16_t* color) {
    const uint32_t nowMs   = millis();
    const bool             fromMs3  = (s.sensor_type == 1);
    const bool             fromBt   = (s.sensor_type == 2);
    const bool             ecuStale = (ecu.last_ms == 0) || (nowMs - ecu.last_ms > 2000);
    // OIL — low pressure (direct A2 transducer in every mode)
    if (s.show_oil_psi && eng.oil_psi_x10 >= 0
        && eng.oil_psi_x10 <= (int)s.oil_warn_psi * 10) {
        *label = "OIL"; *color = PALETTE[s.oil_warn_col]; return true;
    }
    // TEMP — coolant over threshold (source per sensor_type)
    {
        const int16_t coolant = fromBt  ? obd::coolantF_x10()
                              : fromMs3 ? ecu.coolant_f_x10 : eng.coolant_f_x10;
        const bool fault = (coolant < 0) || (fromMs3 && ecuStale)
                           || (fromBt && !obd::dataFresh());
        if (s.show_coolant && !fault && coolant >= (int)s.coolant_warn_f * 10) {
            *label = "TEMP"; *color = PALETTE[s.coolant_warn_col]; return true;
        }
    }
    // VOLT — low system voltage WHILE RUNNING (failing alternator/belt).
    // Gated on RPM so a parked car with ignition on (12.x V) never warns.
    if (s.show_volt && eng.rpm >= ENGINE_RUNNING_RPM) {
        int16_t v = -1;
        if (fromBt && obd::dataFresh() && obd::voltX10() > 0) v = obd::voltX10();
        else if (fromMs3 && !ecuStale && ecu.bat_x10 > 0)     v = ecu.bat_x10;
        if (v > 0 && v <= (int)s.volt_warn_x10) {
            *label = "VOLT"; *color = PALETTE[s.volt_warn_col]; return true;
        }
    }
    // AFR — out of band (MS3 mode only)
    if (s.show_afr && fromMs3 && !ecuStale && ecu.afr_x10 >= 0
        && (ecu.afr_x10 < (int)s.afr_warn_lo_x10
            || ecu.afr_x10 > (int)s.afr_warn_hi_x10)) {
        *label = "AFR"; *color = PALETTE[s.afr_warn_col]; return true;
    }
    return false;
}

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
    int16_t  spd_int     = -1;       // whole-MPH integer for stable equality checks
    uint8_t  fix         = 0xFF;
    uint8_t  sats        = 0xFF;
    uint8_t  status      = 0xFF;
    int8_t   recording   = -1;       // -1=never drawn; 0=stopped; 1=recording
    uint32_t pred_lap_cs = UINT32_MAX;   // predictive lap time in centiseconds
    uint32_t last_lap_cs = UINT32_MAX;   // last completed lap time in centiseconds
    int32_t  delta_cs    = INT32_MIN;    // predictive delta vs best (centiseconds, signed; INT32_MIN=redraw)
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
    int32_t  volt_x10     = INT32_MIN;   // VOLT line (shares the AFR row)
    uint32_t volt_col_tag = UINT32_MAX;
    uint8_t  trkbtn_state = 0xFF;        // TRACK / SET S/F button (v0.1.115)
    // REC badge state: composite tag of (dash recording bit, teensy ack bit,
    // mismatch-warning bit), plus the last-drawn sample count and queue depth.
    uint8_t  rec_badge_tag = 0xFF;
    uint32_t rec_samples  = UINT32_MAX;
    uint32_t cld_queue    = UINT32_MAX;
    // Composite tag for the manual UPLOAD button: low bit = visible, upper
    // bits = queue count. Sentinel UINT32_MAX = needs redraw.
    uint32_t upbtn_tag    = UINT32_MAX;
    uint32_t chbtn_tag    = UINT32_MAX;   // coach checklist button (v0.1.137)
    int32_t  lap_number   = INT32_MIN;   // current lap counter shown near the speed
};
static LastDrawn ld;
static void invalidateAll() {
    ld.rpm_fillW = -1; ld.rpm_text = -1; ld.spd_int = -1;
    ld.fix = 0xFF; ld.sats = 0xFF; ld.status = 0xFF;
    ld.recording = -1;
    ld.rec_badge_tag = 0xFF; ld.rec_samples = UINT32_MAX; ld.cld_queue = UINT32_MAX;
    ld.upbtn_tag = UINT32_MAX;
    ld.pred_lap_cs = UINT32_MAX; ld.last_lap_cs = UINT32_MAX;
    ld.delta_cs    = INT32_MIN;
    ld.lap_number  = INT32_MIN;
    ld.track_tag   = UINT32_MAX;
    ld.temp_x10 = INT32_MIN; ld.temp_col_tag = UINT32_MAX;
    ld.psi_x10  = INT32_MIN; ld.psi_col_tag  = UINT32_MAX;
    ld.afr_x10  = INT32_MIN; ld.afr_col_tag  = UINT32_MAX;
    ld.volt_x10 = INT32_MIN; ld.volt_col_tag = UINT32_MAX;
    ld.trkbtn_state = 0xFF;
}

static void drawRecordButton() {
    // Auto-start dwell in progress -> amber button counting down ("AUTO 3"),
    // so a held-speed arming is visible rather than mysterious.
    const bool     counting = (!recording && autostart_pending_ms > 0);
    const uint16_t fill   = recording ? TFT_RED : (counting ? TFT_ORANGE : TFT_GREEN);
    const uint16_t border = TFT_WHITE;
    char           cbuf[12];
    if (counting) snprintf(cbuf, sizeof(cbuf), "AUTO %u",
                           (unsigned)((autostart_pending_ms + 999) / 1000));
    const char*    label  = recording ? "STOP" : (counting ? cbuf : "START");

    if (dash_sprites_ready) {
        spr_recbtn.fillSprite(fill);
        spr_recbtn.drawRect(0, 0, RECBTN_W,     RECBTN_H,     border);
        spr_recbtn.drawRect(1, 1, RECBTN_W - 2, RECBTN_H - 2, border);
        spr_recbtn.drawRect(2, 2, RECBTN_W - 4, RECBTN_H - 4, border);
        spr_recbtn.setFont(&fonts::Font4);
        spr_recbtn.setTextSize(1);
        spr_recbtn.setTextDatum(textdatum_t::middle_center);
        spr_recbtn.setTextColor(TFT_WHITE);
        spr_recbtn.drawString(label, RECBTN_W / 2, RECBTN_H / 2);
        spr_recbtn.pushSprite(RECBTN_X, RECBTN_Y);
    } else {
        tft.fillRect(RECBTN_X, RECBTN_Y, RECBTN_W, RECBTN_H, fill);
        tft.drawRect(RECBTN_X,     RECBTN_Y,     RECBTN_W,     RECBTN_H,     border);
        tft.drawRect(RECBTN_X + 1, RECBTN_Y + 1, RECBTN_W - 2, RECBTN_H - 2, border);
        tft.drawRect(RECBTN_X + 2, RECBTN_Y + 2, RECBTN_W - 4, RECBTN_H - 4, border);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextColor(TFT_WHITE, fill);
        tft.drawString(label, RECBTN_X + RECBTN_W / 2, RECBTN_Y + RECBTN_H / 2);
    }
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

    // ---- full-screen sensor warning flash (v0.1.110) -----------------------
    // When a sensor warning (OIL / TEMP / VOLT / AFR) is active, the WHOLE
    // screen flashes its warn color at 2 Hz with the warning NAME huge in the
    // middle — a glance tells you WHAT is wrong, not just that something is.
    // The ON phase suppresses the normal dash render (like the lap popup);
    // the OFF phase falls through to it (so the RPM shift flash still shows
    // interleaved). Transitions force a full repaint via pageJustEntered.
    {
        static bool        warn_shown = false;   // colored label frame on screen
        static const char* warn_drawn = nullptr;
        const char* wl = nullptr; uint16_t wc = 0;
        const bool  wactive = activeSensorWarning(&wl, &wc);
        if (wactive && (((millis() / 250) & 1) == 0)) {   // 2 Hz, ON phase
            if (!warn_shown || warn_drawn != wl) {
                tft.fillScreen(wc);
                tft.setFont(&fonts::Font4);
                tft.setTextSize(6);                        // ~156 px tall
                tft.setTextDatum(textdatum_t::middle_center);
                tft.setTextColor(TFT_BLACK, wc);
                tft.drawString(wl, 400, 240);
                tft.setTextSize(1);
                tft.setTextDatum(textdatum_t::top_left);
                warn_shown = true; warn_drawn = wl;
            }
            return;
        }
        if (warn_shown) {   // leaving the ON phase (or warning cleared)
            warn_shown = false; warn_drawn = nullptr;
            pageJustEntered = true;
        }
    }

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
        // x=264 (not 255): clears the bottom-left TEMP/PSI/AFR sensor sprites,
        // which span x=20..260 and were re-painting bg over the first letter
        // (P/L/D) of these labels each time a sensor value updated.
        // Middle-column rows moved to a 28 px pitch: the VALUES are Font4 now
        // (26 px, "slightly bigger" per driver request) — labels stay Font2.
        tft.drawString("PRED", 264, 378);    // middle column — predictive lap time
        tft.drawString("LAP",  264, 406);    // middle column — last completed lap time
        tft.drawString("DELTA",264, 434);    // middle column — predictive delta vs best lap
        tft.drawString("FIX",  620, 380);
        tft.drawString("SATS", 620, 405);
        tft.drawString("GPS",  620, 430);

        // (TRACK / SET S/F button is drawn by the dynamic block below — it
        //  changes with recording/armed state since v0.1.115.)

        ld.bg = bg;
        pageJustEntered = false;
        invalidateAll();
    }

    // ---- RPM bar fill ----
    // Sprite-buffered: render the full inner band into spr_rpm_bar and DMA it
    // across so the user never sees a half-filled bar mid-LCD-scan. The
    // outer white border was painted directly to the framebuffer on page
    // entry and isn't touched here.
    {
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
            if (dash_sprites_ready) {
                spr_rpm_bar.fillSprite(bg);
                if (fillW > 0)
                    spr_rpm_bar.fillRect(0, 0, fillW, ih, fillColor);
                spr_rpm_bar.pushSprite(RPM_BAR_X + 2, RPM_BAR_Y + 2);
            } else {
                const int ix = RPM_BAR_X + 2;
                const int iy = RPM_BAR_Y + 2;
                if (fillW < ld.rpm_fillW)
                    tft.fillRect(ix + fillW, iy, ld.rpm_fillW - fillW, ih, bg);
                if (fillW > 0) tft.fillRect(ix, iy, fillW, ih, fillColor);
            }
            ld.rpm_fillW = fillW;
            ld.rpm_color = fillColor;
        }
    }

    // ---- RPM number, just under the bar at the right edge (Font2) ----
    if ((int32_t)eng.rpm != ld.rpm_text) {
        char rpmBuf[8]; snprintf(rpmBuf, sizeof(rpmBuf), "%u", (unsigned)eng.rpm);
        if (dash_sprites_ready) {
            spr_rpm_text.fillSprite(bg);
            spr_rpm_text.setFont(&fonts::Font2);
            spr_rpm_text.setTextSize(1);
            spr_rpm_text.setTextColor(TFT_LIGHTGREY);
            spr_rpm_text.setTextDatum(textdatum_t::top_right);
            spr_rpm_text.drawString(rpmBuf, 108, 0);
            spr_rpm_text.pushSprite(RPM_BAR_X + RPM_BAR_W - 108, RPM_BAR_Y + RPM_BAR_H + 4);
        } else {
            tft.setTextSize(1);
            tft.setFont(&fonts::Font2);
            tft.setTextColor(TFT_LIGHTGREY, bg);
            tft.setTextDatum(textdatum_t::top_right);
            tft.setTextPadding(80);
            tft.drawString(rpmBuf, RPM_BAR_X + RPM_BAR_W, RPM_BAR_Y + RPM_BAR_H + 4);
            tft.setTextPadding(0);
        }
        ld.rpm_text = eng.rpm;
    }

    // ---- Finish-line lap-time popup ----
    // Shows the just-completed lap time HUGE over everything BELOW the RPM
    // bar + RPM number (driver keeps the live bar). The RPM warning flash
    // takes precedence: while the alert bg is flashing (bg != black) the
    // popup hides; if the popup window is still open when the flash ends it
    // re-draws. Ends after s.lap_overlay_s seconds -> full clean repaint.
    {
        static bool popup_on = false;
        const bool alert_flashing = (bg != TFT_BLACK);
        const bool want = (lap_overlay_until_ms != 0)
                          && (millis() < lap_overlay_until_ms)
                          && !alert_flashing;
        if (want) {
            if (!popup_on) {
                popup_on = true;
                const int top = RPM_BAR_Y + RPM_BAR_H + 26;   // below bar + RPM number
                tft.fillRect(0, top, 800, 480 - top, TFT_BLACK);
                char cap[24];
                snprintf(cap, sizeof(cap), lap_overlay_is_best ? "LAP %d  -  BEST" : "LAP %d",
                         lap_overlay_lapn > 0 ? lap_overlay_lapn : 1);
                tft.setTextDatum(textdatum_t::middle_center);
                tft.setFont(&fonts::Font4);
                tft.setTextSize(1);
                tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
                tft.drawString(cap, 400, top + 30);
                char buf[16];
                formatLapTime(lap_overlay_lap_ms, buf, sizeof(buf));
                tft.setFont(&fonts::Font7);
                tft.setTextSize(2);
                tft.setTextColor(lap_overlay_is_best ? TFT_GREEN : TFT_YELLOW, TFT_BLACK);
                tft.drawString(buf, 400, top + (480 - top) / 2 + 20);
                tft.setTextSize(1);
                tft.setTextDatum(textdatum_t::top_left);
            }
            return;   // freeze the rest of the dash under the popup
        }
        if (popup_on) {
            popup_on = false;
            // Natural expiry clears the popup; an ALERT-FLASH interruption
            // keeps the window armed so the popup returns when the flash ends
            // (crossing the line at high RPM is the common case).
            if (millis() >= lap_overlay_until_ms) lap_overlay_until_ms = 0;
            pageJustEntered = true;   // full clean repaint on the next pass
            return;
        }
    }

    // ---- HUGE speed number — Font7 size 4 ----
    // Always integer MPH; max 3 digits. Sprite-buffered so the user never
    // sees a half-rendered digit during the LCD scan — this is the biggest
    // tearing offender on the dash page given its size and 25 Hz update.
    {
        // Link-stale honesty (v0.1.122): if no GPS line has arrived for 3 s,
        // show "--" instead of silently freezing the last number — the driver
        // must KNOW the display is blind (the recording on the Teensy side is
        // typically still fine; this is a dash-link problem, not a GPS one).
        const bool link_stale = (g.last_ms == 0) || (millis() - g.last_ms > 3000);
        const int spd_int = link_stale ? -2 : (int)(g.mph + 0.5f);   // -1 = never-drawn sentinel
        if (spd_int != ld.spd_int) {
            char buf[8];
            if (spd_int < 0) snprintf(buf, sizeof(buf), "--");
            else             snprintf(buf, sizeof(buf), "%d", spd_int);
            if (dash_sprites_ready) {
                spr_speed.fillSprite(bg);
                spr_speed.setFont(&fonts::Font7);
                spr_speed.setTextSize(4);
                spr_speed.setTextColor(TFT_WHITE);
                spr_speed.setTextDatum(textdatum_t::middle_center);
                spr_speed.drawString(buf, SPEED_PAD_W / 2, 100);
                spr_speed.pushSprite(SPEED_CX - SPEED_PAD_W / 2, 230 - 100);
            } else {
                tft.setTextColor(TFT_WHITE, bg);
                tft.setTextDatum(textdatum_t::middle_center);
                tft.setFont(&fonts::Font7);
                tft.setTextSize(4);
                tft.setTextPadding(SPEED_PAD_W);
                tft.drawString(buf, SPEED_CX, 230);
                tft.setTextPadding(0);
                tft.setTextSize(1);
            }
            ld.spd_int = (int16_t)spd_int;
            ld.recording = -1;        // speed redraw can clip the button — force its repaint
        }
    }

    // ---- Lap counter (right of the speed, just above the FIX/SATS/GPS column) ----
    // Bigger than the GPS labels, smaller than the speed. Current lap being
    // driven; "LAP --" until the first start/finish crossing arms timing.
    {
        const int lapn = lapTimer.active ? lapTimer.lap_number : 0;
        if (lapn != ld.lap_number) {
            char buf[12];
            if (lapn > 0) snprintf(buf, sizeof(buf), "LAP %d", lapn);
            else          snprintf(buf, sizeof(buf), "LAP --");
            tft.setFont(&fonts::Font4);
            tft.setTextSize(1);
            tft.setTextColor(TFT_YELLOW, bg);
            tft.setTextDatum(textdatum_t::top_left);
            tft.setTextPadding(170);
            tft.drawString(buf, 610, 346);
            tft.setTextPadding(0);
            tft.setTextDatum(textdatum_t::top_left);
            ld.lap_number = lapn;
        }
    }

    // ---- Start/Stop button (sits to the left of the speed) ----
    // TRACK / SET S/F button (dynamic, v0.1.115): TRACK + picker when idle;
    // while RECORDING it becomes the on-the-fly S/F capture (two-tap). The
    // confirmation flashes green for 3 s after a capture.
    {
        uint8_t st = 0;   // 0=TRACK 1=SET S/F 2=TAP AGAIN(armed) 3=result msg
        if (recording && lapTrackIdx() < 0) {
            // v0.1.129: SET S/F mode only at UNKNOWN tracks — known tracks'
            // S/F is web-managed, so the button stays a plain TRACK label.
            const bool armed   = sf_set_armed && (millis() - sf_set_arm_ms < 5000);
            const bool haveMsg = sf_set_msg[0] && (millis() - sf_set_msg_ms < 3000);
            st = haveMsg ? 3 : armed ? 2 : 1;
        }
        if (st != ld.trkbtn_state) {
            const uint16_t fill = (st == 0) ? TFT_DARKCYAN
                                : (st == 1) ? TFT_NAVY
                                : (st == 2) ? TFT_ORANGE : TFT_DARKGREEN;
            const uint16_t fg   = (st == 2) ? TFT_BLACK : TFT_WHITE;
            const char* lbl = (st == 0) ? "TRACK"
                            : (st == 1) ? "SET S/F"
                            : (st == 2) ? "TAP AGAIN"
                            : (strncmp(sf_set_msg, "S/F", 3) == 0 ? "S/F SET!" : "NO TRACK");
            tft.fillRect(TRKBTN_X, TRKBTN_Y, TRKBTN_W, TRKBTN_H, fill);
            tft.drawRect(TRKBTN_X,     TRKBTN_Y,     TRKBTN_W,     TRKBTN_H,     TFT_WHITE);
            tft.drawRect(TRKBTN_X + 1, TRKBTN_Y + 1, TRKBTN_W - 2, TRKBTN_H - 2, TFT_WHITE);
            tft.setFont(&fonts::Font4);
            tft.setTextSize(1);
            tft.setTextDatum(textdatum_t::middle_center);
            tft.setTextColor(fg, fill);
            tft.drawString(lbl, TRKBTN_X + TRKBTN_W / 2, TRKBTN_Y + TRKBTN_H / 2);
            tft.setTextDatum(textdatum_t::top_left);
            ld.trkbtn_state = st;
        }
    }

    {
        // Repaint on record-state change OR each whole second of the auto-start
        // countdown (encoded into the cached tag so nothing else redraws).
        const int tag = recording ? -100
                      : (autostart_pending_ms > 0
                         ? (int)((autostart_pending_ms + 999) / 1000) : 0);
        if (tag != ld.recording) {
            drawRecordButton();
            ld.recording = tag;
        }
    }

    // ---- Manual UPLOAD button (between START and the speed digit) ----
    // Visible whenever we are idle (not recording) and the SD queue has
    // at least one file waiting. The Teensy queue walker runs every 10 s
    // automatically; this button just kicks it immediately so the user
    // can manually trigger a drain from the dash.
    {
        const bool visible = (!recording) && (cloud_queue_depth > 0);
        const uint32_t tag = (visible ? (uint32_t)1 : 0)
                           | ((cloud_queue_depth & 0xFFFFFF) << 1);
        if (tag != ld.upbtn_tag) {
            if (dash_sprites_ready) {
                if (visible) {
                    spr_upbtn.fillSprite(TFT_DARKCYAN);
                    spr_upbtn.drawRect(0, 0, UPBTN_W,     UPBTN_H,     TFT_WHITE);
                    spr_upbtn.drawRect(1, 1, UPBTN_W - 2, UPBTN_H - 2, TFT_WHITE);
                    spr_upbtn.setFont(&fonts::Font4);
                    spr_upbtn.setTextSize(1);
                    spr_upbtn.setTextDatum(textdatum_t::middle_center);
                    spr_upbtn.setTextColor(TFT_WHITE);
                    spr_upbtn.drawString("UPLOAD", UPBTN_W / 2, UPBTN_H / 2 - 12);
                    char buf[24];
                    snprintf(buf, sizeof(buf), "%lu file%s",
                             (unsigned long)cloud_queue_depth,
                             cloud_queue_depth == 1 ? "" : "s");
                    spr_upbtn.setFont(&fonts::Font2);
                    spr_upbtn.drawString(buf, UPBTN_W / 2, UPBTN_H / 2 + 16);
                } else {
                    spr_upbtn.fillSprite(bg);
                }
                spr_upbtn.pushSprite(UPBTN_X, UPBTN_Y);
            } else {
                if (visible) {
                    tft.fillRect(UPBTN_X, UPBTN_Y, UPBTN_W, UPBTN_H, TFT_DARKCYAN);
                    tft.drawRect(UPBTN_X,     UPBTN_Y,     UPBTN_W,     UPBTN_H,     TFT_WHITE);
                    tft.drawRect(UPBTN_X + 1, UPBTN_Y + 1, UPBTN_W - 2, UPBTN_H - 2, TFT_WHITE);
                    tft.setFont(&fonts::Font4);
                    tft.setTextSize(1);
                    tft.setTextDatum(textdatum_t::middle_center);
                    tft.setTextColor(TFT_WHITE, TFT_DARKCYAN);
                    tft.drawString("UPLOAD", UPBTN_X + UPBTN_W / 2, UPBTN_Y + UPBTN_H / 2 - 12);
                    char buf[24];
                    snprintf(buf, sizeof(buf), "%lu file%s",
                             (unsigned long)cloud_queue_depth,
                             cloud_queue_depth == 1 ? "" : "s");
                    tft.setFont(&fonts::Font2);
                    tft.setTextColor(TFT_WHITE, TFT_DARKCYAN);
                    tft.drawString(buf, UPBTN_X + UPBTN_W / 2, UPBTN_Y + UPBTN_H / 2 + 16);
                } else {
                    tft.fillRect(UPBTN_X, UPBTN_Y, UPBTN_W, UPBTN_H, bg);
                }
            }
            ld.upbtn_tag = tag;
        }
    }

    // ---- Coach checklist button (under UPLOAD, v0.1.137) ----
    // Visible only when: the setting is on, we're NOT recording (mid-session is
    // no time to read coaching), and the server actually has open items. Ticked
    // items never arrive here — the server only serves open ones.
    {
        const bool visible = s.coach_show && !recording && coach_n > 0;
        const uint32_t tag = visible ? (uint32_t)(0x100 | coach_n) : 0;
        if (tag != ld.chbtn_tag) {
            if (visible) {
                tft.fillRect(CHBTN_X, CHBTN_Y, CHBTN_W, CHBTN_H, TFT_PURPLE);
                tft.drawRect(CHBTN_X,     CHBTN_Y,     CHBTN_W,     CHBTN_H,     TFT_WHITE);
                tft.drawRect(CHBTN_X + 1, CHBTN_Y + 1, CHBTN_W - 2, CHBTN_H - 2, TFT_WHITE);
                tft.setFont(&fonts::Font4);
                tft.setTextSize(1);
                tft.setTextDatum(textdatum_t::middle_center);
                tft.setTextColor(TFT_WHITE, TFT_PURPLE);
                tft.drawString("COACH", CHBTN_X + CHBTN_W / 2, CHBTN_Y + CHBTN_H / 2 - 10);
                char cb[20];
                snprintf(cb, sizeof(cb), "%d item%s", (int)coach_n, coach_n == 1 ? "" : "s");
                tft.setFont(&fonts::Font2);
                tft.drawString(cb, CHBTN_X + CHBTN_W / 2, CHBTN_Y + CHBTN_H / 2 + 14);
                tft.setTextDatum(textdatum_t::top_left);
            } else {
                tft.fillRect(CHBTN_X, CHBTN_Y, CHBTN_W, CHBTN_H, bg);
            }
            ld.chbtn_tag = tag;
        }
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
            if (dash_sprites_ready) {
                spr_rec_badge.fillSprite(bg);
                spr_rec_badge.setFont(&fonts::Font2);
                spr_rec_badge.setTextSize(1);
                spr_rec_badge.setTextDatum(textdatum_t::middle_left);
                if (dash_rec && teensy_ok) {
                    spr_rec_badge.fillCircle(6, BH/2, 5, TFT_RED);
                    spr_rec_badge.setTextColor(TFT_WHITE);
                    char buf[24]; snprintf(buf, sizeof(buf), "REC  %lu", (unsigned long)samples);
                    spr_rec_badge.drawString(buf, 18, BH/2);
                } else if (warn) {
                    spr_rec_badge.fillCircle(6, BH/2, 5, TFT_ORANGE);
                    spr_rec_badge.setTextColor(TFT_ORANGE);
                    spr_rec_badge.drawString("REC ? no ack", 18, BH/2);
                } else if (qd > 0) {
                    spr_rec_badge.setTextColor(TFT_CYAN);
                    char qbuf[20]; snprintf(qbuf, sizeof(qbuf), "queue: %lu", (unsigned long)qd);
                    spr_rec_badge.drawString(qbuf, 0, BH/2);
                }
                spr_rec_badge.pushSprite(BX, BY);
            } else {
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
                    tft.setTextColor(TFT_CYAN, bg);
                    char qbuf[20]; snprintf(qbuf, sizeof(qbuf), "queue: %lu", (unsigned long)qd);
                    tft.drawString(qbuf, BX, BY + BH/2);
                }
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
        const bool    fromBt    = (s.sensor_type == 2);   // BLE OBD-II dongle
        const int16_t coolant   = fromBt  ? obd::coolantF_x10()
                                : fromMs3 ? ecu.coolant_f_x10
                                          : eng.coolant_f_x10;
        const bool    fault     = (coolant < 0) || (fromMs3 && ecuStale)
                                  || (fromBt && !obd::dataFresh());
        const bool    warn_active = s.show_coolant && !fault
                                    && (coolant >= (int)s.coolant_warn_f * 10);
        const uint32_t tag = ((uint32_t)s.show_coolant << 24)
                           | ((uint32_t)s.coolant_warn_col << 16)
                           | ((uint32_t)fault << 8)
                           | ((uint32_t)warn_active)
                           | ((uint32_t)fromMs3 << 12);   // re-render on source flip
        const int32_t  val = s.show_coolant ? (int32_t)coolant : INT32_MIN + 1;
        if (val != ld.temp_x10 || tag != ld.temp_col_tag) {
            char buf[24] = "";
            uint16_t col = TFT_WHITE;
            if (s.show_coolant) {
                if (fault) { snprintf(buf, sizeof(buf), "TEMP: ---"); col = TFT_DARKGREY; }
                else {
                    const int t = (coolant + 5) / 10;
                    snprintf(buf, sizeof(buf), "TEMP: %d\xB0""F", t);
                    col = warn_active ? PALETTE[s.coolant_warn_col] : TFT_WHITE;
                }
            }
            if (dash_sprites_ready) {
                spr_temp.fillSprite(bg);
                if (s.show_coolant && buf[0]) {
                    spr_temp.setFont(&fonts::Font4);
                    spr_temp.setTextSize(1);
                    spr_temp.setTextDatum(textdatum_t::top_left);
                    spr_temp.setTextColor(col);
                    spr_temp.drawString(buf, 0, 0);
                }
                spr_temp.pushSprite(SENS_X, SENS_TEMP_Y);
            } else {
                tft.fillRect(SENS_X, SENS_TEMP_Y, SENS_W, SENS_H, bg);
                if (s.show_coolant && buf[0]) {
                    tft.setFont(&fonts::Font4);
                    tft.setTextSize(1);
                    tft.setTextDatum(textdatum_t::top_left);
                    drawValue(SENS_X, SENS_TEMP_Y, buf, col);
                }
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
            char buf[24] = "";
            uint16_t col = TFT_WHITE;
            if (s.show_oil_psi) {
                if (fault) { snprintf(buf, sizeof(buf), "PSI: ---"); col = TFT_DARKGREY; }
                else {
                    const int p = (eng.oil_psi_x10 + 5) / 10;
                    snprintf(buf, sizeof(buf), "PSI: %d", p);
                    col = warn_active ? PALETTE[s.oil_warn_col] : TFT_WHITE;
                }
            }
            if (dash_sprites_ready) {
                spr_psi.fillSprite(bg);
                if (s.show_oil_psi && buf[0]) {
                    spr_psi.setFont(&fonts::Font4);
                    spr_psi.setTextSize(1);
                    spr_psi.setTextDatum(textdatum_t::top_left);
                    spr_psi.setTextColor(col);
                    spr_psi.drawString(buf, 0, 0);
                }
                spr_psi.pushSprite(SENS_X, SENS_PSI_Y);
            } else {
                tft.fillRect(SENS_X, SENS_PSI_Y, SENS_W, SENS_H, bg);
                if (s.show_oil_psi && buf[0]) {
                    tft.setFont(&fonts::Font4);
                    tft.setTextSize(1);
                    tft.setTextDatum(textdatum_t::top_left);
                    drawValue(SENS_X, SENS_PSI_Y, buf, col);
                }
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
            char buf[24] = "";
            uint16_t col = TFT_WHITE;
            if (visible) {
                if (fault) { snprintf(buf, sizeof(buf), "AFR: ---"); col = TFT_DARKGREY; }
                else {
                    snprintf(buf, sizeof(buf), "AFR: %d.%d",
                             ecu.afr_x10 / 10, ecu.afr_x10 % 10);
                    col = warn_active ? PALETTE[s.afr_warn_col] : TFT_WHITE;
                }
            }
            if (dash_sprites_ready) {
                spr_afr.fillSprite(bg);
                if (visible && buf[0]) {
                    spr_afr.setFont(&fonts::Font4);
                    spr_afr.setTextSize(1);
                    spr_afr.setTextDatum(textdatum_t::top_left);
                    spr_afr.setTextColor(col);
                    spr_afr.drawString(buf, 0, 0);
                }
                spr_afr.pushSprite(SENS_X, SENS_AFR_Y);
            } else {
                tft.fillRect(SENS_X, SENS_AFR_Y, SENS_W, SENS_H, bg);
                if (visible && buf[0]) {
                    tft.setFont(&fonts::Font4);
                    tft.setTextSize(1);
                    tft.setTextDatum(textdatum_t::top_left);
                    drawValue(SENS_X, SENS_AFR_Y, buf, col);
                }
            }
            ld.afr_x10     = val;
            ld.afr_col_tag = tag;
        }
    }

    // ---- Voltage line (v0.1.110) ----
    // Shares the AFR row (no free row below it). Renders only when the AFR
    // line isn't visible (AFR is MS3-only; voltage matters most in BT mode),
    // the engine is RUNNING, and a live source exists (BT ATRV / MS3 CAN bat).
    {
        const bool afrVisible = s.show_afr && (s.sensor_type == 1);
        int16_t v = -1;
        if (s.sensor_type == 2 && obd::dataFresh() && obd::voltX10() > 0) v = obd::voltX10();
        else if (s.sensor_type == 1 && !ecuStale && ecu.bat_x10 > 0)      v = ecu.bat_x10;
        const bool visible     = s.show_volt && !afrVisible
                                 && (eng.rpm >= ENGINE_RUNNING_RPM) && (v > 0);
        const bool warn_active = visible && v <= (int)s.volt_warn_x10;
        if (afrVisible) {
            // AFR owns the row — never draw, and invalidate so a later mode
            // flip forces our repaint.
            ld.volt_x10 = INT32_MIN; ld.volt_col_tag = UINT32_MAX;
        } else {
            const uint32_t tag = ((uint32_t)visible << 24)
                               | ((uint32_t)s.volt_warn_col << 16)
                               | ((uint32_t)warn_active);
            const int32_t  val = visible ? (int32_t)v : INT32_MIN + 1;
            if (val != ld.volt_x10 || tag != ld.volt_col_tag) {
                char buf[24] = "";
                uint16_t col = TFT_WHITE;
                if (visible) {
                    snprintf(buf, sizeof(buf), "VOLT: %d.%d", v / 10, v % 10);
                    col = warn_active ? PALETTE[s.volt_warn_col] : TFT_WHITE;
                }
                if (dash_sprites_ready) {
                    spr_afr.fillSprite(bg);   // shared row sprite
                    if (visible && buf[0]) {
                        spr_afr.setFont(&fonts::Font4);
                        spr_afr.setTextSize(1);
                        spr_afr.setTextDatum(textdatum_t::top_left);
                        spr_afr.setTextColor(col);
                        spr_afr.drawString(buf, 0, 0);
                    }
                    spr_afr.pushSprite(SENS_X, SENS_AFR_Y);
                } else {
                    tft.fillRect(SENS_X, SENS_AFR_Y, SENS_W, SENS_H, bg);
                    if (visible && buf[0]) {
                        tft.setFont(&fonts::Font4);
                        tft.setTextSize(1);
                        tft.setTextDatum(textdatum_t::top_left);
                        drawValue(SENS_X, SENS_AFR_Y, buf, col);
                    }
                }
                ld.volt_x10     = val;
                ld.volt_col_tag = tag;
            }
        }
    }

    // Restore the small font so the FIX/SATS/GPS block below renders correctly.
    tft.setFont(&fonts::Font2);
    tft.setTextDatum(textdatum_t::top_left);

    // Right column: FIX / SATS / GPS. Sprite-buffered so the value flips are
    // atomic to the LCD scan; the static labels at x=620 were painted once on
    // pageJustEntered and aren't touched here.
    {
        auto drawTextSprite = [&](LGFX_Sprite& s, int sx, int sy,
                                  const char* str, uint16_t col) {
            s.fillSprite(bg);
            s.setFont(&fonts::Font2);
            s.setTextSize(1);
            s.setTextDatum(textdatum_t::top_left);
            s.setTextColor(col);
            s.drawString(str, 0, 0);
            s.pushSprite(sx, sy);
        };
        if (g.fix != ld.fix) {
            const uint16_t col = (g.fix >= 3) ? TFT_GREEN : (g.fix >= 2) ? TFT_YELLOW : TFT_RED;
            char buf[8]; snprintf(buf, sizeof(buf), "%-5s", fixName(g.fix));
            if (dash_sprites_ready) drawTextSprite(spr_fix, 680, 380, buf, col);
            else                    drawValue(680, 380, buf, col);
            ld.fix = g.fix;
        }
        if (g.sats != ld.sats) {
            char buf[8]; snprintf(buf, sizeof(buf), "%2u", (unsigned)g.sats);
            if (dash_sprites_ready) drawTextSprite(spr_sats, 680, 405, buf, TFT_WHITE);
            else                    drawValue(680, 405, buf, TFT_WHITE);
            ld.sats = g.sats;
        }
        if (g.status != ld.status) {
            char buf[8]; snprintf(buf, sizeof(buf), "%-5s", gpsStatusName(g.status));
            const uint16_t col = gpsStatusColor(g.status);
            if (dash_sprites_ready) drawTextSprite(spr_gps, 680, 430, buf, col);
            else                    drawValue(680, 430, buf, col);
            ld.status = g.status;
        }
    }

    // Middle column: predictive (PRED) and last completed (LAP) lap times.
    // PRED is green when on pace for a faster lap, white when ~even, red when
    // slower, grey until there's a ghost lap. LAP is white — a static fact.
    {
        // Blank (grey --) when not recording — lap timing is session-only.
        const uint32_t predMs = recording ? predictiveLapMs() : 0;
        const uint32_t cs = predMs / 10;
        if (cs != ld.pred_lap_cs) {
            char buf[12];
            uint16_t col;
            if (predMs == 0) {
                snprintf(buf, sizeof(buf), "--:--.--");
                col = TFT_DARKGREY;
            } else {
                formatLapTime(predMs, buf, sizeof(buf));
                // Colour by pace vs best: faster=green, same=white, slower=red.
                const int32_t d = predictiveDeltaMs();
                col = (d == INT32_MIN)     ? TFT_WHITE
                    : (d < -DELTA_SAME_MS) ? TFT_GREEN
                    : (d >  DELTA_SAME_MS) ? TFT_RED
                                           : TFT_WHITE;
            }
            if (dash_sprites_ready) {
                spr_pred.fillSprite(bg);
                spr_pred.setFont(&fonts::Font4);
                spr_pred.setTextSize(1);
                spr_pred.setTextDatum(textdatum_t::top_left);
                spr_pred.setTextColor(col);
                spr_pred.drawString(buf, 0, 0);
                spr_pred.pushSprite(325, 372);
            } else {
                tft.setFont(&fonts::Font4);
                tft.setTextDatum(textdatum_t::top_left);
                tft.setTextPadding(150);
                tft.setTextColor(col, bg);
                tft.drawString(buf, 325, 372);
                tft.setTextPadding(0);
            }
            ld.pred_lap_cs = cs;
        }
    }
    {
        // Pre-arm S/F countdown (v0.1.129): until the first crossing arms the
        // timer, the LAP row shows the LIVE DISTANCE to the start/finish —
        // watch it sweep to zero on the out-lap. If it never gets small, the
        // S/F is misplaced and you know it on lap 1, not after the session.
        const bool prearm = recording && lapTimer.active && !lapTimer.timing_started
                            && lapTimer.last_lap_ms == 0 && g.fix >= 2;
        uint32_t cs;
        char buf[12];
        uint16_t col = TFT_WHITE;
        if (prearm) {
            float aLat, aLon, bLat, bLon; bool hasLine;
            effectiveSfLine(lapTimer.track_idx, &aLat, &aLon, &bLat, &bLon, &hasLine);
            if (aLat != 0.0f || aLon != 0.0f) {
                const float mLat = hasLine ? (aLat + bLat) * 0.5f : aLat;
                const float mLon = hasLine ? (aLon + bLon) * 0.5f : aLon;
                const int dm = (int)(trackDistanceKm(g.lat_deg, g.lon_deg, mLat, mLon) * 1000.0f);
                if (dm < 9950) snprintf(buf, sizeof(buf), "SF %dm", dm);
                else           snprintf(buf, sizeof(buf), "SF %.1fkm", dm / 1000.0f);
                col = TFT_CYAN;
                cs  = 0x40000000u | (uint32_t)(dm / 3);   // ~3 m redraw hysteresis
            } else {
                snprintf(buf, sizeof(buf), "--:--.--");
                cs = 0x7FFFFFFFu;
            }
        } else if (lapTimer.last_lap_ms == 0) {
            snprintf(buf, sizeof(buf), "--:--.--");
            cs = 0x7FFFFFFEu;
        } else {
            formatLapTime(lapTimer.last_lap_ms, buf, sizeof(buf));
            cs = lapTimer.last_lap_ms / 10;
        }
        if (cs != ld.last_lap_cs) {
            if (dash_sprites_ready) {
                spr_lap.fillSprite(bg);
                spr_lap.setFont(&fonts::Font4);
                spr_lap.setTextSize(1);
                spr_lap.setTextDatum(textdatum_t::top_left);
                spr_lap.setTextColor(col);
                spr_lap.drawString(buf, 0, 0);
                spr_lap.pushSprite(325, 400);
            } else {
                tft.setFont(&fonts::Font4);
                tft.setTextDatum(textdatum_t::top_left);
                tft.setTextPadding(150);
                tft.setTextColor(col, bg);
                tft.drawString(buf, 325, 400);
                tft.setTextPadding(0);
            }
            ld.last_lap_cs = cs;
        }
    }

    // DELTA — live gap vs the session's best lap at the same point on track.
    // Green when ahead of best pace, white when within DELTA_SAME_MS (even),
    // red when behind, grey until a ghost (best) lap has been recorded.
    {
        // Blank (grey --) when not recording — lap timing is session-only.
        const int32_t deltaMs = recording ? predictiveDeltaMs() : INT32_MIN;
        const int32_t cs = (deltaMs == INT32_MIN) ? INT32_MIN : deltaMs / 10;
        if (cs != ld.delta_cs) {
            char buf[12];
            uint16_t col;
            if (deltaMs == INT32_MIN) {
                snprintf(buf, sizeof(buf), "--.--");
                col = TFT_DARKGREY;
            } else {
                const int32_t a = deltaMs < 0 ? -deltaMs : deltaMs;
                snprintf(buf, sizeof(buf), "%c%u.%02u",
                         deltaMs < 0 ? '-' : '+',
                         (unsigned)(a / 1000), (unsigned)((a % 1000) / 10));
                // faster=green, within DELTA_SAME_MS=white (same), slower=red.
                col = (deltaMs < -DELTA_SAME_MS) ? TFT_GREEN
                    : (deltaMs >  DELTA_SAME_MS) ? TFT_RED
                                                 : TFT_WHITE;
            }
            if (dash_sprites_ready) {
                spr_delta.fillSprite(bg);
                spr_delta.setFont(&fonts::Font4);
                spr_delta.setTextSize(1);
                spr_delta.setTextDatum(textdatum_t::top_left);
                spr_delta.setTextColor(col);
                spr_delta.drawString(buf, 0, 0);
                spr_delta.pushSprite(325, 428);
            } else {
                tft.setFont(&fonts::Font4);
                tft.setTextDatum(textdatum_t::top_left);
                tft.setTextPadding(150);
                tft.setTextColor(col, bg);
                tft.drawString(buf, 325, 428);
                tft.setTextPadding(0);
            }
            ld.delta_cs = cs;
        }
    }

    // Active track name displayed under the TRACK button (Font2). Changes only
    // when the user confirms a new track, but sprite-buffered for consistency.
    {
        uint32_t tag = 0;
        memcpy(&tag, active_track_name, sizeof(tag));
        if (tag != ld.track_tag) {
            if (dash_sprites_ready) {
                spr_track_name.fillSprite(bg);
                if (active_track_name[0]) {
                    spr_track_name.setFont(&fonts::Font2);
                    spr_track_name.setTextSize(1);
                    spr_track_name.setTextDatum(textdatum_t::top_left);
                    spr_track_name.setTextColor(TFT_LIGHTGREY);
                    spr_track_name.drawString(active_track_name, 0, 0);
                }
                spr_track_name.pushSprite(TRKBTN_X, TRKBTN_Y + TRKBTN_H + 6);
            } else {
                tft.setFont(&fonts::Font2);
                tft.setTextSize(1);
                tft.setTextDatum(textdatum_t::top_left);
                tft.setTextPadding(TRKBTN_W + 160);
                if (active_track_name[0]) {
                    tft.setTextColor(TFT_LIGHTGREY, bg);
                    tft.drawString(active_track_name, TRKBTN_X, TRKBTN_Y + TRKBTN_H + 6);
                } else {
                    tft.fillRect(TRKBTN_X, TRKBTN_Y + TRKBTN_H + 6, TRKBTN_W + 160, 18, bg);
                }
                tft.setTextPadding(0);
            }
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
    // ENUM = cycle through string list (PROTOCOL_NAMES, etc.).
    // TEXT = read-only display today; tap will open a popup keyboard once
    //        that's implemented (next iteration).
    // INFO = read-only display row (no controls, no tap action). Used today
    // for the WiFi connection status line in the Internet block.
    enum Kind { NUMERIC, TOGGLE, COLOR, ENUM, TEXT, ACTION, INFO, SLIDER } kind;
};
static const SettingRow ROWS[ST_COUNT] = {
    { ST_BRIGHTNESS,   "Brightness",            SettingRow::SLIDER  },
    { ST_LAP_OVERLAY,  "Lap time popup (sec)",  SettingRow::NUMERIC },
    { ST_INET_MODE,    "Internet",              SettingRow::ENUM    },
    { ST_WIFI_SSID,    "WiFi network (SSID)",   SettingRow::TEXT    },
    { ST_WIFI_PASS,    "WiFi password",         SettingRow::TEXT    },
    { ST_WIFI_STATUS,  "WiFi status",           SettingRow::INFO    },
    { ST_RPM_MIN,    "Min RPM display",      SettingRow::NUMERIC },
    { ST_RPM_MAX,    "Max RPM display",      SettingRow::NUMERIC },
    { ST_RPM_DIV,    "Tach pulses/rev",      SettingRow::NUMERIC },
    { ST_RPM_SMOOTH, "RPM smoothing",        SettingRow::SLIDER  },
    { ST_RPM_SPIKE,  "RPM spike filter",     SettingRow::ENUM    },
    { ST_GPS_FILTER, "GPS drift filter",     SettingRow::ENUM    },
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
    { ST_SHOW_VOLT,    "Show voltage (engine on)", SettingRow::TOGGLE  },
    { ST_COACH_SHOW,   "Show coach checklist",     SettingRow::TOGGLE  },
    { ST_VOLT_WARN,    "Voltage low-warn (x10)",   SettingRow::NUMERIC },
    { ST_VOLT_WARN_COL,"Voltage warn color",       SettingRow::COLOR   },
    { ST_SENSOR_TYPE,  "Sensor data source",    SettingRow::ENUM    },
    { ST_SHOW_AFR,     "Show AFR (MS3 only)",   SettingRow::TOGGLE  },
    { ST_AFR_WARN_LO,  "AFR rich-warn (x10)",   SettingRow::NUMERIC },
    { ST_AFR_WARN_HI,  "AFR lean-warn (x10)",   SettingRow::NUMERIC },
    { ST_AFR_WARN_COL, "AFR warn color",        SettingRow::COLOR   },
    { ST_REC_SD,        "Record to SD card",    SettingRow::TOGGLE  },
    { ST_REC_CLOUD,     "Record to cloud",      SettingRow::TOGGLE  },
    { ST_AUTO_TRACK,    "Auto select by GPS",   SettingRow::TOGGLE  },
    { ST_DEBUG_LOG,     "Debug logging (SD)",   SettingRow::TOGGLE  },
    { ST_AUTO_START,    "Auto start recording", SettingRow::TOGGLE  },
    { ST_AUTO_START_MPH,"Auto start at (mph)",  SettingRow::NUMERIC },
    { ST_AUTO_START_SEC,"Auto start hold (sec)",SettingRow::NUMERIC },
    { ST_CL_HOST,      "Cloud host (DNS/IP)",   SettingRow::TEXT    },
    { ST_CL_PORT,      "Cloud port",            SettingRow::TEXT    },
    { ST_CL_PROTO,     "Cloud protocol",        SettingRow::ENUM    },
    // ("Cloud stream mode" Live/AfterRace row removed — live "stream to cloud"
    //  isn't ready and its code was deleted; cloud recording always uploads
    //  After Race via the /queue/ + dash-driven upload path.)
    { ST_CL_AUTH_USER, "User email",            SettingRow::TEXT    },
    { ST_CL_AUTH_PASS, "API key",               SettingRow::TEXT    },
    { ST_TIMEZONE,     "Time zone",              SettingRow::ENUM    },
    { ST_GPS_BAUD,     "GPS settings",           SettingRow::ENUM    },
    { ST_GPS_STATUS,   "GPS link",               SettingRow::INFO    },
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
constexpr int SLIDER_TRACK_X = 410, SLIDER_TRACK_W = 300;   // brightness slider groove

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
        case ST_VOLT_WARN:    return {  80, 160, 1 };  // 8.0-16.0 V, ×10
        case ST_AFR_WARN_LO:  return {  80, 200, 1 };
        case ST_AFR_WARN_HI:  return {  80, 200, 1 };
        case ST_AUTO_START_MPH: return { 5, 150, 5 };  // speed to trigger auto-record
        case ST_AUTO_START_SEC: return { 1,  30, 1 };  // seconds it must be HELD
        case ST_LAP_OVERLAY:    return { 0,   9, 1 };  // lap popup seconds (0 = off)
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
        case ST_VOLT_WARN:    return s.volt_warn_x10;
        case ST_AFR_WARN_LO:  return s.afr_warn_lo_x10;
        case ST_AFR_WARN_HI:  return s.afr_warn_hi_x10;
        case ST_AUTO_START_MPH: return s.auto_start_mph;
        case ST_AUTO_START_SEC: return s.auto_start_sec;
        case ST_LAP_OVERLAY:    return s.lap_overlay_s;
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
        case ST_VOLT_WARN:    s.volt_warn_x10 = v; break;
        case ST_AFR_WARN_LO:  s.afr_warn_lo_x10 = v; break;
        case ST_AFR_WARN_HI:  s.afr_warn_hi_x10 = v; break;
        case ST_AUTO_START_MPH: s.auto_start_mph = v; break;
        case ST_AUTO_START_SEC: s.auto_start_sec = (uint16_t)(v < 1 ? 1 : (v > 30 ? 30 : v)); break;
        case ST_LAP_OVERLAY:    s.lap_overlay_s  = (uint8_t)(v > 9 ? 9 : v); break;
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

// Snap the settings scroll to a row boundary (on drag release) so every visible
// row lands fully inside the tappable body band — otherwise a row's -/+ buttons
// can straddle the y>=BODY_BOTTOM dead zone and refuse taps at some offsets.
static void snapSettingsScroll() {
    settingsScrollY = ((settingsScrollY + SETTINGS_ROW_DY / 2) / SETTINGS_ROW_DY) * SETTINGS_ROW_DY;
    clampSettingsScroll();
}

static const char* boolValueOnRow(SettingId id) {
    switch (id) {
        case ST_ALERTS:      return s.alerts_enabled    ? "ON" : "OFF";
        case ST_REC_SD:      return s.record_sd         ? "ON" : "OFF";
        case ST_REC_CLOUD:   return s.record_cloud      ? "ON" : "OFF";
        case ST_AUTO_TRACK:  return s.auto_select_track ? "ON" : "OFF";
        case ST_DEBUG_LOG:   return s.debug_enabled ? "ON" : "OFF";
        case ST_AUTO_START:  return s.auto_start        ? "ON" : "OFF";
        case ST_SHOW_TEMP:   return s.show_coolant      ? "ON" : "OFF";
        case ST_SHOW_VOLT:   return s.show_volt         ? "ON" : "OFF";
        case ST_COACH_SHOW:  return s.coach_show        ? "ON" : "OFF";
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
        case ST_DEBUG_LOG:   return s.debug_enabled;
        case ST_AUTO_START:  return s.auto_start;
        case ST_SHOW_TEMP:   return s.show_coolant;
        case ST_SHOW_VOLT:   return s.show_volt;
        case ST_COACH_SHOW:  return s.coach_show;
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
static bool wifiConnectedNow() { return wifi_state == WS_CONNECTED; }

// ---------------------------------------------------------------------------
// WiFi-via-dash cloud forwarder (WUP).
// When the Teensy needs to upload an NDJSON session file while we're the WiFi
// owner, it streams the file over UART using WUP,* and we POST it to the
// configured cloud endpoint over HTTP/HTTPS. See the matching block in
// src/main.cpp for the protocol description. Defined in this section so it
// has direct access to wifi_state without needing extern indirection.
// ---------------------------------------------------------------------------
static constexpr size_t WUP_MAX_BYTES = 4 * 1024 * 1024;  // 4 MB PSRAM cap per session
static uint8_t*  wup_buf       = nullptr;
static size_t   wup_capacity   = 0;
static size_t   wup_written    = 0;
static uint32_t wup_expected   = 0;
static uint32_t wup_lines      = 0;
static char     wup_file[80]   = "";
static char     wup_track[40]  = "";
static uint32_t wup_session_id = 0;
static bool     wup_active     = false;
static bool     wup_cancelled  = false;

static void wupFree() {
    if (wup_buf) { free(wup_buf); wup_buf = nullptr; }
    wup_capacity = wup_written = wup_expected = wup_lines = 0;
    wup_file[0] = '\0';
    wup_track[0] = '\0';
    wup_session_id = 0;
    wup_active = false;
    wup_cancelled = false;
}

static int wupDoCloudPost(int* http_status_out, char* err_out, size_t err_sz) {
    if (!wup_buf || wup_written == 0) {
        snprintf(err_out, err_sz, "empty buffer");
        return 0;
    }
    if (s.internet_mode != 1 || wifi_state != WS_CONNECTED) {
        snprintf(err_out, err_sz, "wifi not connected");
        return 0;
    }
    if (s.cloud_host[0] == '\0' || s.cloud_port == 0) {
        snprintf(err_out, err_sz, "cloud host/port unset");
        return 0;
    }
    char url[256];
    snprintf(url, sizeof(url), "%s://%s:%u/upload",
             s.cloud_protocol == 1 ? "https" : "http",
             s.cloud_host, (unsigned)s.cloud_port);
    Serial.printf("DBG,wup_post_begin url=%s bytes=%u\n",
                  url, (unsigned)wup_written);

    WiFiClientSecure secureClient;
    WiFiClient       plainClient;
    HTTPClient http;
    bool ok;
    if (s.cloud_protocol == 1) {
        secureClient.setInsecure();   // TODO: pin the racecar API CA
        ok = http.begin(secureClient, url);
    } else {
        ok = http.begin(plainClient, url);
    }
    if (!ok) {
        snprintf(err_out, err_sz, "http.begin failed");
        return 0;
    }
    http.setTimeout(60000);
    http.addHeader("Content-Type", "application/x-ndjson");
    http.addHeader("X-API-Key",    s.cloud_auth_pass);
    http.addHeader("X-User-Email", s.cloud_auth_user);
    char sid[24]; snprintf(sid, sizeof(sid), "%lu", (unsigned long)wup_session_id);
    http.addHeader("X-Session-Id", sid);
    http.addHeader("X-Track-Name", wup_track);
    const int code = http.POST(wup_buf, wup_written);
    if (http_status_out) *http_status_out = code;
    Serial.printf("DBG,wup_post_end code=%d\n", code);
    if (code <= 0) {
        snprintf(err_out, err_sz, "http error %d", code);
    } else if (code < 200 || code >= 300) {
        // Non-2xx is a failure even though the HTTP roundtrip succeeded. Pull
        // a snippet of the server response so the dash can show why.
        String body = http.getString();
        if (body.length() > 0) {
            char snippet[48];
            snprintf(snippet, sizeof(snippet), "%s", body.c_str());
            snprintf(err_out, err_sz, "http %d: %s", code, snippet);
        } else {
            snprintf(err_out, err_sz, "http %d", code);
        }
    }
    http.end();
    return code;
}

static bool parseWupLine(const String& line) {
    if (!line.startsWith("WUP,")) return false;
    const String tail = line.substring(4);

    if (tail.startsWith("START,")) {
        // Diagnostic: report receipt + dash-side state. Routed to Teensy USB
        // via our DBG relay so the developer can see exactly what's happening
        // when an upload kicks off.
        Serial.printf("DBG,wup_recv tail_len=%u inet=%u wifi=%u rxBufLen=%u\n",
                      (unsigned)tail.length(),
                      (unsigned)s.internet_mode,
                      (unsigned)wifi_state,
                      (unsigned)rxBuf.length());
        wupFree();
        const String s2 = tail.substring(6);
        int p1 = s2.indexOf(',');
        int p2 = (p1 >= 0) ? s2.indexOf(',', p1 + 1) : -1;
        int p3 = (p2 >= 0) ? s2.indexOf(',', p2 + 1) : -1;
        if (p1 < 0 || p2 < 0 || p3 < 0) {
            Serial.println("WUP,NACK,bad_start");
            Serial.flush();
            return true;
        }
        const String fn   = s2.substring(0, p1);
        const uint32_t sz = (uint32_t)s2.substring(p1 + 1, p2).toInt();
        const uint32_t sid= (uint32_t)s2.substring(p2 + 1, p3).toInt();
        const String trk  = s2.substring(p3 + 1);
        if (s.internet_mode != 1) {
            Serial.println("WUP,NACK,not_wifi_mode"); Serial.flush(); return true;
        }
        if (wifi_state != WS_CONNECTED) {
            Serial.println("WUP,NACK,no_wifi"); Serial.flush(); return true;
        }
        if (sz == 0 || sz > WUP_MAX_BYTES) {
            Serial.printf("WUP,NACK,size %lu\n", (unsigned long)sz); Serial.flush();
            return true;
        }
        wup_buf = (uint8_t*)ps_malloc((size_t)sz + 32 * 1024);
        if (!wup_buf) {
            Serial.println("WUP,NACK,no_psram"); Serial.flush(); return true;
        }
        wup_capacity   = (size_t)sz + 32 * 1024;
        wup_written    = 0;
        wup_expected   = sz;
        wup_lines      = 0;
        fn .toCharArray(wup_file,  sizeof(wup_file));
        trk.toCharArray(wup_track, sizeof(wup_track));
        wup_session_id = sid;
        wup_active     = true;
        wup_cancelled  = false;
        Serial.println("WUP,READY");
        Serial.flush();
        return true;
    }
    if (tail.startsWith("L,")) {
        if (!wup_active || !wup_buf) return true;
        const char* data = line.c_str() + 4 + 2;   // skip "WUP,L,"
        const size_t n   = strlen(data);
        if (wup_written + n + 1 > wup_capacity) {
            wupFree();
            Serial.println("WUP,NACK,overflow");
            Serial.flush();
            return true;
        }
        memcpy(wup_buf + wup_written, data, n);
        wup_written += n;
        wup_buf[wup_written++] = '\n';
        wup_lines++;
        Serial.println("U");
        Serial.flush();
        return true;
    }
    if (tail == "CANCEL") {
        wup_cancelled = true;
        wupFree();
        return true;
    }
    if (tail.startsWith("END")) {
        if (!wup_active || !wup_buf) {
            Serial.println("WUP,RESULT,FAIL,not_active"); Serial.flush();
            wupFree();
            return true;
        }
        const int p1 = tail.indexOf(',');
        const int p2 = (p1 >= 0) ? tail.indexOf(',', p1 + 1) : -1;
        const uint32_t teensy_bytes = (p2 >= 0)
            ? (uint32_t)tail.substring(p2 + 1).toInt()
            : 0;
        if (teensy_bytes != 0 && teensy_bytes != wup_expected) {
            Serial.printf("WUP,RESULT,FAIL,size_drift teensy=%lu dash=%u\n",
                          (unsigned long)teensy_bytes, (unsigned)wup_written);
            Serial.flush();
            wupFree();
            return true;
        }
        char err[64] = "";
        int  http_status = 0;
        const int code = wupDoCloudPost(&http_status, err, sizeof(err));
        if (code >= 200 && code < 300) {
            Serial.printf("WUP,RESULT,OK,%d\n", code);
        } else {
            Serial.printf("WUP,RESULT,FAIL,%s\n", err[0] ? err : "http_fail");
        }
        Serial.flush();
        wupFree();
        return true;
    }
    return false;
}
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
    // Never transmit on UART while a Teensy OTA is in flight. The Teensy is
    // either receiving HEX lines (each line ACKed via UART) or inside
    // flash_move() committing the new image. A stray SETTIME would jam the
    // protocol or arrive mid-flash-rewrite.
    if (currentPage == PAGE_OTA &&
        (ota_state == OTA_S_TEENSY_DOWNLOADING ||
         ota_state == OTA_S_TEENSY_WAITING)) return;
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
    if (millis() - last_tick_ms < 1000) { if (net_owner == NET_WIFI) wifiTickNtp(); return; }
    last_tick_ms = millis();

    // Radio time-share: while Bluetooth owns (or is releasing) the radio, WiFi
    // stays hard-off — running both crashes the core (see arbiter comment).
    if (net_owner != NET_WIFI) {
        if (wifi_state != WS_OFF) {
            WiFi.disconnect(true, true);
            WiFi.mode(WIFI_OFF);
            wifi_ip[0]    = '\0';
            wifi_ntp_done = false;
            setWifiState(WS_OFF);
        }
        return;
    }

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
            // Disable modem power-save. Default is WIFI_PS_MIN_MODEM, where the
            // radio sleeps between AP beacons — that stretches DHCP
            // DISCOVER/OFFER/REQUEST round-trips out to many seconds (the slow
            // "took a while to get an IP" symptom), and adds latency to every
            // later cloud upload packet. We're wall-powered in the car, so
            // there's no reason to power-save the WiFi radio.
            WiFi.setSleep(false);
            WiFi.setAutoReconnect(true);
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

// --- radio time-share arbiter (see the NET_* comment block up top) ----------
// Take the radio for BLE. Synchronous WiFi hard-off FIRST — the crash is the
// BT controller enabling while WiFi runs, so sequencing is everything (the obd
// task also waits 150 ms before NimBLEDevice::init as margin).
static void btAcquireRadio() {
    if (net_owner == NET_BT) return;
    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);   // esp_wifi_stop is synchronous — radio is off on return
    wifi_ip[0]    = '\0';
    wifi_ntp_done = false;
    setWifiState(WS_OFF);
    net_owner    = NET_BT;
    net_owner_ms = millis();
    Serial.println("[net] radio -> BT (WiFi off)");
}
// Give the radio back to WiFi. Async: BLE must fully deinit on the obd task
// first; netOwnerTick() flips to NET_WIFI when obd::isDown() (or 12 s failsafe
// — covers a scan in flight (<=8 s) and the parked low-heap task, which never
// enabled the controller and is safe to run WiFi over).
static void btReleaseRadio() {
    if (net_owner == NET_WIFI) return;
    obd::requestShutdown();
    net_owner    = NET_TO_WIFI;
    net_owner_ms = millis();
    Serial.println("[net] radio -> WiFi (BLE draining)");
}
static void netOwnerTick() {
    // Recording-edge watcher (single choke point — catches the START button,
    // auto-start, Teensy-side stops, everything that flips `recording`).
    static bool prev_rec = false;
    if (recording != prev_rec) {
        prev_rec = recording;
        if (recording) {
            if (s.sensor_type == 2 && s.bt_addr[0] && !obd::blocked() && !upload_active) {
                btAcquireRadio();
                obd::begin();
                obd::connectTo(s.bt_addr, s.bt_atype, s.bt_name);
            }
        } else {
            // Session over — give WiFi the radio back (uploads/OTA), unless
            // the user is mid-pairing on the sensor/scan pages.
            if (currentPage != PAGE_SENSOR && currentPage != PAGE_BT_SCAN &&
                currentPage != PAGE_PID_SCAN)
                btReleaseRadio();
        }
    }
    if (net_owner == NET_TO_WIFI) {
        if (obd::isDown() || millis() - net_owner_ms > 12000) {
            net_owner    = NET_WIFI;
            net_owner_ms = millis();
            wifiForceReconfigure();   // wifiTick brings the link up within ~1 s
            Serial.println("[net] radio -> WiFi (BLE down, connecting)");
        }
        return;
    }
    if (net_owner == NET_WIFI && net_pending_upload) {
        if (wifiConnectedNow()) {
            net_pending_upload = false;
            if (net_pending_sel) { net_pending_sel = false; ufStartSelected(); }
            else                 { ufStartListing(); }   // modal is already open
        } else if (millis() - net_owner_ms > 45000) {
            // WiFi never came up — abort the pending upload; closeUploadModal's
            // hook hands the radio back to BT if that's still the source.
            net_pending_upload = false;
            net_pending_sel    = false;
            snprintf(upload_result_msg, sizeof(upload_result_msg), "WiFi didn't connect");
            closeUploadModal();
        }
    }
}

// ---------------------------------------------------------------------------
// PAGE_GPS — GPS Settings: pick UART baud + nav rate (Hz) with LIVE feedback,
// then DONE (save) or CANCEL (revert baud/Hz + the module to what they were on
// entry). Tapping a value applies it immediately (CFG,gpsbaud / CFG,gpshz) so
// you can see if data comes back; Cancel undoes it. Snapshot for revert is
// taken in openGpsPage().
// ---------------------------------------------------------------------------
namespace {
  constexpr int GPS_BTN_W = 140, GPS_BTN_H = 46, GPS_BTN_X0 = 26, GPS_BTN_DX = 152;
  constexpr int GPS_BAUD_Y = 198, GPS_HZ_Y = 300;
  constexpr int GPS_CANCEL_X = 60,  GPS_DONE_X = 440, GPS_FOOT_Y = 410, GPS_FOOT_W = 300, GPS_FOOT_H = 54;
}

static void openGpsPage() {
    gps_orig_baud = s.gps_baud;
    gps_orig_hz   = s.gps_nav_hz;
    currentPage   = PAGE_GPS;
    pageJustEntered = true;
    gps_page_dirty  = true;
}

static void drawGpsSelBtn(int x, int y, const char* label, bool sel) {
    const uint16_t fill = sel ? TFT_DARKGREEN : TFT_NAVY;
    tft.fillRect(x, y, GPS_BTN_W, GPS_BTN_H, fill);
    tft.drawRect(x, y, GPS_BTN_W, GPS_BTN_H, sel ? TFT_GREEN : TFT_DARKGREY);
    tft.setFont(&fonts::Font2); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, fill);
    tft.setTextPadding(GPS_BTN_W - 6);
    tft.drawString(label, x + GPS_BTN_W / 2, y + GPS_BTN_H / 2);
    tft.setTextPadding(0);
    tft.setTextDatum(textdatum_t::top_left);
}

static void drawGpsPage() {
    const uint16_t BG = TFT_BLACK;
    if (pageJustEntered) { tft.fillScreen(BG); pageJustEntered = false; }
    gps_page_dirty = false;

    // Header
    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::top_left);
    tft.setTextColor(TFT_CYAN, BG); tft.setTextPadding(500);
    tft.drawString("GPS SETTINGS", 20, 12);
    tft.setTextPadding(0);

    // Live status
    tft.setFont(&fonts::Font2); tft.setTextSize(1);
    tft.setTextPadding(760);
    char buf[96];
    const bool live = gps_diag_libok && gps_diag_age_ms < 1500;
    snprintf(buf, sizeof(buf), "Link: %lu baud @ %u Hz   %s",
             (unsigned long)gps_diag_baud, (unsigned)gps_diag_hz,
             live ? "LIVE" : (gps_diag_libok ? "STALE" : "NO DATA"));
    tft.setTextColor(live ? TFT_GREEN : TFT_RED, BG);
    tft.drawString(buf, 20, 58);
    tft.setTextColor(TFT_WHITE, BG);
    snprintf(buf, sizeof(buf), "Fix: %d (%s)    Sats: %d",
             (int)g.fix, fixName(g.fix), (int)g.sats);
    tft.drawString(buf, 20, 84);
    snprintf(buf, sizeof(buf), "PVT age: %lu ms    resync: %lu    re-begin: %lu",
             (unsigned long)gps_diag_age_ms, (unsigned long)gps_diag_recover,
             (unsigned long)gps_diag_reinit);
    tft.drawString(buf, 20, 110);
    tft.setTextPadding(0);

    // UART baud
    tft.setTextColor(TFT_LIGHTGREY, BG);
    tft.drawString("UART BAUD", 26, 174);
    for (int i = 0; i < N_GPS_BAUD; i++)
        drawGpsSelBtn(GPS_BTN_X0 + i * GPS_BTN_DX, GPS_BAUD_Y, GPS_BAUD_NAMES[i],
                      s.gps_baud == GPS_BAUD_VALS[i]);

    // Nav rate
    tft.setTextColor(TFT_LIGHTGREY, BG);
    tft.drawString("NAV RATE (Hz)", 26, 276);
    for (int i = 0; i < N_GPS_HZ; i++)
        drawGpsSelBtn(GPS_BTN_X0 + i * GPS_BTN_DX, GPS_HZ_Y, GPS_HZ_NAMES[i],
                      s.gps_nav_hz == GPS_HZ_VALS[i]);

    // Hint
    tft.setTextColor(TFT_DARKGREY, BG);
    tft.drawString("Tap a value to try it live. DONE saves · CANCEL reverts.", 26, 362);

    // Footer: CANCEL (revert) / DONE (save)
    tft.fillRect(GPS_CANCEL_X, GPS_FOOT_Y, GPS_FOOT_W, GPS_FOOT_H, TFT_MAROON);
    tft.drawRect(GPS_CANCEL_X, GPS_FOOT_Y, GPS_FOOT_W, GPS_FOOT_H, TFT_WHITE);
    tft.fillRect(GPS_DONE_X,   GPS_FOOT_Y, GPS_FOOT_W, GPS_FOOT_H, TFT_DARKGREEN);
    tft.drawRect(GPS_DONE_X,   GPS_FOOT_Y, GPS_FOOT_W, GPS_FOOT_H, TFT_WHITE);
    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, TFT_MAROON);
    tft.drawString("CANCEL", GPS_CANCEL_X + GPS_FOOT_W / 2, GPS_FOOT_Y + GPS_FOOT_H / 2);
    tft.setTextColor(TFT_WHITE, TFT_DARKGREEN);
    tft.drawString("DONE", GPS_DONE_X + GPS_FOOT_W / 2, GPS_FOOT_Y + GPS_FOOT_H / 2);
    tft.setTextDatum(textdatum_t::top_left);
}

static void handleGpsPageTap(int x, int y) {
    // Baud row
    if (y >= GPS_BAUD_Y && y <= GPS_BAUD_Y + GPS_BTN_H) {
        for (int i = 0; i < N_GPS_BAUD; i++) {
            const int bx = GPS_BTN_X0 + i * GPS_BTN_DX;
            if (x >= bx && x <= bx + GPS_BTN_W) {
                if (s.gps_baud != GPS_BAUD_VALS[i]) {
                    s.gps_baud = GPS_BAUD_VALS[i];
                    Serial.printf("CFG,gpsbaud,%lu\n", (unsigned long)s.gps_baud);
                }
                gps_page_dirty = true; return;
            }
        }
    }
    // Hz row
    if (y >= GPS_HZ_Y && y <= GPS_HZ_Y + GPS_BTN_H) {
        for (int i = 0; i < N_GPS_HZ; i++) {
            const int bx = GPS_BTN_X0 + i * GPS_BTN_DX;
            if (x >= bx && x <= bx + GPS_BTN_W) {
                if (s.gps_nav_hz != GPS_HZ_VALS[i]) {
                    s.gps_nav_hz = GPS_HZ_VALS[i];
                    Serial.printf("CFG,gpshz,%u\n", (unsigned)s.gps_nav_hz);
                }
                gps_page_dirty = true; return;
            }
        }
    }
    // Footer
    if (y >= GPS_FOOT_Y && y <= GPS_FOOT_Y + GPS_FOOT_H) {
        if (x >= GPS_CANCEL_X && x <= GPS_CANCEL_X + GPS_FOOT_W) {
            // Revert: restore baud/Hz and re-apply the ORIGINAL to the module.
            if (s.gps_baud != gps_orig_baud)
                Serial.printf("CFG,gpsbaud,%lu\n", (unsigned long)gps_orig_baud);
            if (s.gps_nav_hz != gps_orig_hz)
                Serial.printf("CFG,gpshz,%u\n", (unsigned)gps_orig_hz);
            s.gps_baud   = gps_orig_baud;
            s.gps_nav_hz = gps_orig_hz;
            currentPage = PAGE_SETTINGS; pageJustEntered = true; settingsDirty = true;
            return;
        }
        if (x >= GPS_DONE_X && x <= GPS_DONE_X + GPS_FOOT_W) {
            saveSettings();   // persist the current baud/Hz
            currentPage = PAGE_SETTINGS; pageJustEntered = true; settingsDirty = true;
            return;
        }
    }
}

// ---------------------------------------------------------------------------
// PAGE_SENSOR — Sensor Source picker (Direct / MegaSquirt / Bluetooth), modeled
// on the GPS page. For Bluetooth it also shows the paired OBD-II dongle + its
// live connection status and a SCAN button. DONE saves, CANCEL reverts.
// ---------------------------------------------------------------------------
namespace {
  constexpr int SS_BTN_W = 232, SS_BTN_H = 56, SS_BTN_X0 = 30, SS_BTN_DX = 246, SS_BTN_Y = 96;
  constexpr int SS_SCAN_X = 30,  SS_SCAN_Y = 300, SS_SCAN_W = 300, SS_SCAN_H = 54;
  constexpr int SS_CANCEL_X = 60, SS_DONE_X = 440, SS_FOOT_Y = 410, SS_FOOT_W = 300, SS_FOOT_H = 54;
}

// Apply a sensor-source choice live: bring up / tear down the BLE OBD client.
static void applySensorSource(uint8_t t) {
    s.sensor_type = t % N_SENSOR_TYPE;
    if (s.sensor_type == 2) {                 // Bluetooth OBD-II
        obd::setBlocked(false);   // explicit user action -> allow the (re)try even after a prior crash
        btAcquireRadio();         // WiFi hard-off BEFORE any BLE init (coex crash)
        obd::begin();
        if (s.bt_addr[0]) obd::connectTo(s.bt_addr, s.bt_atype, s.bt_name);
    } else {
        btReleaseRadio();         // full BLE shutdown -> arbiter restores WiFi
    }
}

static void openSensorPage() {
    sensor_orig_type  = s.sensor_type;
    currentPage       = PAGE_SENSOR;
    pageJustEntered   = true;
    sensor_page_dirty = true;
}

static void drawSensorPage() {
    const uint16_t BG = TFT_BLACK;
    if (pageJustEntered) { tft.fillScreen(BG); pageJustEntered = false; }
    sensor_page_dirty = false;

    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::top_left);
    tft.setTextColor(TFT_CYAN, BG); tft.setTextPadding(500);
    tft.drawString("SENSOR SOURCE", 20, 12);
    tft.setTextPadding(0);

    tft.setFont(&fonts::Font2); tft.setTextSize(1);
    tft.setTextColor(TFT_LIGHTGREY, BG);
    tft.drawString("RPM always comes from the tach/CAN via the Teensy.", 30, 64);
    // Show a prior BLE-init crash reason ALWAYS (source reverts off BT after a
    // crash, so this can't live in the BT-only block or it'd never be seen).
    tft.setTextPadding(760);
    if (ble_diag[0]) { tft.setTextColor(TFT_RED, BG); tft.drawString(ble_diag, 30, 82); }
    else             { tft.setTextColor(BG, BG);      tft.drawString(" ", 30, 82); }
    tft.setTextPadding(0);

    // Three source buttons (one row)
    for (int i = 0; i < N_SENSOR_TYPE; i++)
        drawGpsSelBtn(SS_BTN_X0 + i * SS_BTN_DX, SS_BTN_Y, SENSOR_TYPE_NAMES[i], s.sensor_type == i);

    // Bluetooth detail block (only when BT is the chosen source)
    const int by = 250;
    tft.setFont(&fonts::Font2); tft.setTextSize(1);
    tft.setTextPadding(740);
    if (s.sensor_type == 2) {
        char buf[128];
        tft.setTextColor(TFT_WHITE, BG);
        snprintf(buf, sizeof(buf), "Device: %s",
                 s.bt_name[0] ? s.bt_name : (s.bt_addr[0] ? s.bt_addr : "(none selected)"));
        tft.drawString(buf, 30, by);

        const bool conn = obd::connected();
        char cbuf[96] = {0};
        int  cn = 0;
        if (obd::coolantF_x10() >= 0)
            cn += snprintf(cbuf + cn, sizeof(cbuf) - cn, "   coolant %d F",
                           (int)((obd::coolantF_x10() + 5) / 10));
        if (obd::iatF_x10() >= 0 && cn < (int)sizeof(cbuf))
            cn += snprintf(cbuf + cn, sizeof(cbuf) - cn, "   IAT %d F",
                           (int)((obd::iatF_x10() + 5) / 10));
        if (obd::voltX10() > 0 && cn < (int)sizeof(cbuf))
            cn += snprintf(cbuf + cn, sizeof(cbuf) - cn, "   %d.%dV",
                           obd::voltX10() / 10, obd::voltX10() % 10);
        if (obd::tpsX10() >= 0 && cn < (int)sizeof(cbuf))
            cn += snprintf(cbuf + cn, sizeof(cbuf) - cn, "   TPS %d%%",
                           (int)((obd::tpsX10() + 5) / 10));
        if (obd::sparkX10() > -1000 && cn < (int)sizeof(cbuf))
            cn += snprintf(cbuf + cn, sizeof(cbuf) - cn, "   SPK %.1f",
                           obd::sparkX10() / 10.0f);
        snprintf(buf, sizeof(buf), "Status: %s%s", obd::stateStr(), cbuf);
        tft.setTextColor(conn ? TFT_GREEN : TFT_YELLOW, BG);
        tft.drawString(buf, 30, by + 26);
        if (obd::lastErr()[0]) {   // e.g. "BT skipped: low heap (..KB free)"
            tft.setTextColor(TFT_YELLOW, BG);
            tft.drawString(obd::lastErr(), 30, by + 52);
        } else if (conn && obd::coolantF_x10() < 0 && obd::lastResp()[0]) {
            // Dongle linked but the ECU isn't answering the coolant PID — show
            // the raw ELM reply (NODATA / UNABLETOCONNECT / SEARCHING...).
            char ebuf[64];
            snprintf(ebuf, sizeof(ebuf), "ECU reply: %s  (ignition on? OBD wired?)",
                     obd::lastResp());
            tft.setTextColor(TFT_YELLOW, BG);
            tft.drawString(ebuf, 30, by + 52);
        }

        // SCAN button
        tft.fillRect(SS_SCAN_X, SS_SCAN_Y, SS_SCAN_W, SS_SCAN_H, TFT_NAVY);
        tft.drawRect(SS_SCAN_X, SS_SCAN_Y, SS_SCAN_W, SS_SCAN_H, TFT_WHITE);
        tft.setFont(&fonts::Font2); tft.setTextSize(1);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.drawString("SCAN FOR DEVICES", SS_SCAN_X + SS_SCAN_W / 2, SS_SCAN_Y + SS_SCAN_H / 2);
        // COOLANT PID mapper button (needs a connected dongle to actually scan)
        tft.fillRect(SS_SCAN_X + 330, SS_SCAN_Y, SS_SCAN_W, SS_SCAN_H, TFT_NAVY);
        tft.drawRect(SS_SCAN_X + 330, SS_SCAN_Y, SS_SCAN_W, SS_SCAN_H, TFT_WHITE);
        char pl[28]; snprintf(pl, sizeof(pl), "COOLANT PID: %02X", s.bt_pid_clt);
        tft.drawString(pl, SS_SCAN_X + 330 + SS_SCAN_W / 2, SS_SCAN_Y + SS_SCAN_H / 2);
        tft.setTextDatum(textdatum_t::top_left);
    }
    tft.setTextPadding(0);

    // Footer: CANCEL / DONE
    tft.fillRect(SS_CANCEL_X, SS_FOOT_Y, SS_FOOT_W, SS_FOOT_H, TFT_MAROON);
    tft.drawRect(SS_CANCEL_X, SS_FOOT_Y, SS_FOOT_W, SS_FOOT_H, TFT_WHITE);
    tft.fillRect(SS_DONE_X,   SS_FOOT_Y, SS_FOOT_W, SS_FOOT_H, TFT_DARKGREEN);
    tft.drawRect(SS_DONE_X,   SS_FOOT_Y, SS_FOOT_W, SS_FOOT_H, TFT_WHITE);
    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, TFT_MAROON);
    tft.drawString("CANCEL", SS_CANCEL_X + SS_FOOT_W / 2, SS_FOOT_Y + SS_FOOT_H / 2);
    tft.setTextColor(TFT_WHITE, TFT_DARKGREEN);
    tft.drawString("DONE", SS_DONE_X + SS_FOOT_W / 2, SS_FOOT_Y + SS_FOOT_H / 2);
    tft.setTextDatum(textdatum_t::top_left);
}

static void handleSensorPageTap(int x, int y) {
    // Source buttons
    if (y >= SS_BTN_Y && y <= SS_BTN_Y + SS_BTN_H) {
        for (int i = 0; i < N_SENSOR_TYPE; i++) {
            const int bx = SS_BTN_X0 + i * SS_BTN_DX;
            if (x >= bx && x <= bx + SS_BTN_W) {
                if (s.sensor_type != i) applySensorSource((uint8_t)i);
                sensor_page_dirty = true; return;
            }
        }
    }
    // SCAN (BT mode only)
    if (s.sensor_type == 2 && y >= SS_SCAN_Y && y <= SS_SCAN_Y + SS_SCAN_H &&
        x >= SS_SCAN_X && x <= SS_SCAN_X + SS_SCAN_W) { openBtScan(); return; }
    // COOLANT PID mapper (BT mode only)
    if (s.sensor_type == 2 && y >= SS_SCAN_Y && y <= SS_SCAN_Y + SS_SCAN_H &&
        x >= SS_SCAN_X + 330 && x <= SS_SCAN_X + 330 + SS_SCAN_W) { openPidScan(); return; }
    // Footer
    if (y >= SS_FOOT_Y && y <= SS_FOOT_Y + SS_FOOT_H) {
        if (x >= SS_CANCEL_X && x <= SS_CANCEL_X + SS_FOOT_W) {
            if (s.sensor_type != sensor_orig_type) applySensorSource(sensor_orig_type);
            if (!recording) btReleaseRadio();   // leaving pairing UI: WiFi gets the radio back
            currentPage = PAGE_SETTINGS; pageJustEntered = true; settingsDirty = true; return;
        }
        if (x >= SS_DONE_X && x <= SS_DONE_X + SS_FOOT_W) {
            saveSettings();   // persists sensor_type + bt_* and re-syncs CFG,srctyp to Teensy
            if (!recording) btReleaseRadio();   // leaving pairing UI: WiFi gets the radio back
            currentPage = PAGE_SETTINGS; pageJustEntered = true; settingsDirty = true; return;
        }
    }
}

// ---------------------------------------------------------------------------
// PAGE_BT_SCAN — scan for BLE OBD-II dongles, tap one to pair it.
// ---------------------------------------------------------------------------
// (BT_* layout constants + bt_scan_scroll live near bt_scan_dirty, up top,
//  so handleTouch() can drag-scroll this page.)

static void openBtScan() {
    // Explicit user action: clear a prior-crash block too, else after a BLE
    // crash the SCAN button silently did nothing (begin() no-ops when blocked).
    obd::setBlocked(false);
    btAcquireRadio();   // WiFi hard-off BEFORE any BLE init (coex crash)
    obd::begin();
    obd::startScan();
    currentPage     = PAGE_BT_SCAN;
    pageJustEntered = true;
    bt_scan_dirty   = true;
    bt_scan_scroll  = 0;
}

static void selectBtDevice(int i) {
    const obd::ScanItem* it = obd::scanItem(i);
    if (!it) return;
    strncpy(s.bt_addr, it->addr, sizeof(s.bt_addr) - 1); s.bt_addr[sizeof(s.bt_addr) - 1] = 0;
    s.bt_atype = it->atype;
    strncpy(s.bt_name, it->name, sizeof(s.bt_name) - 1); s.bt_name[sizeof(s.bt_name) - 1] = 0;
    s.sensor_type = 2;
    saveSettings();
    obd::connectTo(s.bt_addr, s.bt_atype, s.bt_name);
    currentPage = PAGE_SENSOR; pageJustEntered = true; sensor_page_dirty = true;
}

static void drawBtScanPage() {
    const uint16_t BG = TFT_BLACK;
    if (pageJustEntered) { tft.fillScreen(BG); pageJustEntered = false; }
    bt_scan_dirty = false;

    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::top_left);
    tft.setTextColor(TFT_CYAN, BG); tft.setTextPadding(600);
    tft.drawString("BLUETOOTH DEVICES", 20, 12);

    tft.setFont(&fonts::Font2); tft.setTextSize(1);
    tft.setTextColor(obd::scanning() ? TFT_YELLOW : TFT_LIGHTGREY, BG);
    {
        const int n0 = obd::scanCount();
        char sub[64];
        if      (obd::scanning()) strncpy(sub, "Scanning...", sizeof(sub));
        else if (!n0)             strncpy(sub, "No devices found. RESCAN to retry.", sizeof(sub));
        else if (n0 * BT_ROW_H > BT_VIEW_H)
            snprintf(sub, sizeof(sub), "%d devices - drag to scroll, tap to pair:", n0);
        else
            snprintf(sub, sizeof(sub), "Tap your OBD dongle to pair:");
        tft.drawString(sub, 20, 60);
    }
    tft.setTextPadding(0);

    // Scrollable device list. Clip to the body band so partially-visible rows
    // at the edges can't spill into the header/footer; every pixel of the band
    // is painted exactly once per frame (rows + gap strips + tail) — no
    // wipe-then-draw flash.
    const int n = obd::scanCount();
    clampBtScanScroll();
    tft.setClipRect(20, BT_ROW_Y0, 760, BT_VIEW_H);
    int lastBottom = BT_ROW_Y0;
    for (int i = 0; i < n; i++) {
        const int ry = BT_ROW_Y0 + i * BT_ROW_H - bt_scan_scroll;
        if (ry + BT_ROW_H <= BT_ROW_Y0 || ry >= BT_ROW_Y0 + BT_VIEW_H) continue;
        tft.fillRect(20, ry, 760, BT_ROW_H - 6, TFT_NAVY);
        tft.fillRect(20, ry + BT_ROW_H - 6, 760, 6, BG);   // gap strip below row
        const obd::ScanItem* it = obd::scanItem(i);
        if (!it) continue;
        tft.drawRect(20, ry, 760, BT_ROW_H - 6, TFT_DARKGREY);
        tft.setFont(&fonts::Font2); tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.drawString(it->name, 32, ry + 4);
        char buf[64];
        snprintf(buf, sizeof(buf), "%s   %d dBm", it->addr, (int)it->rssi);
        tft.setTextColor(TFT_LIGHTGREY, TFT_NAVY);
        tft.drawString(buf, 32, ry + 24);
        if (ry + BT_ROW_H > lastBottom) lastBottom = ry + BT_ROW_H;
    }
    // Blank whatever the rows didn't cover (short lists, scrolled past end).
    if (lastBottom < BT_ROW_Y0 + BT_VIEW_H)
        tft.fillRect(20, lastBottom, 760, BT_ROW_Y0 + BT_VIEW_H - lastBottom, BG);
    tft.clearClipRect();

    // Footer: RESCAN / BACK
    tft.fillRect(BT_RESCAN_X, BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_NAVY);
    tft.drawRect(BT_RESCAN_X, BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_WHITE);
    tft.fillRect(BT_BACK_X,   BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_DARKGREEN);
    tft.drawRect(BT_BACK_X,   BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_WHITE);
    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, TFT_NAVY);
    tft.drawString("RESCAN", BT_RESCAN_X + BT_FOOT_W / 2, BT_FOOT_Y + BT_FOOT_H / 2);
    tft.setTextColor(TFT_WHITE, TFT_DARKGREEN);
    tft.drawString("BACK", BT_BACK_X + BT_FOOT_W / 2, BT_FOOT_Y + BT_FOOT_H / 2);
    tft.setTextDatum(textdatum_t::top_left);
}

// ---------------------------------------------------------------------------
// PAGE_PID_SCAN — enumerate the car's supported Mode-01 PIDs with a live
// sample of each, and tap one to map it to the COOLANT function (NVS btpid).
// Decode assumes the 1-byte temperature formula A-40 degC (PIDs 05/0F/46/5C).
// ---------------------------------------------------------------------------
static const char* pidName(uint8_t p) {
    switch (p) {
        case 0x04: return "Engine load";
        case 0x05: return "Coolant temp (std)";
        case 0x06: return "ST fuel trim 1";
        case 0x07: return "LT fuel trim 1";
        case 0x08: return "ST fuel trim 2";
        case 0x09: return "LT fuel trim 2";
        case 0x0A: return "Fuel pressure";
        case 0x0B: return "MAP";
        case 0x0C: return "RPM";
        case 0x0D: return "Vehicle speed";
        case 0x0E: return "Timing advance";
        case 0x0F: return "Intake air temp";
        case 0x10: return "MAF";
        case 0x11: return "Throttle";
        case 0x13: return "O2 sensors";
        case 0x14: return "O2 B1S1";
        case 0x15: return "O2 B1S2";
        case 0x1C: return "OBD standard";
        case 0x1F: return "Run time";
        case 0x21: return "Dist w/ MIL";
        case 0x2F: return "Fuel level";
        case 0x33: return "Baro pressure";
        case 0x42: return "Module voltage";
        case 0x46: return "Ambient temp";
        case 0x5C: return "Oil temp";
        default:   return "";
    }
}

static void openPidScan() {
    currentPage     = PAGE_PID_SCAN;
    pageJustEntered = true;
    pid_scan_dirty  = true;
    pid_scan_scroll = 0;
    if (obd::connected() && obd::pidCount() == 0) obd::startPidScan();
}

static void drawPidScanPage() {
    const uint16_t BG = TFT_BLACK;
    if (pageJustEntered) { tft.fillScreen(BG); pageJustEntered = false; }
    pid_scan_dirty = false;

    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::top_left);
    tft.setTextColor(TFT_CYAN, BG); tft.setTextPadding(600);
    tft.drawString("OBD-II PID SCAN", 20, 12);

    tft.setFont(&fonts::Font2); tft.setTextSize(1);
    {
        const int n0 = obd::pidCount();
        char sub[72];
        uint16_t scol = TFT_LIGHTGREY;
        if (!obd::connected()) {
            strncpy(sub, "Not connected - pair + connect a dongle first.", sizeof(sub));
            scol = TFT_YELLOW;
        } else if (obd::pidScanning()) {
            snprintf(sub, sizeof(sub), "Scanning... %d PIDs so far (takes ~30 s)", n0);
            scol = TFT_YELLOW;
        } else if (!n0) {
            strncpy(sub, "No PIDs answered. RESCAN with ignition ON.", sizeof(sub));
        } else {
            snprintf(sub, sizeof(sub), "%d PIDs - tap the one to use as COOLANT:", n0);
        }
        sub[sizeof(sub) - 1] = 0;
        tft.setTextColor(scol, BG); tft.setTextPadding(720);
        tft.drawString(sub, 20, 60);
        tft.setTextPadding(0);
    }

    const int n = obd::pidCount();
    clampPidScanScroll();
    tft.setClipRect(20, PS_ROW_Y0, 760, PS_VIEW_H);
    int lastBottom = PS_ROW_Y0;
    for (int i = 0; i < n; i++) {
        const int ry = PS_ROW_Y0 + i * PS_ROW_H - pid_scan_scroll;
        if (ry + PS_ROW_H <= PS_ROW_Y0 || ry >= PS_ROW_Y0 + PS_VIEW_H) continue;
        const obd::PidItem* it = obd::pidItem((uint8_t)i);
        if (!it) continue;
        const bool mapped = (it->pid == s.bt_pid_clt);
        const uint16_t rowBg = mapped ? TFT_DARKGREEN : TFT_NAVY;
        tft.fillRect(20, ry, 760, PS_ROW_H - 4, rowBg);
        tft.fillRect(20, ry + PS_ROW_H - 4, 760, 4, BG);   // gap strip
        tft.drawRect(20, ry, 760, PS_ROW_H - 4, mapped ? TFT_GREEN : TFT_DARKGREY);
        tft.setFont(&fonts::Font2); tft.setTextSize(1);
        char lbuf[48];
        const char* nm = pidName(it->pid);
        snprintf(lbuf, sizeof(lbuf), "01 %02X  %s%s", it->pid, nm[0] ? nm : "(unknown)",
                 mapped ? "  << COOLANT" : "");
        tft.setTextColor(TFT_WHITE, rowBg);
        tft.drawString(lbuf, 32, ry + 10);
        char vbuf[48];
        if (it->a >= 0) {
            const int degF = (int)lroundf((it->a - 40) * 9.0f / 5.0f + 32.0f);
            snprintf(vbuf, sizeof(vbuf), "A=%-3d (%dF)  [%s]", (int)it->a, degF, it->raw);
            tft.setTextColor(TFT_LIGHTGREY, rowBg);
        } else {
            strncpy(vbuf, "no answer", sizeof(vbuf));
            tft.setTextColor(TFT_DARKGREY, rowBg);
        }
        tft.setTextDatum(textdatum_t::top_right);
        tft.drawString(vbuf, 768, ry + 10);
        tft.setTextDatum(textdatum_t::top_left);
        if (ry + PS_ROW_H > lastBottom) lastBottom = ry + PS_ROW_H;
    }
    if (lastBottom < PS_ROW_Y0 + PS_VIEW_H)
        tft.fillRect(20, lastBottom, 760, PS_ROW_Y0 + PS_VIEW_H - lastBottom, BG);
    tft.clearClipRect();

    // Footer: RESCAN / BACK (shares the BT footer geometry)
    tft.fillRect(BT_RESCAN_X, BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_NAVY);
    tft.drawRect(BT_RESCAN_X, BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_WHITE);
    tft.fillRect(BT_BACK_X,   BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_DARKGREEN);
    tft.drawRect(BT_BACK_X,   BT_FOOT_Y, BT_FOOT_W, BT_FOOT_H, TFT_WHITE);
    tft.setFont(&fonts::Font4); tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, TFT_NAVY);
    tft.drawString("RESCAN", BT_RESCAN_X + BT_FOOT_W / 2, BT_FOOT_Y + BT_FOOT_H / 2);
    tft.setTextColor(TFT_WHITE, TFT_DARKGREEN);
    tft.drawString("BACK", BT_BACK_X + BT_FOOT_W / 2, BT_FOOT_Y + BT_FOOT_H / 2);
    tft.setTextDatum(textdatum_t::top_left);
}

static void handlePidScanTap(int x, int y) {
    if (y >= BT_FOOT_Y && y <= BT_FOOT_Y + BT_FOOT_H) {
        if (x >= BT_RESCAN_X && x <= BT_RESCAN_X + BT_FOOT_W) {
            if (obd::connected() && !obd::pidScanning()) obd::startPidScan();
            pid_scan_dirty = true; return;
        }
        if (x >= BT_BACK_X && x <= BT_BACK_X + BT_FOOT_W) {
            currentPage = PAGE_SENSOR; pageJustEntered = true; sensor_page_dirty = true; return;
        }
    }
    if (obd::pidScanning()) return;   // list still changing — ignore row taps
    if (y >= PS_ROW_Y0 && y < PS_ROW_Y0 + PS_VIEW_H) {
        const int rel = y - PS_ROW_Y0 + pid_scan_scroll;
        const int i   = rel / PS_ROW_H;
        if (i >= 0 && i < (int)obd::pidCount() && (rel % PS_ROW_H) < PS_ROW_H - 4) {
            const obd::PidItem* it = obd::pidItem((uint8_t)i);
            if (it) {
                s.bt_pid_clt = it->pid;
                obd::setCoolantPid(it->pid);
                saveSettings();
                pid_scan_dirty = true;
            }
        }
    }
}

static void handleBtScanTap(int x, int y) {
    const int n = obd::scanCount();
    if (y >= BT_ROW_Y0 && y < BT_ROW_Y0 + BT_VIEW_H) {
        const int rel = y - BT_ROW_Y0 + bt_scan_scroll;   // scroll-aware hit test
        const int i   = rel / BT_ROW_H;
        if (i >= 0 && i < n && (rel % BT_ROW_H) < BT_ROW_H - 6) { selectBtDevice(i); return; }
    }
    if (y >= BT_FOOT_Y && y <= BT_FOOT_Y + BT_FOOT_H) {
        if (x >= BT_RESCAN_X && x <= BT_RESCAN_X + BT_FOOT_W) { obd::startScan(); bt_scan_dirty = true; return; }
        if (x >= BT_BACK_X   && x <= BT_BACK_X   + BT_FOOT_W) { currentPage = PAGE_SENSOR; pageJustEntered = true; sensor_page_dirty = true; return; }
    }
}

static const char* enumValue(SettingId id) {
    switch (id) {
        case ST_INET_MODE:   return INET_MODE_NAMES[s.internet_mode % N_INET_MODE];
        case ST_CL_PROTO:    return PROTOCOL_NAMES[s.cloud_protocol % N_PROTOCOL];
        case ST_TIMEZONE:    return TIMEZONES[s.timezone_idx % N_TIMEZONES].name;
        case ST_SENSOR_TYPE: return SENSOR_TYPE_NAMES[s.sensor_type % N_SENSOR_TYPE];
        case ST_RPM_SPIKE:   return SPIKE_FILTER_NAMES[s.rpm_spike % N_SPIKE_FILTER];
        case ST_GPS_FILTER:  return SPIKE_FILTER_NAMES[s.gps_filter % N_SPIKE_FILTER];
        case ST_RPM_DIV:     return RPM_PPR_NAMES[rpmPprIndex()];
        case ST_GPS_BAUD: {
            static char b[24];
            snprintf(b, sizeof(b), "%s/%sHz",
                     GPS_BAUD_NAMES[gpsBaudIndex()], GPS_HZ_NAMES[gpsHzIndex()]);
            return b;
        }
        default:             return "?";
    }
}

// Conditionally hide settings rows based on hardware state.
// ST_REC_SD only makes sense when a card is mounted; ST_SD_FORMAT now lives
// the card is present but unformatted. Hiding keeps the list uncluttered
// and prevents tapping controls that have no effect.
// Which rows are visible right now. A settings row is hidden when the toggle/
// selector it depends on is off/irrelevant, so disabling a feature collapses
// all of its dependent sub-settings out of the list (they don't just grey out).
// rowScreenY() compacts the gaps, and the tap/draw/height loops all gate on
// this, so a hidden row can't be drawn, tapped, or counted in the scroll height.
static bool rowShouldShow(SettingId id) {
    switch (id) {
        case ST_REC_SD:    return sd_card_status == 2;  // hidden unless card is READY
        // (ST_SD_FORMAT row never appears in the settings list anymore —
        //  the maintenance action moved to PAGE_TOOLS.)
        // WiFi credential + status rows only meaningful when mode=WiFi.
        case ST_WIFI_SSID:
        case ST_WIFI_PASS:
        case ST_WIFI_STATUS: return s.internet_mode == 1;

        // Tach pulses/rev divider only applies to the Direct opto tach; in
        // MegaSquirt mode RPM comes straight from CAN.
        case ST_RPM_DIV:   return s.sensor_type == 0;

        // RPM alert thresholds/colours/blink only when alerts are enabled.
        case ST_A1_RPM:
        case ST_A1_COL:
        case ST_A1_HZ:
        case ST_AM_RPM:
        case ST_AM_COL:
        case ST_AM_HZ:     return s.alerts_enabled;

        // Coolant warn threshold + colour only when the gauge is shown.
        case ST_TEMP_WARN_F:
        case ST_TEMP_WARN_COL: return s.show_coolant;
        case ST_VOLT_WARN:
        case ST_VOLT_WARN_COL: return s.show_volt;

        // Oil-pressure warn threshold + colour only when the gauge is shown.
        case ST_PSI_WARN_PSI:
        case ST_PSI_WARN_COL:  return s.show_oil_psi;

        // AFR is a MegaSquirt-only reading: hide the whole AFR block in Direct
        // mode, and hide the warn sub-settings unless AFR display is on.
        case ST_SHOW_AFR:  return s.sensor_type == 1;
        case ST_AFR_WARN_LO:
        case ST_AFR_WARN_HI:
        case ST_AFR_WARN_COL:  return s.sensor_type == 1 && s.show_afr;

        // Cloud endpoint/credentials only matter when recording to cloud.
        case ST_CL_HOST:
        case ST_CL_PORT:
        case ST_CL_PROTO:
        case ST_CL_AUTH_USER:
        case ST_CL_AUTH_PASS:  return s.record_cloud;

        // Auto-start speed/hold only matter when auto-start is enabled.
        case ST_AUTO_START_MPH: case ST_AUTO_START_SEC: return s.auto_start;
        default:           return true;
    }
}

// Settings sections. A divider line is drawn above the first visible row whose
// group differs from the row above it, so related settings read as a block.
enum SettingsGroup : int { SG_DISPLAY, SG_NET, SG_RPM, SG_SENSORS, SG_RECORDING, SG_CLOUD, SG_TIME };
static int rowGroup(SettingId id) {
    switch (id) {
        case ST_BRIGHTNESS: case ST_LAP_OVERLAY: return SG_DISPLAY;
        case ST_INET_MODE:
        case ST_WIFI_SSID: case ST_WIFI_PASS: case ST_WIFI_STATUS: return SG_NET;
        case ST_RPM_MIN: case ST_RPM_MAX: case ST_RPM_DIV: case ST_RPM_SMOOTH: case ST_RPM_SPIKE:
        case ST_GPS_FILTER: case ST_ALERTS:
        case ST_A1_RPM: case ST_A1_COL: case ST_A1_HZ:
        case ST_AM_RPM: case ST_AM_COL: case ST_AM_HZ: return SG_RPM;
        case ST_SENSOR_TYPE:
        case ST_SHOW_TEMP: case ST_TEMP_WARN_F: case ST_TEMP_WARN_COL:
        case ST_SHOW_PSI:  case ST_PSI_WARN_PSI: case ST_PSI_WARN_COL:
        case ST_SHOW_AFR:  case ST_AFR_WARN_LO: case ST_AFR_WARN_HI: case ST_AFR_WARN_COL:
        case ST_SHOW_VOLT: case ST_VOLT_WARN: case ST_VOLT_WARN_COL: return SG_SENSORS;
        case ST_COACH_SHOW: return SG_RECORDING;
        case ST_REC_SD: case ST_REC_CLOUD: case ST_AUTO_TRACK:
        case ST_AUTO_START: case ST_AUTO_START_MPH:
        case ST_AUTO_START_SEC: return SG_RECORDING;
        case ST_CL_HOST: case ST_CL_PORT: case ST_CL_PROTO:
        case ST_CL_AUTH_USER: case ST_CL_AUTH_PASS: return SG_CLOUD;
        case ST_TIMEZONE: case ST_GPS_BAUD: case ST_GPS_STATUS:
        case ST_DEBUG_LOG:
        case ST_SET_TIME: return SG_TIME;
        default: return SG_DISPLAY;
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

// Brightness slider geometry / hit-testing (used by the touch handler so a
// drag on the slider isn't mistaken for a page swipe or a scroll).
static SettingId settingsSliderHit(int x, int y) {
    if (currentPage != PAGE_SETTINGS) return ST_COUNT;
    for (uint8_t i = 0; i < ST_COUNT; ++i) {
        if (ROWS[i].kind != SettingRow::SLIDER) continue;
        if (!rowShouldShow(ROWS[i].id)) continue;
        const int ry = rowScreenY(i);
        if (!rowVisible(ry)) continue;
        // Only the track region grabs; left of it (the label) stays scroll/swipe.
        if (y >= ry && y < ry + SETTINGS_ROW_DY &&
            x >= SLIDER_TRACK_X - 20 && x <= SLIDER_TRACK_X + SLIDER_TRACK_W + 40)
            return ROWS[i].id;
    }
    return ST_COUNT;
}
// Value range + current value for a SLIDER-kind row.
static void sliderSpec(SettingId id, int* vmin, int* vmax, int* val) {
    switch (id) {
        case ST_BRIGHTNESS: *vmin = 10;  *vmax = 100; *val = s.brightness; break;
        case ST_RPM_SMOOTH: *vmin = -10; *vmax = 10;  *val = s.rpm_smooth; break;
        default:            *vmin = 0;   *vmax = 100; *val = 0;            break;
    }
}
static int sliderValFromX(SettingId id, int x) {
    int vmin, vmax, cur; sliderSpec(id, &vmin, &vmax, &cur);
    const int span = (vmax > vmin) ? (vmax - vmin) : 1;
    int v = vmin + ((x - SLIDER_TRACK_X) * span + SLIDER_TRACK_W / 2) / SLIDER_TRACK_W;
    if (v < vmin) v = vmin;
    if (v > vmax) v = vmax;
    return v;
}
static void sliderSetFromX(SettingId id, int x) {
    const int v = sliderValFromX(id, x);
    if (id == ST_BRIGHTNESS)      { s.brightness = (uint8_t)v; applyBrightness(s.brightness); }
    else if (id == ST_RPM_SMOOTH) { s.rpm_smooth = (int8_t)v; }
}

static void drawSettingsPage() {
    // Content height depends on how many rows are currently visible.
    int visCount = 0;
    for (uint8_t i = 0; i < ST_COUNT; ++i)
        if (rowShouldShow(ROWS[i].id)) visCount++;
    settingsContentHeight = visCount * SETTINGS_ROW_DY + 10;
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
    // Cap redraws to ~30 Hz during drags so the LCD gets a clean scan window
    // between renders. Stays dirty so the next loop picks it up.
    static uint32_t lastDraw = 0;
    if (millis() - lastDraw < 33) return;
    lastDraw = millis();
    settingsDirty = false;

    // Per-row direct draw with narrow strip-wipes on scroll. (A full-screen
    // PSRAM back-buffer push was tried and reverted: on this single-framebuffer
    // RGB panel the ~600 KB push burst-starves the scan-out DMA and tears.)
    static int lastDrawnScrollY = -1;
    // When a toggle collapses/expands its dependent rows the visible-row COUNT
    // changes and every row below shifts — a strip-wipe isn't enough (the rows
    // that used to sit at the bottom would linger). Full-clear the body band
    // once on any visible-count change so no stale row text is left behind.
    static int lastVisCount = -1;
    if (visCount != lastVisCount) {
        tft.fillRect(0, BODY_TOP, 800, BODY_HEIGHT, TFT_BLACK);
        lastVisCount = visCount;
        lastDrawnScrollY = settingsScrollY;
    }
    if (settingsScrollY != lastDrawnScrollY) {
        tft.fillRect(0, BODY_TOP,                       800, SETTINGS_ROW_DY, TFT_BLACK);
        tft.fillRect(0, BODY_BOTTOM - SETTINGS_ROW_DY,  800, SETTINGS_ROW_DY, TFT_BLACK);
        lastDrawnScrollY = settingsScrollY;
    }

    tft.setTextSize(1);
    tft.setFont(&fonts::Font4);
    tft.setTextDatum(textdatum_t::top_left);

    int prevGroup = -1;
    for (uint8_t i = 0; i < ST_COUNT; ++i) {
        const SettingRow& r = ROWS[i];
        if (!rowShouldShow(r.id)) continue;
        const int  g            = rowGroup(r.id);
        const bool sectionStart = (prevGroup >= 0 && g != prevGroup);
        prevGroup = g;
        const int y = rowScreenY(i);
        if (!rowVisible(y)) continue;

        tft.setClipRect(0, BODY_TOP, 800, BODY_HEIGHT);
        tft.fillRect(0, y, 800, SETTINGS_ROW_DY, TFT_BLACK);
        // Section divider above the first row of each group.
        if (sectionStart) tft.drawFastHLine(20, y + 1, 760, TFT_DARKGREY);
        tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
        tft.drawString(r.label, 20, y + 6);

        if (r.kind == SettingRow::SLIDER) {
            // groove + filled portion + knob + value readout (brightness % or
            // RPM-smoothing +/-). 0 sits mid-track for the signed smoothing slider.
            int vmin, vmax, val; sliderSpec(r.id, &vmin, &vmax, &val);
            const int cy   = y + SETTINGS_ROW_HEIGHT / 2;
            const int span = (vmax > vmin) ? (vmax - vmin) : 1;
            int fillW = (int)((long)(val - vmin) * SLIDER_TRACK_W / span);
            if (fillW < 0) fillW = 0;
            if (fillW > SLIDER_TRACK_W) fillW = SLIDER_TRACK_W;
            tft.fillRoundRect(SLIDER_TRACK_X, cy - 4, SLIDER_TRACK_W, 8, 4, TFT_DARKGREY);
            tft.fillRoundRect(SLIDER_TRACK_X, cy - 4, fillW > 1 ? fillW : 1, 8, 4, TFT_CYAN);
            const int kx = SLIDER_TRACK_X + fillW;
            tft.fillCircle(kx, cy, 12, TFT_WHITE);
            tft.drawCircle(kx, cy, 12, TFT_DARKGREY);
            char pbuf[8];
            if (r.id == ST_BRIGHTNESS) snprintf(pbuf, sizeof(pbuf), "%d%%", val);
            else                       snprintf(pbuf, sizeof(pbuf), "%+d", val);
            tft.setTextColor(TFT_WHITE, TFT_BLACK);
            tft.setTextDatum(textdatum_t::middle_right);
            tft.drawString(pbuf, 790, cy);
            tft.setTextDatum(textdatum_t::top_left);
        } else if (r.kind == SettingRow::NUMERIC) {
            // [-]
            tft.fillRect(CTRL_MINUS_X, y, CTRL_MINUS_W, SETTINGS_ROW_HEIGHT, TFT_DARKGREY);
            tft.setTextColor(TFT_WHITE, TFT_DARKGREY);
            tft.setTextDatum(textdatum_t::middle_center);
            tft.drawString("-", CTRL_MINUS_X + CTRL_MINUS_W / 2, y + SETTINGS_ROW_HEIGHT / 2);
            // value
            char buf[16];
            if (r.id == ST_RPM_DIV) snprintf(buf, sizeof(buf), "%s", RPM_PPR_NAMES[rpmPprIndex()]);
            else                    snprintf(buf, sizeof(buf), "%u", (unsigned)getNum(r.id));
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
                case ST_AFR_WARN_COL:  cidx = s.afr_warn_col;        break;
                case ST_VOLT_WARN_COL: cidx = s.volt_warn_col;       break;
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
            if (r.id == ST_GPS_STATUS)  shown = gps_status_buf[0]  ? gps_status_buf  : "-";
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
            // Tach pulses/rev steps through a discrete list (0.5,1,2,3,4,6,8) via
            // -/+; no keypad (free numeric entry wouldn't map to the list).
            if (r.id == ST_RPM_DIV) {
                // Discrete list (0.5..8) via -/+; no keypad. IMPORTANT: only
                // return when the tap actually hit THIS row's -/+ ; otherwise
                // fall through so taps on rows below this one still dispatch.
                int idx = rpmPprIndex();
                if (inRect(x, y, CTRL_MINUS_X, ry, CTRL_MINUS_W, SETTINGS_ROW_HEIGHT)) {
                    if (idx > 0) idx--;
                    s.rpm_ppr_x10 = RPM_PPR_X10[idx]; settingsDirty = true; return;
                }
                if (inRect(x, y, CTRL_PLUS_X, ry, CTRL_PLUS_W, SETTINGS_ROW_HEIGHT)) {
                    if (idx < N_RPM_PPR - 1) idx++;
                    s.rpm_ppr_x10 = RPM_PPR_X10[idx]; settingsDirty = true; return;
                }
            } else {
                // Tap the value to type it directly on the numeric keypad.
                if (inRect(x, y, CTRL_VALUE_X, ry, CTRL_VALUE_W, SETTINGS_ROW_HEIGHT)) {
                    openNumericKeyboard(r.id);
                    return;
                }
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
            }
        } else if (r.kind == SettingRow::TOGGLE) {
            if (inRect(x, y, CTRL_TOGGLE_X, ry, CTRL_TOGGLE_W, SETTINGS_ROW_HEIGHT)) {
                switch (r.id) {
                    case ST_ALERTS:     s.alerts_enabled    = !s.alerts_enabled;    break;
                    case ST_REC_SD:     s.record_sd         = !s.record_sd;         break;
                    case ST_REC_CLOUD:  s.record_cloud      = !s.record_cloud;      break;
                    case ST_AUTO_TRACK: s.auto_select_track = !s.auto_select_track; break;
                    case ST_DEBUG_LOG:  s.debug_enabled = !s.debug_enabled;
                                        Serial.printf("CFG,dbg_on,%d\n", (int)s.debug_enabled); break;
                    case ST_AUTO_START: s.auto_start        = !s.auto_start;        break;
                    case ST_SHOW_TEMP:  s.show_coolant      = !s.show_coolant;      break;
                    case ST_SHOW_VOLT:  s.show_volt         = !s.show_volt;         break;
                    case ST_COACH_SHOW: s.coach_show        = !s.coach_show;        break;
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
                    case ST_VOLT_WARN_COL: s.volt_warn_col      = (s.volt_warn_col      + 1) % N_PALETTE; break;
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
                    // Snap the port to the new protocol's well-known default so
                    // the common case (HTTPS->443) needs zero extra taps. Still
                    // fully editable afterwards via the Cloud port row.
                    static const uint16_t PROTO_DEFAULT_PORT[] = { 80, 443, 21 };
                    s.cloud_port = PROTO_DEFAULT_PORT[s.cloud_protocol % N_PROTOCOL];
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
                } else if (r.id == ST_GPS_BAUD) {
                    // Don't cycle-on-tap (rapid module switches freak the GPS
                    // out) — open the dedicated GPS page for deliberate selection.
                    openGpsPage();
                    return;
                } else if (r.id == ST_RPM_SPIKE) {
                    s.rpm_spike = (s.rpm_spike + 1) % N_SPIKE_FILTER;
                    Serial.printf("CFG,rpmspk,%u\n", (unsigned)s.rpm_spike);
                } else if (r.id == ST_GPS_FILTER) {
                    s.gps_filter = (s.gps_filter + 1) % N_SPIKE_FILTER;
                    Serial.printf("CFG,gpsflt,%u\n", (unsigned)s.gps_filter);
                } else if (r.id == ST_SENSOR_TYPE) {
                    // Open the dedicated Sensor Source page (Direct/MegaSquirt/
                    // Bluetooth) instead of cycling — Bluetooth needs a device
                    // picker + status, which a cycle-on-tap can't do.
                    ecu = EcuState{};   // clear stale ECU on any source change
                    openSensorPage();
                    return;
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
        } else if (r.kind == SettingRow::TEXT) { // open the appropriate on-screen keyboard.
                 // (SLIDER + INFO rows have no tap action — the slider is
                 //  handled as a drag gesture in handleTouch.)
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
            default: {
                // Any numeric settings row typed on the keypad (RPM min/max,
                // alert RPM/Hz, temp/psi/afr): clamp to that setting's bounds.
                const NumBounds nb = numBounds(kb.target);
                if (nb.step > 0) {
                    long v = atol(kb.editBuf);
                    if (v < nb.lo) v = nb.lo;
                    if (v > nb.hi) v = nb.hi;
                    setNum(kb.target, (uint16_t)v);
                    clampInvariants();
                }
                break;
            }
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
        if (TRACKS[i].aux) continue;   // hidden S/F-storage tombstones (v0.1.115)
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
        } else {
            // Unlisted track: make the selection STICK so a later START honours
            // it directly (instead of re-opening the picker), and so the TRACK
            // button can pre-select it. We persist it as the active track with
            // last_track_idx = -1 (no known TRACKS[] entry). Wire name stays the
            // documented "UNKNOWN" sentinel.
            strncpy(active_track_name, "UNKNOWN", sizeof(active_track_name) - 1);
            active_track_name[sizeof(active_track_name) - 1] = '\0';
            last_track_idx = -1;
            prefs.begin("dash", false);
            prefs.putString("last_trk",   "UNKNOWN");   // matches no TRACKS[] -> idx=-1 on reload
            prefs.putString("last_trk_d", "UNKNOWN");
            prefs.end();
            sendSfToTeensy(-1);   // unknown-track captured S/F (if any) for lap stamping
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
            tft.drawString("Unlisted track  (record here anyway)", 20, y + 6);
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
    // Config index FIRST — saveLastTrack persists it + sends the (possibly
    // config-specific) S/F line to the Teensy via sendSfToTeensy.
    active_cfg_idx = (cp.selected >= 0 && cp.selected < (int)t.n_configs)
                     ? (uint8_t)cp.selected : 0;
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
        // (Phase title is drawn dynamically below, every frame.)
        // Static CANCEL button frame
        tft.fillRect(UM_BTN_X, UM_BTN_Y, UM_BTN_W, UM_BTN_H, TFT_MAROON);
        tft.drawRect(UM_BTN_X, UM_BTN_Y, UM_BTN_W, UM_BTN_H, TFT_WHITE);
        tft.drawRect(UM_BTN_X+1, UM_BTN_Y+1, UM_BTN_W-2, UM_BTN_H-2, TFT_WHITE);
        tft.setTextColor(TFT_WHITE, TFT_MAROON);
        tft.drawString("CANCEL", UM_BTN_X + UM_BTN_W/2, UM_BTN_Y + UM_BTN_H/2);
        tft.setTextDatum(textdatum_t::top_left);
        pageJustEntered = false;
    }

    // Dynamic phase title. Uploads are two explicit phases: first the whole
    // session file is copied off the Teensy/SD into the dash's PSRAM cache
    // ("Copying to cache"), and ONLY once that completes does the network
    // upload begin ("Uploading to cloud"). Redrawn each frame with its own bg.
    const char* phase = "Uploading session";
    switch (uf.state) {
        case UF_LISTING:
        case UF_FETCH_HEAD:    phase = "Preparing...";           break;
        case UF_STREAMING:     phase = "Streaming to cloud...";  break;
        case UF_POSTING:       phase = "Streaming to cloud...";  break;
        case UF_RETRY_WAIT:    phase = "Retrying...";            break;
        case UF_STREAM_FINISH: phase = "Finalizing...";          break;
        case UF_DELETING:      phase = "Cleaning up...";      break;
        default: break;
    }
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextColor(TFT_WHITE, TFT_NAVY);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextPadding(UM_CARD_W - 40);
    // Append "n/N" when the batch has more than one file, so a multi-session
    // drain shows which one it's on instead of looking like it restarted.
    char phbuf[64];
    if (uf.files_n > 1 && uf.files_idx < uf.files_n)
        snprintf(phbuf, sizeof(phbuf), "%s  %d/%d", phase, uf.files_idx + 1, uf.files_n);
    else
        snprintf(phbuf, sizeof(phbuf), "%s", phase);
    tft.drawString(phbuf, UM_CARD_X + UM_CARD_W / 2, UM_CARD_Y + 30);
    tft.setTextPadding(0);

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
    const uint16_t barColor = TFT_GREEN;
    tft.drawRect(UM_BAR_X, UM_BAR_Y, UM_BAR_W, UM_BAR_H, TFT_WHITE);
    tft.fillRect(UM_BAR_X + 2, UM_BAR_Y + 2, UM_BAR_W - 4, UM_BAR_H - 4, TFT_BLACK);
    if (fillW > 0)
        tft.fillRect(UM_BAR_X + 2, UM_BAR_Y + 2, fillW, UM_BAR_H - 4, barColor);

    // ---- Bytes / percent / RATE / ETA (v0.1.136) ----
    // Rate is sampled on a ~700 ms cadence and lightly smoothed: steady enough
    // to read, but it visibly collapses toward 0 when the Teensy stalls, which
    // is exactly when the driver wants to know something is wrong.
    static uint32_t rt_last_ms   = 0;
    static uint32_t rt_last_done = 0;
    static float    rt_bps       = 0.0f;
    {
        const uint32_t nr = millis();
        if (rt_last_ms == 0 || done < rt_last_done) {          // new file / reset
            rt_last_ms = nr; rt_last_done = done; rt_bps = 0.0f;
        } else if (nr - rt_last_ms >= 700) {
            const float inst = (float)(done - rt_last_done) * 1000.0f
                             / (float)(nr - rt_last_ms);
            rt_bps     = (rt_bps <= 0.0f) ? inst : (rt_bps * 0.6f + inst * 0.4f);
            rt_last_ms = nr; rt_last_done = done;
        }
    }
    char line[80];
    const int pct = (int)((uint64_t)done * 100 / total);
    if (upload_total <= 1) {
        // Nothing known yet (Q,LIST/Q,GET outstanding) — say so instead of
        // showing a meaningless "0 / 0 KB".
        snprintf(line, sizeof(line), "reading queue...");
    } else {
        char amt[40], extra[32] = "";
        if (upload_total >= 1024 * 1024)
            snprintf(amt, sizeof(amt), "%lu.%lu / %lu.%lu MB  %d%%",
                     (unsigned long)(done  / (1024UL*1024UL)),
                     (unsigned long)((done  % (1024UL*1024UL)) / 104858UL),
                     (unsigned long)(total / (1024UL*1024UL)),
                     (unsigned long)((total % (1024UL*1024UL)) / 104858UL),
                     pct);
        else
            snprintf(amt, sizeof(amt), "%lu / %lu KB  %d%%",
                     (unsigned long)(done / 1024UL),
                     (unsigned long)(total / 1024UL), pct);
        if (rt_bps > 512.0f && done < total) {
            const uint32_t eta = (uint32_t)(((float)(total - done)) / rt_bps);
            if (eta < 6000) {
                if (eta >= 60) snprintf(extra, sizeof(extra), "  %.0fK/s  %lu:%02lu",
                                        rt_bps / 1024.0f,
                                        (unsigned long)(eta / 60), (unsigned long)(eta % 60));
                else           snprintf(extra, sizeof(extra), "  %.0fK/s  %lus",
                                        rt_bps / 1024.0f, (unsigned long)eta);
            }
        }
        snprintf(line, sizeof(line), "%s%s", amt, extra);
    }
    tft.setFont(&fonts::Font4);
    tft.setTextColor(TFT_WHITE, TFT_NAVY);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextPadding(UM_CARD_W - 40);
    tft.drawString(line, UM_CARD_X + UM_CARD_W / 2, UM_BAR_Y + UM_BAR_H + 24);
    tft.setTextPadding(0);

    // Diagnostic internals line (v0.1.119): raw uploader state so a stuck
    // modal is self-describing — S=flow state, N=net task state (0 idle /
    // 1 running / 2 done / 3 failed), rt/rh = ring sent/queued KB, then the
    // most recent error text. Photograph this line when something wedges.
    {
        // Error preference: last_err first — net_err is often just "aborted",
        // which is the SYMPTOM of the loop's abort, not the cause (v0.1.120).
        const char* derr = uf.last_err[0] ? uf.last_err : uf.net_err;
        char diag[110];
        snprintf(diag, sizeof(diag), "S%u N%u rt=%luK rh=%luK %.48s",
                 (unsigned)uf.state, (unsigned)uf.net_state,
                 (unsigned long)(uf.ring_tail / 1024),
                 (unsigned long)(uf.ring_head / 1024), derr);
        tft.setFont(&fonts::Font2);
        tft.setTextColor(TFT_LIGHTGREY, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextPadding(UM_CARD_W - 40);
        tft.drawString(diag, UM_CARD_X + UM_CARD_W / 2, UM_BAR_Y + UM_BAR_H + 46);
        tft.setTextPadding(0);
    }

    // Result banner replaces filename area when DONE arrives.
    if (upload_result_msg[0] != '\0') {
        uint16_t banner = TFT_GREEN;
        if (strncmp(upload_result_msg, "FAIL", 4)      == 0) banner = TFT_RED;
        if (strncmp(upload_result_msg, "CANCELLED", 9) == 0) banner = TFT_ORANGE;
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
// OTA now pulls from OUR OWN server (racecar.api.blueuc.com), not GitHub raw.
// The server serves /firmware/manifest.json with Cache-Control: no-store, so a
// freshly published version is visible to the dash INSTANTLY — no CDN lag. (The
// GitHub raw path was fronted by Fastly, which ignores query-string cache-
// busting AND client no-cache headers and serves ~5 min stale after a push:
// the "device still sees the old version right after publishing" pain.) The
// artifact .bin/.hex URLs inside the manifest also point at the server and are
// served no-store. Push new firmware with server/publish_firmware.sh.
static constexpr const char* OTA_MANIFEST_URL =
    "https://racecar.api.blueuc.com/firmware/manifest.json";

// Kept from the GitHub-CDN era as belt-and-suspenders: appending a unique query
// string + sending no-cache request headers. Harmless against our no-store
// server (it ignores the extra query param); still helps if OTA_MANIFEST_URL is
// ever repointed at a caching host. dst must hold the URL + ~24 bytes.
static void otaBustCache(char* dst, size_t dstsz, const char* url) {
    const char sep = (strchr(url, '?') != nullptr) ? '&' : '?';
    snprintf(dst, dstsz, "%s%ccb=%lu", url, sep, (unsigned long)millis());
}

namespace {
    constexpr int OM_CARD_X = 80,  OM_CARD_Y = 55;
    constexpr int OM_CARD_W = 640, OM_CARD_H = 370;
    constexpr int OM_BAR_X  = OM_CARD_X + 30, OM_BAR_Y = OM_CARD_Y + 145;
    constexpr int OM_BAR_W  = OM_CARD_W - 60, OM_BAR_H = 24;
    constexpr int OM_BAR2_Y = OM_BAR_Y + 72;
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

// Parse "a.b.c.d" into 4 ints (missing components = 0). Hand-rolled on
// purpose: these were the ONLY two sscanf() calls in the firmware, and
// linking sscanf drags in newlib's __ssvfscanf_r + __ssvfiscanf_r — ~17 KB
// of flash for one version compare. Same semantics for version strings
// (stops at the first non-digit, leaves the rest zero).
static void versionParse4(const char* s, int* out) {
    out[0] = out[1] = out[2] = out[3] = 0;
    int i = 0;
    while (i < 4 && *s) {
        if (*s < '0' || *s > '9') break;
        int v = 0;
        while (*s >= '0' && *s <= '9') { v = v * 10 + (*s - '0'); ++s; }
        out[i++] = v;
        if (*s == '.') ++s; else break;
    }
}

static int versionCmp(const char* a, const char* b) {
    int aa[4], bb[4];
    versionParse4(versionNoV(a), aa);
    versionParse4(versionNoV(b), bb);
    for (int i = 0; i < 4; ++i) if (aa[i] != bb[i]) return aa[i] - bb[i];
    return 0;
}

// Compute SHA256 of the given buffer and format it as lowercase hex into
// `out_hex` (must be at least 65 bytes). Used to verify that downloaded OTA
// artifacts match the sha256 in firmware/manifest.json before flashing. Raw
// GitHub serves manifest.json and the artifacts independently and the
// artifact CDN edge can lag the manifest after a push, so we may briefly
// receive a fresh manifest pointing at a stale .bin/.hex. Verifying the
// sha256 prevents flashing those stale bytes.
static void computeSha256Hex(const uint8_t* data, size_t len, char* out_hex) {
    uint8_t digest[32];
    mbedtls_sha256_context ctx;
    mbedtls_sha256_init(&ctx);
    mbedtls_sha256_starts_ret(&ctx, 0);   // 0 = SHA-256 (not SHA-224)
    mbedtls_sha256_update_ret(&ctx, data, len);
    mbedtls_sha256_finish_ret(&ctx, digest);
    mbedtls_sha256_free(&ctx);
    static const char H[] = "0123456789abcdef";
    for (int i = 0; i < 32; ++i) {
        out_hex[i*2]     = H[(digest[i] >> 4) & 0xF];
        out_hex[i*2 + 1] = H[digest[i]        & 0xF];
    }
    out_hex[64] = '\0';
}

// Case-insensitive sha256 hex compare. Manifest uses lowercase but be lenient.
static bool sha256HexEqual(const char* a, const char* b) {
    if (!a || !b) return false;
    for (int i = 0; i < 64; ++i) {
        char ca = a[i], cb = b[i];
        if (ca >= 'A' && ca <= 'F') ca = (char)(ca - 'A' + 'a');
        if (cb >= 'A' && cb <= 'F') cb = (char)(cb - 'A' + 'a');
        if (ca != cb) return false;
        if (ca == '\0') return true;
    }
    return a[64] == '\0' && b[64] == '\0';
}

// Blocking but short version refresh used before making OTA decisions. The
// normal pumpUart() path may have a stale teensy_fw_version if the previous
// OTA attempt false-failed and the user immediately retries from the modal.
// Query here so we don't reflash a Teensy that already updated successfully.
static bool queryTeensyVersionBlocking(uint32_t timeout_ms) {
    Serial.println("VER?");
    Serial.flush();

    char line[180];
    size_t n = 0;
    bool got = false;
    const uint32_t end = millis() + timeout_ms;
    while ((int32_t)(end - millis()) > 0) {
        while (Serial.available()) {
            const char c = (char)Serial.read();
            if (c == '\r') continue;
            if (c == '\n') {
                line[n] = '\0';
                if (n > 0) {
                    if (strncmp(line, "VER,teensy,", 11) == 0) {
                        strncpy(teensy_fw_version, versionNoV(line + 11),
                                sizeof(teensy_fw_version) - 1);
                        teensy_fw_version[sizeof(teensy_fw_version) - 1] = '\0';
                        got = true;
                    } else {
                        // Don't discard useful telemetry/status lines just
                        // because we are doing a synchronous OTA preflight.
                        parseLine(String(line));
                    }
                }
                n = 0;
                if (got) return true;
            } else if (n + 1 < sizeof(line)) {
                line[n++] = c;
            } else {
                n = 0;
            }
        }
        delay(2);
    }
    return got;
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
        ota_t_dl_total        = 0;
        ota_t_dl_done         = 0;
        ota_t_tx_total        = 0;
        ota_t_tx_done         = 0;
        ota_cancel_requested  = false;
        ota_teensy_commit_seen = false;
        ota_state             = OTA_S_CHECKING;
    }
    otaOpenModal();
}

static void otaDoCheck() {
    WiFiClientSecure client;
    client.setInsecure();   // TODO: embed GitHub root CA for proper validation
    HTTPClient http;
    char manifest_url[192];
    otaBustCache(manifest_url, sizeof(manifest_url), OTA_MANIFEST_URL);
    if (!http.begin(client, manifest_url)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "manifest HTTPS begin failed");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Pragma", "no-cache");
    const int code = http.GET();
    if (code != HTTP_CODE_OK) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "manifest fetch HTTP %d", code);
        http.end();
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    const String body = http.getString();
    http.end();

    // This board only ever consumes the manifest entry keyed by its own
    // compile-time DASH_BOARD_ID (e.g. "crowpanel5"), so a 5" can never pull a
    // 7" image (mismatched RGB timing = black screen) and vice-versa.
    if (!jsonStr(body, DASH_OTA_KEY, "\"version\"",
                 ota_latest_version, sizeof(ota_latest_version)) ||
        !jsonStr(body, DASH_OTA_KEY, "\"url\"",
                 ota_url, sizeof(ota_url))) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "no %s entry in manifest", DASH_BOARD_ID);
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    ota_sha256[0] = '\0';
    jsonStr(body, DASH_OTA_KEY, "\"sha256\"", ota_sha256, sizeof(ota_sha256));
    // Belt-and-suspenders: each manifest entry also carries a "board" field.
    // If a publish error ever points our entry at the wrong-board binary,
    // refuse rather than brick the panel. Recovery is always a USB reflash.
    {
        char mboard[24] = "";
        if (jsonStr(body, DASH_OTA_KEY, "\"board\"", mboard, sizeof(mboard)) &&
            strcmp(mboard, DASH_BOARD_ID) != 0) {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "board mismatch: %s vs %s", mboard, DASH_BOARD_ID);
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
    }
    // Parse teensy entry too (optional — if missing, only crowpanel update considered)
    ota_teensy_version[0] = '\0';
    ota_teensy_url[0]     = '\0';
    ota_teensy_size       = 0;
    ota_teensy_sha256[0]  = '\0';
    char tsize_buf[16] = "";
    jsonStr(body, "\"teensy\"", "\"version\"",
            ota_teensy_version, sizeof(ota_teensy_version));
    jsonStr(body, "\"teensy\"", "\"url\"",
            ota_teensy_url, sizeof(ota_teensy_url));
    jsonStr(body, "\"teensy\"", "\"sha256\"",
            ota_teensy_sha256, sizeof(ota_teensy_sha256));
    // Size is a number in the manifest, not a quoted string. Find it by hand.
    {
        int s = body.indexOf("\"teensy\"");
        int k = (s >= 0) ? body.indexOf("\"size\"", s) : -1;
        int colon = (k >= 0) ? body.indexOf(':', k + 6) : -1;
        if (colon >= 0) ota_teensy_size = (uint32_t)body.substring(colon + 1).toInt();
    }

    // Refresh Teensy version RIGHT BEFORE deciding whether it needs an update.
    // This prevents stale cached versions from causing an unnecessary second
    // Teensy flash after a previous attempt actually succeeded but the dash
    // missed the final VER response.
    queryTeensyVersionBlocking(1500);

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
// catch the Teensy's FW handshake lines without going through pumpUart
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

// After the final Intel HEX EOF line is ACKed, FlasherX still validates the
// staged image, then emits FW,COMMITTING immediately before flash_move(). The
// reboot/version wait timer should start AFTER that marker, not after the last
// line ACK. Starting too early caused false failures where the dash gave up at
// 60 s, then the Teensy finished flash_move() and rebooted successfully a few
// seconds later.
// Return:  1 = marker seen, 0 = timeout/no marker (continue anyway), -1 = FW,ERR.
static int waitForFwCommitting(uint32_t timeout_ms, char* last_seen, size_t last_seen_size) {
    if (last_seen_size) last_seen[0] = '\0';
    char line[100];
    const uint32_t end = millis() + timeout_ms;
    while ((int32_t)(end - millis()) > 0) {
        if (!readSerialLineTimeout(line, sizeof(line), 250)) continue;
        if (line[0] && last_seen_size) {
            strncpy(last_seen, line, last_seen_size - 1);
            last_seen[last_seen_size - 1] = '\0';
        }
        if (strncmp(line, "FW,COMMITTING", 13) == 0) return 1;
        if (strncmp(line, "FW,ERR", 6) == 0) {
            snprintf(ota_err_msg, sizeof(ota_err_msg), "teensy: %s", line);
            ota_state = OTA_S_FAILED;
            ota_modal_dirty = true;
            return -1;
        }
    }
    return 0;
}

static void otaProceedAfterTeensy(const char* msg) {
    ota_need_teensy = false;
    if (ota_need_crowpanel) {
        ota_total_bytes = 0;
        ota_done_bytes  = 0;
        ota_state       = OTA_S_DOWNLOADING;
    } else {
        if (msg && msg[0]) snprintf(ota_err_msg, sizeof(ota_err_msg), "%s", msg);
        ota_state = OTA_S_UPTODATE;
    }
    ota_modal_dirty = true;
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

    // Final preflight. If the Teensy is already at/above target, skip its
    // flash entirely and go straight to the dash update. This is especially
    // important on retry after a false verifier failure.
    queryTeensyVersionBlocking(1500);
    if (strcmp(teensy_fw_version, "?") != 0 &&
        versionCmp(teensy_fw_version, ota_teensy_version) >= 0) {
        otaProceedAfterTeensy("Teensy already current");
        return;
    }

    ota_teensy_commit_seen = false;

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(15);
    HTTPClient http;
    char teensy_url_cb[256];
    otaBustCache(teensy_url_cb, sizeof(teensy_url_cb), ota_teensy_url);
    if (!http.begin(client, teensy_url_cb)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), "teensy hex HTTPS begin failed");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Pragma", "no-cache");
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
    ota_t_dl_total  = (uint32_t)contentLen;
    ota_t_dl_done   = 0;
    ota_t_tx_total  = (uint32_t)contentLen;
    ota_t_tx_done   = 0;

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
            // Modal progress for phase 1: the WiFi download bar.
            if (millis() - last_dl_draw >= 250) {
                last_dl_draw = millis();
                ota_t_dl_done  = dl_done;
                ota_done_bytes = dl_done / 2;  // legacy overall progress
                ota_modal_dirty = true;
                drawOtaModal();
            }
        }
        http.end();
        ota_t_dl_done = (uint32_t)contentLen;
        ota_modal_dirty = true;
        drawOtaModal();
    }

    // Verify the downloaded hex matches the manifest sha256 BEFORE we hand
    // any of it to FlasherX. If GitHub's CDN edge is still serving a stale
    // .hex (we have the fresh manifest already) we'd otherwise flash old code.
    if (ota_teensy_sha256[0]) {
        char actual_hex[65];
        computeSha256Hex(hexbuf, (size_t)contentLen, actual_hex);
        if (!sha256HexEqual(actual_hex, ota_teensy_sha256)) {
            free(hexbuf);
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "teensy hex stale (sha mismatch); retry in ~30s");
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
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
        // Modal progress for phase 2: the ACK-paced UART transfer bar.
        ota_t_tx_done  = pos;
        ota_done_bytes = (uint32_t)contentLen / 2 + pos / 2;  // legacy overall progress
        if (millis() - last_draw_ms >= 400) {
            last_draw_ms = millis();
            ota_modal_dirty = true;
            drawOtaModal();
        }
    }
    free(hexbuf);
    Serial.flush();

    // EOF of the .hex file (':00000001FF') was sent and ACKed. Now wait for
    // FlasherX's explicit commit marker before starting the reboot/version
    // timer. If the marker is missed, continue anyway after 15 s — older
    // firmware or a dropped marker should not brick the process — but don't
    // count those 15 s against the reboot window.
    char commit_last[80];
    const int commit = waitForFwCommitting(15000, commit_last, sizeof(commit_last));
    if (commit < 0) return;
    ota_teensy_commit_seen = (commit == 1);

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
    const uint32_t TEENSY_REBOOT_TIMEOUT_MS = ota_teensy_commit_seen ? 30000UL : 180000UL;
    constexpr uint32_t TEENSY_VER_PING_MS   = 1000;

    // Quiet-after-commit window: the Teensy can take several seconds inside
    // flash_move() to physically copy the staged image into program flash,
    // and we must not transmit on UART during that period. ANY incoming byte
    // can fire a UART RX interrupt that vectors through code which may be
    // mid-rewrite, or simply distract the FlexSPI sequence. After the window
    // we resume sending VER? once per second, hoping the new image is alive
    // and answering.
    constexpr uint32_t QUIET_AFTER_COMMIT_MS = 8000;

    if (wait_start == 0) {
        wait_start   = now;
        last_ping_ms = 0;
        last_draw_ms = 0;
        line_n       = 0;
        last_seen[0] = '\0';
        // Do NOT drain Serial here. We now enter this state only after waiting
        // for FW,COMMITTING, so any bytes already queued may be the new boot's
        // VER,teensy,<target> line. Draining here can discard the exact line
        // we're waiting for.
    }

    const uint32_t elapsed_since_start = now - wait_start;
    const bool in_quiet_window =
        ota_teensy_commit_seen && (elapsed_since_start < QUIET_AFTER_COMMIT_MS);

    // Periodically ask for the version once we're past the quiet window.
    // Inside the quiet window we ONLY listen — no UART tx. This keeps the
    // Teensy fully alone while flash_move() is in progress.
    if (!in_quiet_window &&
        (last_ping_ms == 0 || now - last_ping_ms >= TEENSY_VER_PING_MS)) {
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
        otaProceedAfterTeensy("Teensy verified");
        return;
    }

    const uint32_t elapsed = now - wait_start;
    if (elapsed >= TEENSY_REBOOT_TIMEOUT_MS) {
        wait_start = 0;
        if (ota_teensy_commit_seen) {
            // FlasherX accepted the staged image and entered flash_move(), but
            // we did NOT observe the target VER afterward. Do not fake the
            // version here: field testing showed that can report success even
            // when STATUS later proves the Teensy is still on the old image.
            // If a CrowPanel update is also pending, continue so the dash can
            // still be brought forward; final STATUS will show whether the
            // Teensy actually changed. If this was a Teensy-only update, stop
            // with an honest unverified result.
            if (ota_need_crowpanel) {
                otaProceedAfterTeensy("Teensy commit unverified; updating dash");
                return;
            }
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "teensy commit unverified; STATUS says v%s",
                     teensy_fw_version);
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
        if (teensy_fw_version[0] && strcmp(teensy_fw_version, "?") != 0) {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "teensy still v%s; expected v%s",
                     teensy_fw_version, ota_teensy_version);
        } else if (last_seen[0]) {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "no VER from teensy; last: %.45s", last_seen);
        } else {
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "no VER from teensy after 180s");
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
    char ota_url_cb[256];
    otaBustCache(ota_url_cb, sizeof(ota_url_cb), ota_url);
    if (!http.begin(client, ota_url_cb)) {
        snprintf(ota_err_msg, sizeof(ota_err_msg), ".bin HTTPS begin failed");
        ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
    }
    http.addHeader("Cache-Control", "no-cache");
    http.addHeader("Pragma", "no-cache");
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

    // Verify against the manifest sha256 BEFORE writing to the OTA partition.
    // Without this check, a stale CDN edge that returned an older .bin would
    // be written + booted into, leaving the dash silently at the old version
    // after reboot — exactly the symptom seen in v0.1.19 -> v0.1.20 testing.
    if (ota_sha256[0]) {
        char actual_hex[65];
        computeSha256Hex(binbuf, (size_t)contentLen, actual_hex);
        if (!sha256HexEqual(actual_hex, ota_sha256)) {
            free(binbuf);
            snprintf(ota_err_msg, sizeof(ota_err_msg),
                     "dash bin stale (sha mismatch); retry in ~30s");
            ota_state = OTA_S_FAILED; ota_modal_dirty = true; return;
        }
    }

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
static OtaState  om_last_state       = (OtaState)0xFF;       // sentinel
static uint32_t  om_last_done_kb     = 0xFFFFFFFFu;
static uint32_t  om_last_total_kb    = 0xFFFFFFFFu;
static uint32_t  om_last_tdl_done_kb = 0xFFFFFFFFu;
static uint32_t  om_last_tdl_total_kb= 0xFFFFFFFFu;
static uint32_t  om_last_ttx_done_kb = 0xFFFFFFFFu;
static uint32_t  om_last_ttx_total_kb= 0xFFFFFFFFu;
static char      om_last_status[16]  = "\x01";              // sentinel
static char      om_last_vline[40]   = "\x01";

static void omPaintButton(int x, int w, uint16_t fill, const char* label) {
    tft.fillRect(x, OM_BTN_Y, w, OM_BTN_H, fill);
    tft.drawRect(x, OM_BTN_Y, w, OM_BTN_H, TFT_WHITE);
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextColor(TFT_WHITE, fill);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.drawString(label, x + w/2, OM_BTN_Y + OM_BTN_H/2);
}

static void omResetProgressCache() {
    om_last_done_kb      = 0xFFFFFFFFu;
    om_last_total_kb     = 0xFFFFFFFFu;
    om_last_tdl_done_kb  = 0xFFFFFFFFu;
    om_last_tdl_total_kb = 0xFFFFFFFFu;
    om_last_ttx_done_kb  = 0xFFFFFFFFu;
    om_last_ttx_total_kb = 0xFFFFFFFFu;
}

static void omPaintBarShell(int y, const char* label) {
    tft.setFont(&fonts::Font2);
    tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::top_left);
    tft.setTextColor(TFT_LIGHTGREY, TFT_NAVY);
    tft.setTextPadding(0);
    tft.drawString(label, OM_BAR_X, y - 19);
    tft.drawRect(OM_BAR_X, y, OM_BAR_W, OM_BAR_H, TFT_WHITE);
    tft.fillRect(OM_BAR_X + 2, y + 2, OM_BAR_W - 4, OM_BAR_H - 4, TFT_BLACK);
}

static void omPaintProgressLayout(bool teensy_two_bars, const char* single_label) {
    tft.fillRect(OM_BAR_X - 4, OM_BAR_Y - 24,
                 OM_BAR_W + 8, OM_BTN_Y - (OM_BAR_Y - 24) - 6, TFT_NAVY);
    if (teensy_two_bars) {
        omPaintBarShell(OM_BAR_Y,  "1  Download from GitHub to dash");
        omPaintBarShell(OM_BAR2_Y, "2  Send to Teensy over UART");
    } else if (single_label && single_label[0]) {
        omPaintBarShell(OM_BAR_Y, single_label);
    }
    omResetProgressCache();
}

static void omPaintBarValue(int y, uint32_t done, uint32_t total,
                            bool seconds, uint32_t* last_done_units,
                            uint32_t* last_total_units) {
    if (total == 0) total = 1;
    if (done > total) done = total;
    const uint32_t total_units = seconds ? ((total + 999UL) / 1000UL)
                                         : (total / 1024UL);
    const uint32_t done_units  = seconds ? (done / 1000UL)
                                         : (done / 1024UL);
    if (done_units == *last_done_units && total_units == *last_total_units) return;
    *last_done_units  = done_units;
    *last_total_units = total_units;

    const int fillW = (int)((uint64_t)done * (OM_BAR_W - 4) / total);
    tft.fillRect(OM_BAR_X + 2, y + 2, OM_BAR_W - 4, OM_BAR_H - 4, TFT_BLACK);
    if (fillW > 0) tft.fillRect(OM_BAR_X + 2, y + 2, fillW, OM_BAR_H - 4, TFT_GREEN);

    const int pct = (int)((uint64_t)done * 100 / total);
    char pbline[48];
    if (seconds) {
        snprintf(pbline, sizeof(pbline), "%lu / %lu sec   %d%%",
                 (unsigned long)done_units, (unsigned long)total_units, pct);
    } else {
        snprintf(pbline, sizeof(pbline), "%lu / %lu KB   %d%%",
                 (unsigned long)done_units, (unsigned long)total_units, pct);
    }
    tft.setFont(&fonts::Font2);
    tft.setTextSize(1);
    tft.setTextColor(TFT_WHITE, TFT_NAVY);
    tft.setTextDatum(textdatum_t::middle_right);
    tft.setTextPadding(210);
    tft.drawString(pbline, OM_BAR_X + OM_BAR_W, y - 10);
    tft.setTextPadding(0);
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
        pageJustEntered = false;
        // Force every dynamic block to repaint on this first frame.
        om_last_state      = (OtaState)0xFF;
        omResetProgressCache();
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

    // ---- Progress bars. Teensy update shows separate WiFi download and UART
    // transfer bars; other states use one bar. Re-layout on state transitions.
    const bool om_state_changed = (ota_state != om_last_state);
    const bool show_bar = (ota_state == OTA_S_DOWNLOADING        ||
                           ota_state == OTA_S_TEENSY_DOWNLOADING ||
                           ota_state == OTA_S_TEENSY_WAITING     ||
                           ota_state == OTA_S_APPLYING           ||
                           ota_state == OTA_S_REBOOT);
    if (show_bar) {
        const bool teensy_two_bars   = (ota_state == OTA_S_TEENSY_DOWNLOADING);
        const bool waiting_for_teensy= (ota_state == OTA_S_TEENSY_WAITING);
        if (om_state_changed) {
            const char* single_label = waiting_for_teensy ? "Waiting for Teensy to report version"
                                    : (ota_state == OTA_S_DOWNLOADING) ? "Download dash firmware"
                                    : (ota_state == OTA_S_APPLYING)    ? "Write dash firmware"
                                    : (ota_state == OTA_S_REBOOT)      ? "Restarting dash"
                                    : "Progress";
            omPaintProgressLayout(teensy_two_bars, single_label);
        }

        if (teensy_two_bars) {
            omPaintBarValue(OM_BAR_Y,  ota_t_dl_done, ota_t_dl_total, false,
                            &om_last_tdl_done_kb, &om_last_tdl_total_kb);
            omPaintBarValue(OM_BAR2_Y, ota_t_tx_done, ota_t_tx_total, false,
                            &om_last_ttx_done_kb, &om_last_ttx_total_kb);
        } else {
            omPaintBarValue(OM_BAR_Y, ota_done_bytes, ota_total_bytes,
                            waiting_for_teensy,
                            &om_last_done_kb, &om_last_total_kb);
        }
    } else if (ota_state == OTA_S_FAILED && ota_err_msg[0] && om_state_changed) {
        // Wipe the progress area, draw error message in its place. Done once
        // per state transition into FAILED.
        tft.fillRect(OM_BAR_X - 4, OM_BAR_Y - 24,
                     OM_BAR_W + 8, OM_BTN_Y - (OM_BAR_Y - 24) - 6, TFT_NAVY);
        tft.setFont(&fonts::Font2);
        tft.setTextSize(1);
        tft.setTextColor(TFT_ORANGE, TFT_NAVY);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextPadding(OM_CARD_W - 40);
        tft.drawString(ota_err_msg, OM_CARD_X + OM_CARD_W / 2, OM_BAR_Y + 22);
        tft.setTextPadding(0);
    } else if (om_state_changed) {
        tft.fillRect(OM_BAR_X - 4, OM_BAR_Y - 24,
                     OM_BAR_W + 8, OM_BTN_Y - (OM_BAR_Y - 24) - 6, TFT_NAVY);
        omResetProgressCache();
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
    constexpr int TOOLS_BTN_H = 64,  TOOLS_GAP   = 12;   // 5 buttons since v0.1.116
    constexpr int TOOLS_BTN1_Y = 50;
    constexpr int TOOLS_BTN2_Y = TOOLS_BTN1_Y + TOOLS_BTN_H + TOOLS_GAP;
    constexpr int TOOLS_BTN3_Y = TOOLS_BTN2_Y + TOOLS_BTN_H + TOOLS_GAP;
    constexpr int TOOLS_BTN4_Y = TOOLS_BTN3_Y + TOOLS_BTN_H + TOOLS_GAP;
    constexpr int TOOLS_BTN5_Y = TOOLS_BTN4_Y + TOOLS_BTN_H + TOOLS_GAP;   // WiFi speed test
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

// ---------------------------------------------------------------------------
// WiFi speed test (v0.1.116). POSTs 2 MB of junk from PSRAM straight to the
// server's /nettest — NO Teensy, NO UART, NO session files: it measures the
// dash's radio + TCP/TLS path and NOTHING else. This is the discriminator
// between "upload code broken" and "dash RF starved" (RGB-panel EMI / weak
// AP). Every run is also RECORDED server-side (upload event log, ev=nettest,
// with RSSI + fw) so results can be reviewed later at /admin/upload/log.
// Runs on a core-0 task; the UI just shows nettest_result.
// ---------------------------------------------------------------------------
// (nettest_state now declared up in the zblocks section — zbFree guards on it)
static char nettest_result[80] = "";

static void netTestTask(void*) {
    // TWO passes of 1 MB each (v0.1.117): pass A sends from INTERNAL RAM,
    // pass B from PSRAM. On this board the RGB panel scans the framebuffer
    // out of the same OPI PSRAM bus the WiFi/TLS stack fights for -- if A is
    // much faster than B, PSRAM contention is the dash's throughput ceiling
    // and the uploader's bounce-buffer fix is the cure. Both passes are
    // logged server-side (X-Note: ram=int / ram=psram, X-Tls: connect ms).
    constexpr uint32_t PASS_TOTAL = 1UL * 1024 * 1024;
    constexpr size_t   BLK        = 16384;
    static uint8_t     iblk[BLK];                       // internal RAM (.bss)
    uint8_t* pblk = (uint8_t*)ps_malloc(BLK);           // PSRAM
    const int rssi0 = (int)WiFi.RSSI();
    uint32_t kbps_a = 0, kbps_b = 0;
    bool fail = false;
    char failwhy[40] = "";

    for (int pass = 0; pass < 2 && !fail; ++pass) {
        uint8_t* blk = (pass == 0) ? iblk : pblk;
        if (!blk) { snprintf(failwhy, sizeof(failwhy), "no PSRAM"); fail = true; break; }
        memset(blk, 'x', BLK);
        WiFiClient* c = nullptr;
        if (s.cloud_protocol == 1) {
            WiFiClientSecure* sec = new WiFiClientSecure();
            sec->setInsecure();
            sec->setTimeout(15);
            c = sec;
        } else {
            c = new WiFiClient();
            c->setTimeout(15);
        }
        const uint32_t tc0 = millis();
        if (!c->connect(s.cloud_host, s.cloud_port)) {
            snprintf(failwhy, sizeof(failwhy), "connect failed");
            fail = true;
            c->stop(); delete c;
            break;
        }
        const uint32_t tls_ms = millis() - tc0;
        c->setNoDelay(true);
        c->printf("POST /nettest HTTP/1.1\r\n"
                  "Host: %s\r\n"
                  "Content-Type: application/octet-stream\r\n"
                  "Content-Length: %lu\r\n"
                  "X-Rssi: %d\r\n"
                  "X-Fw: %s\r\n"
                  "X-Note: ram=%s\r\n"
                  "X-Tls: %lu\r\n"
                  "Connection: close\r\n\r\n",
                  s.cloud_host, (unsigned long)PASS_TOTAL, rssi0, FIRMWARE_VERSION,
                  (pass == 0) ? "int" : "psram", (unsigned long)tls_ms);
        const uint32_t t0 = millis();
        uint32_t sent = 0, last = millis();
        while (sent < PASS_TOTAL) {
            size_t want = PASS_TOTAL - sent;
            if (want > BLK) want = BLK;
            const int w = c->write(blk, want);
            if (w > 0) { sent += (uint32_t)w; last = millis(); }
            else if (!c->connected() || millis() - last > 20000) {
                snprintf(failwhy, sizeof(failwhy), "stalled at %luKB (%s)",
                         (unsigned long)(sent / 1024), (pass == 0) ? "int" : "psram");
                fail = true;
                break;
            } else vTaskDelay(1);
        }
        const uint32_t dt = millis() - t0;
        const uint32_t kbps = (uint32_t)(((uint64_t)sent * 1000) / 1024 / (dt ? dt : 1));
        if (pass == 0) kbps_a = kbps; else kbps_b = kbps;
        // Drain the response briefly so the server finishes logging the run.
        const uint32_t rt0 = millis();
        while (millis() - rt0 < 4000) {
            while (c->available()) (void)c->read();
            if (!c->connected() && !c->available()) break;
            vTaskDelay(10);
        }
        c->stop(); delete c;
    }

    // ---- pass C (v0.1.127): EFFECTIVE throughput of the compressed upload
    // path. Synthesizes telemetry-shaped NDJSON (realistic entropy — random
    // data won't compress, constant data compresses absurdly), pushes it
    // through the SAME zdeflate + zblocks framing the uploader uses, and
    // reports RAW bytes/second — i.e. what a session upload will actually
    // achieve on the net hop. Server decodes the frames and logs raw+ratio.
    uint32_t kbps_c = 0, ratio_x10 = 0, czps = 0;
    if (!fail) {
        zbProbeCaps();
        if (srv_zblocks == 1 && zbEnsure()) {
            // Pre-measure PURE compressor speed (no network): 16 blocks of
            // synthetic telemetry, wall-clocked. Reported as czps=<KB/s> in
            // the X-Note so a slow compressor (e.g. buffers stuck in PSRAM)
            // is remotely distinguishable from a slow socket.
            {
                uint32_t lcg0 = 0xC0FFEE42u; float t2 = 0;
                uint32_t raw_b = 0; const uint32_t tz0 = millis();
                for (int blk = 0; blk < 16; ++blk) {
                    uint32_t fill = 0;
                    while (fill + 300 < ZDEF_BLOCK_MAX) {
                        lcg0 = lcg0 * 1664525u + 1013904223u;
                        t2 += 0.04f;
                        fill += (uint32_t)snprintf((char*)zb_raw + fill,
                            ZDEF_BLOCK_MAX - fill,
                            "{\"t\":%.3f,\"lat\":%.6f,\"lon\":%.6f,\"rpm\":%d,"
                            "\"speed_mph\":%.1f,\"ax\":%.3f}\n",
                            (double)(1784000000.0 + t2), (double)(39.2 + t2 * 1e-6),
                            (double)(-77.9 + t2 * 1e-6), (int)(900 + (lcg0 % 6300)),
                            (double)(lcg0 % 140), (double)((int)(lcg0 % 200) - 100) * 0.01);
                    }
                    (void)zdeflate(zb_ws, zb_raw, fill, zb_out, ZDEF_BOUND(ZDEF_BLOCK_MAX));
                    raw_b += fill;
                }
                const uint32_t dtz = millis() - tz0;
                czps = (uint32_t)(((uint64_t)raw_b * 1000) / 1024 / (dtz ? dtz : 1));
            }
            WiFiClient* c = nullptr;
            if (s.cloud_protocol == 1) {
                WiFiClientSecure* sec = new WiFiClientSecure();
                sec->setInsecure();
                sec->setTimeout(15);
                c = sec;
            } else {
                c = new WiFiClient();
                c->setTimeout(15);
            }
            if (c->connect(s.cloud_host, s.cloud_port)) {
                c->setNoDelay(true);
                c->printf("POST /nettest HTTP/1.1\r\nHost: %s\r\n"
                          "Content-Type: application/octet-stream\r\n"
                          "Transfer-Encoding: chunked\r\n"
                          "X-Body-Format: zblocks\r\n"
                          "X-Rssi: %d\r\nX-Fw: %s\r\nX-Note: ram=zb czps=%lu\r\n"
                          "Connection: close\r\n\r\n",
                          s.cloud_host, rssi0, FIRMWARE_VERSION,
                          (unsigned long)czps);
                constexpr uint32_t RAW_TOTAL = 3UL * 1024 * 1024;
                uint32_t raw_sent = 0, wire_sent = 0, lcg = 0x2545F491u;
                float tt = 0, mph = 60, rpm = 3500;
                const uint32_t t0 = millis();
                bool cfail = false;
                while (raw_sent < RAW_TOTAL && !cfail) {
                    // fill one 32 KB block with telemetry-shaped lines
                    uint32_t fill = 0;
                    while (fill + 300 < ZDEF_BLOCK_MAX) {
                        lcg = lcg * 1664525u + 1013904223u;
                        tt += 0.04f;
                        mph += (float)((int)(lcg % 17) - 8) * 0.1f;
                        rpm += (float)((int)((lcg >> 8) % 251) - 125);
                        if (rpm < 900) rpm = 900; if (rpm > 7200) rpm = 7200;
                        fill += (uint32_t)snprintf((char*)zb_raw + fill,
                            ZDEF_BLOCK_MAX - fill,
                            "{\"t\":%.3f,\"fix\":3,\"sats\":12,\"lat\":%.6f,"
                            "\"lon\":%.6f,\"speed_mph\":%.1f,\"heading_deg\":%.1f,"
                            "\"rpm\":%d,\"ax\":%.3f,\"ay\":%.3f,\"az\":1.002}\n",
                            (double)(1784000000.0 + tt), (double)(39.2352 + tt * 1e-6),
                            (double)(-77.9691 + tt * 1.3e-6), (double)mph,
                            (double)((int)(tt * 7) % 360), (int)rpm,
                            (double)((int)(lcg % 200) - 100) * 0.01,
                            (double)((int)((lcg >> 16) % 260) - 130) * 0.01);
                    }
                    const size_t cl = zdeflate(zb_ws, zb_raw, fill, zb_out,
                                               ZDEF_BOUND(ZDEF_BLOCK_MAX));
                    if (!cl) { cfail = true; break; }
                    uint8_t fh[10] = {'Z', 'B'};
                    const uint32_t rl = fill, cl32 = (uint32_t)cl;
                    memcpy(fh + 2, &rl, 4); memcpy(fh + 6, &cl32, 4);
                    char hdr[12];
                    const int hn = snprintf(hdr, sizeof(hdr), "%x\r\n",
                                            (unsigned)(10 + cl));
                    // chunk = frame; write with a stall guard like passes A/B
                    const uint8_t* parts[3] = {(const uint8_t*)hdr, fh, zb_out};
                    const size_t   plen[3]  = {(size_t)hn, 10, cl};
                    for (int p = 0; p < 3 && !cfail; ++p) {
                        size_t offp = 0; uint32_t lastw = millis();
                        while (offp < plen[p]) {
                            const int w = c->write(parts[p] + offp, plen[p] - offp);
                            if (w > 0) { offp += (size_t)w; lastw = millis(); }
                            else if (!c->connected() || millis() - lastw > 20000) { cfail = true; break; }
                            else vTaskDelay(1);
                        }
                    }
                    if (!cfail && c->write((const uint8_t*)"\r\n", 2) != 2) cfail = true;
                    raw_sent  += fill;
                    wire_sent += 10 + (uint32_t)cl + (uint32_t)hn + 2;
                }
                if (!cfail) (void)c->write((const uint8_t*)"0\r\n\r\n", 5);
                const uint32_t dt = millis() - t0;
                if (!cfail && dt) {
                    kbps_c = (uint32_t)(((uint64_t)raw_sent * 1000) / 1024 / dt);
                    if (wire_sent) ratio_x10 = (uint32_t)(((uint64_t)raw_sent * 10) / wire_sent);
                }
                const uint32_t rt0 = millis();
                while (millis() - rt0 < 4000) {
                    while (c->available()) (void)c->read();
                    if (!c->connected() && !c->available()) break;
                    vTaskDelay(10);
                }
            }
            c->stop(); delete c;
        }
    }

    if (pblk) free(pblk);
    if (fail)
        snprintf(nettest_result, sizeof(nettest_result),
                 "FAIL: %s  RSSI %d", failwhy, rssi0);
    else if (kbps_c)
        snprintf(nettest_result, sizeof(nettest_result),
                 "raw %lu/%lu  ZB %lu KB/s (%lu.%lux)  RSSI %d",
                 (unsigned long)kbps_a, (unsigned long)kbps_b,
                 (unsigned long)kbps_c, (unsigned long)(ratio_x10 / 10),
                 (unsigned long)(ratio_x10 % 10), rssi0);
    else
        snprintf(nettest_result, sizeof(nettest_result),
                 "intRAM %lu / PSRAM %lu KB/s  RSSI %d",
                 (unsigned long)kbps_a, (unsigned long)kbps_b, rssi0);
    nettest_state = 2;
    zbFree();   // per-use buffers (state already 2, so the guard lets this run)
    vTaskDelete(NULL);
}

// ---------------------------------------------------------------------------
// Coach checklist page. One row per open item; tap a row to tick it off (it
// POSTs done and vanishes). BACK returns to the dash.
// ---------------------------------------------------------------------------
static constexpr int CO_ROW_Y0 = 96;
static constexpr int CO_ROW_H  = 92;

static void drawCoachPage() {
    coach_dirty = false;
    if (pageJustEntered) {
        tft.fillScreen(TFT_BLACK);
        tft.setFont(&fonts::Font4);
        tft.setTextSize(1);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("COACH CHECKLIST", 400, 30);
        tft.fillRect(0, 56, 800, 1, TFT_DARKGREY);
        tft.setFont(&fonts::Font2);
        tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
        tft.drawString("tap an item to tick it off - it won't come back", 400, 74);
        // BACK button
        tft.fillRect(300, 412, 200, 52, TFT_DARKCYAN);
        tft.drawRect(300, 412, 200, 52, TFT_WHITE);
        tft.setFont(&fonts::Font4);
        tft.setTextColor(TFT_WHITE, TFT_DARKCYAN);
        tft.drawString("BACK", 400, 438);
        tft.setTextDatum(textdatum_t::top_left);
        pageJustEntered = false;
    }
    const int n = coach_n;
    for (int i = 0; i < COACH_MAX; ++i) {
        const int y = CO_ROW_Y0 + i * CO_ROW_H;
        tft.fillRect(20, y, 760, CO_ROW_H - 10, i < n ? TFT_NAVY : TFT_BLACK);
        if (i >= n) continue;
        tft.drawRect(20, y, 760, CO_ROW_H - 10, TFT_WHITE);
        // tick box
        tft.drawRect(36, y + 22, 36, 36, TFT_GREEN);
        tft.drawRect(37, y + 23, 34, 34, TFT_GREEN);
        tft.setFont(&fonts::Font2);
        tft.setTextSize(1);
        tft.setTextDatum(textdatum_t::top_left);
        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        // wrap the item text across up to two lines (~62 chars each)
        const char* t = coach_txt[i];
        const size_t len = strlen(t);
        if (len <= 62) {
            tft.drawString(t, 92, y + 30);
        } else {
            size_t cut = 62;
            while (cut > 30 && t[cut] != ' ') cut--;
            char l1[68];
            size_t c1 = cut < sizeof(l1) ? cut : sizeof(l1) - 1;
            memcpy(l1, t, c1); l1[c1] = '\0';
            tft.drawString(l1, 92, y + 18);
            char l2[68];
            snprintf(l2, sizeof(l2), "%.66s", t + cut + 1);
            tft.drawString(l2, 92, y + 44);
        }
    }
    if (n == 0) {
        tft.setFont(&fonts::Font4);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
        tft.drawString("all clear", 400, CO_ROW_Y0 + 60);
        tft.setTextDatum(textdatum_t::top_left);
    }
}

static void handleCoachTap(int x, int y) {
    if (x >= 300 && x <= 500 && y >= 412 && y <= 464) {
        currentPage     = PAGE_DASH;
        pageJustEntered = true;
        invalidateAll();
        return;
    }
    if (coach_busy) return;                    // a tick is already in flight
    for (int i = 0; i < coach_n; ++i) {
        const int ry = CO_ROW_Y0 + i * CO_ROW_H;
        if (x >= 20 && x <= 780 && y >= ry && y <= ry + CO_ROW_H - 10) {
            coachKick(coach_id[i]);            // POST done; row disappears on success
            coach_dirty = true;
            return;
        }
    }
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
                   TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN1_Y + 22);
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
    tft.drawString(b1sub, TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN1_Y + 47);

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
                   TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN2_Y + 22);
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
    tft.drawString(b2sub, TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN2_Y + 47);

    // ---- Button 3: Test mode ----
    // Generates synthetic GPS/RPM/IMU on the Teensy and writes a real SD
    // session, exercising the SD-write + cloud-upload pipeline without
    // needing real sensors. STOP closes the session, which triggers upload
    // via either Ethernet (when W5500 lands) or WiFi-via-dash.
    const uint16_t b3_fill = test_mode_active ? TFT_DARKGREEN : TFT_NAVY;
    tft.fillRect(TOOLS_BTN_X, TOOLS_BTN3_Y, TOOLS_BTN_W, TOOLS_BTN_H, b3_fill);
    tft.drawRect(TOOLS_BTN_X, TOOLS_BTN3_Y, TOOLS_BTN_W, TOOLS_BTN_H, TFT_WHITE);
    tft.drawRect(TOOLS_BTN_X+1, TOOLS_BTN3_Y+1, TOOLS_BTN_W-2, TOOLS_BTN_H-2, TFT_WHITE);
    tft.setFont(&fonts::Font4);
    tft.setTextColor(TFT_WHITE, b3_fill);
    tft.drawString(test_mode_active ? "Stop test mode" : "Start test mode",
                   TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN3_Y + 22);
    tft.setFont(&fonts::Font2);
    tft.setTextColor(TFT_LIGHTGREY, b3_fill);
    const char* b3sub = test_mode_active
        ? "recording synthetic data — tap to stop + upload"
        : "record synthetic session for cloud upload test";
    tft.drawString(b3sub, TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN3_Y + 47);

    // ---- Button 4: CAN sniffer ----
    // Records every raw CAN frame to /cansniff/ on the SD card so the MS3Pro
    // broadcast layout can be reverse-engineered offline. Needs SD ready.
    const bool sd_ok = (sd_card_status == 2);
    const uint16_t b4_fill = cansniff_active ? TFT_DARKGREEN
                            : sd_ok          ? TFT_NAVY
                                              : TFT_DARKGREY;
    tft.fillRect(TOOLS_BTN_X, TOOLS_BTN4_Y, TOOLS_BTN_W, TOOLS_BTN_H, b4_fill);
    tft.drawRect(TOOLS_BTN_X, TOOLS_BTN4_Y, TOOLS_BTN_W, TOOLS_BTN_H, TFT_WHITE);
    tft.drawRect(TOOLS_BTN_X+1, TOOLS_BTN4_Y+1, TOOLS_BTN_W-2, TOOLS_BTN_H-2, TFT_WHITE);
    tft.setFont(&fonts::Font4);
    tft.setTextColor(TFT_WHITE, b4_fill);
    tft.drawString(cansniff_active ? "Stop CAN capture" : "Start CAN capture",
                   TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN4_Y + 22);
    tft.setFont(&fonts::Font2);
    tft.setTextColor(TFT_LIGHTGREY, b4_fill);
    char b4sub[80];
    if (cansniff_active) {
        snprintf(b4sub, sizeof(b4sub), "capturing — %lu frames — tap to stop",
                 (unsigned long)cansniff_frames);
    } else if (!sd_ok) {
        snprintf(b4sub, sizeof(b4sub), "needs SD card (currently %s)", sdStatusText());
    } else {
        snprintf(b4sub, sizeof(b4sub), "record raw CAN frames to SD for analysis");
    }
    tft.drawString(b4sub, TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN4_Y + 47);

    // ---- Button 5: WiFi speed test (v0.1.116) ----
    {
        const bool wifi_ok = (s.internet_mode == 1 && wifi_state == WS_CONNECTED);
        const bool running = (nettest_state == 1);
        const uint16_t b5_fill = running ? TFT_DARKGREY : (wifi_ok ? TFT_NAVY : TFT_DARKGREY);
        tft.fillRect(TOOLS_BTN_X, TOOLS_BTN5_Y, TOOLS_BTN_W, TOOLS_BTN_H, b5_fill);
        tft.drawRect(TOOLS_BTN_X, TOOLS_BTN5_Y, TOOLS_BTN_W, TOOLS_BTN_H, TFT_WHITE);
        tft.drawRect(TOOLS_BTN_X+1, TOOLS_BTN5_Y+1, TOOLS_BTN_W-2, TOOLS_BTN_H-2, TFT_WHITE);
        tft.setFont(&fonts::Font4);
        tft.setTextColor(TFT_WHITE, b5_fill);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString("WiFi speed test", TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN5_Y + 22);
        tft.setFont(&fonts::Font2);
        tft.setTextColor(TFT_LIGHTGREY, b5_fill);
        char b5sub[96];
        if (running)                    snprintf(b5sub, sizeof(b5sub), "testing... (up to 30 s)");
        else if (nettest_result[0])     snprintf(b5sub, sizeof(b5sub), "%s", nettest_result);
        else if (!wifi_ok)              snprintf(b5sub, sizeof(b5sub), "requires WiFi connected");
        else                            snprintf(b5sub, sizeof(b5sub),
                                                 "2MB dash->server, no Teensy - result is logged server-side");
        tft.drawString(b5sub, TOOLS_BTN_X + TOOLS_BTN_W / 2, TOOLS_BTN5_Y + 47);
        tft.setTextDatum(textdatum_t::top_left);
    }

    // ---- CAN health readout (live, from CANDIAG lines) ----
    // One line that tells you at a glance which CAN failure mode you're in:
    //   fresh + ~50fps + dup<10% + no ACK err  -> HEALTHY (green)
    //   fresh + high dup% / ACK err             -> STORM   (red) — termination/ACK
    //   0 fps / stale                           -> NO BUS  (grey) — wiring/broadcast
    {
        const int CAN_Y = TOOLS_BTN5_Y + TOOLS_BTN_H + 4;   // below button 5 (v0.1.116)
        tft.fillRect(0, CAN_Y - 2, 800, 28, TFT_BLACK);
        const bool fresh = (candiag_ms != 0) && (millis() - candiag_ms < 3000);
        char line[96]; uint16_t col;
        // Classify by FRAME RATE, not dup%. A real no-ACK retransmit storm runs
        // the bus flat-out (~3000+ fps). A HEALTHY 50 Hz broadcast at idle also
        // shows ~100% dup (engine state barely changes in 20 ms) — that is NOT a
        // storm, so dup% must never trigger the storm label on its own.
        //   0 fps / stale         -> NO BUS
        //   > 600 fps             -> STORM (genuine retransmit; ACK not reaching MS3)
        //   1..600 fps            -> OK (normal broadcast; 50/sec per enabled group)
        // PLAIN-ENGLISH VERDICT. Classify by frame RATE (real storm ~3000+ fps;
        // healthy ~50 fps) and use the Teensy's RX error counter to say WHO is at
        // fault, so nobody has to interpret raw numbers:
        //   storm + RXe high  -> our ACK isn't reaching the bus (Teensy TX path)
        //   storm + RXe ~0    -> we're not erroring; the MS3/bus side is the issue
        if (!fresh || candiag_fps == 0) {
            col = TFT_DARKGREY;
            snprintf(line, sizeof(line), "CAN: NO FRAMES (0 fps) - transceiver power/wiring or MS3 broadcast off");
        } else if (candiag_fps > 600) {
            col = TFT_RED;
            if (candiag_rx_err > 64) {
                snprintf(line, sizeof(line),
                    "STORM %lu fps - TEENSY ACK NOT REACHING BUS (RXe%u). Check pin22->transceiver TX + Rs->GND",
                    (unsigned long)candiag_fps, candiag_rx_err);
            } else {
                // RXe~0 + storm. Use the Teensy TX self-test result to say
                // definitively whether OUR transmit/ACK path is alive:
                //   TX FAIL -> Teensy literally can't put a frame on the bus
                //              (transceiver TX / pin22 / Rs) - hardware, not fw
                //   TX PASS -> our TX works yet MS3 still storms -> MS3 side
                const char* tx = candiag_txtest == 1 ? "TXtest=PASS"
                               : candiag_txtest == 2 ? "TXtest=FAIL" : "TXtest...";
                if (candiag_txtest == 2) {
                    snprintf(line, sizeof(line),
                        "STORM %lu fps - %s: Teensy CANNOT TRANSMIT -> transceiver TX/pin22/Rs (HW)",
                        (unsigned long)candiag_fps, tx);
                } else {
                    snprintf(line, sizeof(line),
                        "STORM %lu fps - %s  RXe%u  Tfw=%s",
                        (unsigned long)candiag_fps, tx, candiag_rx_err, teensy_fw_version);
                }
            }
        } else {
            col = TFT_GREEN;
            snprintf(line, sizeof(line), "CAN OK - %lu fps live  (TXe%u RXe%u)",
                     (unsigned long)candiag_fps, candiag_tx_err, candiag_rx_err);
        }
        tft.setFont(&fonts::Font2);
        tft.setTextColor(col, TFT_BLACK);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.drawString(line, 400, CAN_Y + 10);
    }
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
    // Button 3: Test mode toggle
    if (x >= TOOLS_BTN_X && x <= TOOLS_BTN_X + TOOLS_BTN_W &&
        y >= TOOLS_BTN3_Y && y <= TOOLS_BTN3_Y + TOOLS_BTN_H) {
        Serial.printf(test_mode_active ? "TESTSTOP\n" : "TESTSTART\n");
        // Optimistically flip the local state so the button re-paints
        // immediately; the Teensy TEST,<0|1> reply will reconcile.
        test_mode_active = !test_mode_active;
        return;
    }
    // Button 5: WiFi speed test (v0.1.116)
    if (x >= TOOLS_BTN_X && x <= TOOLS_BTN_X + TOOLS_BTN_W &&
        y >= TOOLS_BTN5_Y && y <= TOOLS_BTN5_Y + TOOLS_BTN_H) {
        if (nettest_state == 1) return;                       // already running
        if (s.internet_mode != 1 || wifi_state != WS_CONNECTED) return;
        if (uf.state != UF_IDLE) return;                      // don't fight an upload
        nettest_state = 1;
        nettest_result[0] = '\0';
        if (xTaskCreatePinnedToCore(netTestTask, "nettest", 12288, nullptr, 1,
                                    nullptr, 0) != pdPASS) {
            nettest_state = 2;
            snprintf(nettest_result, sizeof(nettest_result), "task spawn failed");
        }
        return;
    }
    // Button 4: CAN sniffer toggle
    if (x >= TOOLS_BTN_X && x <= TOOLS_BTN_X + TOOLS_BTN_W &&
        y >= TOOLS_BTN4_Y && y <= TOOLS_BTN4_Y + TOOLS_BTN_H) {
        if (!cansniff_active && sd_card_status != 2) return;  // need SD to start
        Serial.printf(cansniff_active ? "CANSNIFF,0\n" : "CANSNIFF,1\n");
        // Teensy replies with CANSNIFF,<state>,... to reconcile; flip locally
        // for instant button feedback.
        cansniff_active = !cansniff_active;
        pageJustEntered = true;
        return;
    }
}

// ---------------------------------------------------------------------------
// PAGE_SESSIONS — list of NDJSON files queued for upload, with per-row
// checkboxes and Delete / Delete All footer buttons.
//
// Data comes from the Teensy via the existing Q,* protocol (Q,LIST emits
// one Q,FILE,<name>,<size> per file, then Q,END). Delete actions send
// Q,DEL,<name>; the Teensy responds Q,DEL,OK or Q,DEL,FAIL,<reason>. After a
// delete batch finishes, the dash re-issues Q,LIST to refresh.
// ---------------------------------------------------------------------------
namespace {
    constexpr int SES_HEAD_Y       = 0;
    constexpr int SES_HEAD_H       = 42;
    constexpr int SES_BODY_TOP     = 52;
    constexpr int SES_BODY_BOTTOM  = 400;   // leaves room for footer
    constexpr int SES_ROW_H        = 36;
    constexpr int SES_ROW_PAD_Y    = 4;
    constexpr int SES_CB_X         = 16;    // checkbox left
    constexpr int SES_CB_SIZE      = 22;    // checkbox edge length
    constexpr int SES_NAME_X       = 54;    // filename left
    constexpr int SES_SIZE_X       = 670;   // right-aligned size
    constexpr int SES_FOOT_Y       = 408;
    constexpr int SES_FOOT_H       = 64;
    constexpr int SES_BTN_UP_X     = 16;    // Upload (n) — selected files only
    constexpr int SES_BTN_DEL_X    = 280;
    constexpr int SES_BTN_DELALL_X = 544;
    constexpr int SES_BTN_W        = 240;
    constexpr int SES_BTN_H        = SES_FOOT_H;
}

static int sessions_selected_count() {
    int n = 0;
    for (int i = 0; i < sl.count; ++i) if (sl.selected[i]) n++;
    return n;
}

static void drawSessionsPage() {
    sl.dirty = false;
    constexpr uint16_t BG = TFT_BLACK;
    if (pageJustEntered) {
        tft.fillScreen(BG);
        pageJustEntered = false;
    } else {
        // Wipe just the body and footer band; header repaints below.
        tft.fillRect(0, SES_BODY_TOP, 800, SES_FOOT_Y + SES_FOOT_H - SES_BODY_TOP, BG);
    }

    // Header: title + busy indicator + queue count.
    tft.fillRect(0, SES_HEAD_Y, 800, SES_HEAD_H, BG);
    tft.setFont(&fonts::Font4);
    tft.setTextSize(1);
    tft.setTextDatum(textdatum_t::middle_left);
    tft.setTextColor(TFT_WHITE, BG);
    tft.drawString("SESSIONS", 16, SES_HEAD_Y + SES_HEAD_H / 2);
    tft.setFont(&fonts::Font2);
    tft.setTextDatum(textdatum_t::middle_right);
    tft.setTextColor(TFT_LIGHTGREY, BG);
    char hdr[48];
    if (sl.state == SL_LISTING) {
        snprintf(hdr, sizeof(hdr), "loading...");
    } else if (sl.state == SL_DELETING) {
        snprintf(hdr, sizeof(hdr), "deleting %d / %d", sl.del_done, sl.del_done + sl.del_fail + 1);
    } else {
        snprintf(hdr, sizeof(hdr), "%d in queue", sl.count);
    }
    tft.drawString(hdr, 784, SES_HEAD_Y + SES_HEAD_H / 2);
    tft.fillRect(0, SES_HEAD_Y + SES_HEAD_H - 1, 800, 1, TFT_DARKGREY);

    // Body: scrollable list. Each row gets a checkbox, filename, size.
    tft.setTextDatum(textdatum_t::middle_left);
    tft.setFont(&fonts::Font2);
    if (sl.count == 0) {
        tft.setTextColor(TFT_DARKGREY, BG);
        tft.setTextDatum(textdatum_t::middle_center);
        if (sl.state == SL_LISTING)
            tft.drawString("loading queue...", 400, SES_BODY_TOP + 80);
        else
            tft.drawString("no queued sessions", 400, SES_BODY_TOP + 80);
    } else {
        for (int i = 0; i < sl.count; ++i) {
            const int y = SES_BODY_TOP + SES_ROW_PAD_Y + i * SES_ROW_H - sessions_scroll_y;
            if (y + SES_ROW_H < SES_BODY_TOP || y > SES_BODY_BOTTOM) continue;
            // Checkbox
            tft.drawRect(SES_CB_X, y, SES_CB_SIZE, SES_CB_SIZE, TFT_LIGHTGREY);
            if (sl.selected[i]) {
                tft.fillRect(SES_CB_X + 4, y + 4, SES_CB_SIZE - 8, SES_CB_SIZE - 8, TFT_GREEN);
            }
            // Filename (truncated if too long)
            tft.setTextColor(TFT_WHITE, BG);
            tft.setTextDatum(textdatum_t::middle_left);
            tft.setTextPadding(SES_SIZE_X - SES_NAME_X - 10);
            tft.drawString(sl.files[i], SES_NAME_X, y + SES_CB_SIZE / 2);
            // Size
            tft.setTextColor(TFT_LIGHTGREY, BG);
            tft.setTextDatum(textdatum_t::middle_right);
            char sizebuf[24];
            if (sl.sizes[i] >= 1024 * 1024)
                snprintf(sizebuf, sizeof(sizebuf), "%.1f MB", sl.sizes[i] / 1048576.0f);
            else if (sl.sizes[i] >= 1024)
                snprintf(sizebuf, sizeof(sizebuf), "%.1f KB", sl.sizes[i] / 1024.0f);
            else
                snprintf(sizebuf, sizeof(sizebuf), "%lu B", (unsigned long)sl.sizes[i]);
            tft.setTextPadding(120);
            tft.drawString(sizebuf, SES_SIZE_X + 100, y + SES_CB_SIZE / 2);
            tft.setTextPadding(0);
        }
    }

    // Footer: Upload selected / Delete selected / Delete All.
    const int selected_n = sessions_selected_count();
    const bool up_enabled   = sl.state == SL_IDLE && selected_n > 0;
    const bool del_enabled  = sl.state == SL_IDLE && selected_n > 0;
    const bool dall_enabled = sl.state == SL_IDLE && sl.count > 0;
    const uint16_t up_fill   = up_enabled   ? TFT_DARKGREEN : TFT_DARKGREY;
    const uint16_t del_fill  = del_enabled  ? TFT_MAROON : TFT_DARKGREY;
    const uint16_t dall_fill = dall_enabled ? TFT_RED    : TFT_DARKGREY;
    tft.fillRect(SES_BTN_UP_X, SES_FOOT_Y, SES_BTN_W, SES_BTN_H, up_fill);
    tft.drawRect(SES_BTN_UP_X, SES_FOOT_Y, SES_BTN_W, SES_BTN_H, TFT_WHITE);
    tft.drawRect(SES_BTN_UP_X + 1, SES_FOOT_Y + 1, SES_BTN_W - 2, SES_BTN_H - 2, TFT_WHITE);
    tft.fillRect(SES_BTN_DEL_X, SES_FOOT_Y, SES_BTN_W, SES_BTN_H, del_fill);
    tft.drawRect(SES_BTN_DEL_X, SES_FOOT_Y, SES_BTN_W, SES_BTN_H, TFT_WHITE);
    tft.drawRect(SES_BTN_DEL_X + 1, SES_FOOT_Y + 1, SES_BTN_W - 2, SES_BTN_H - 2, TFT_WHITE);
    tft.fillRect(SES_BTN_DELALL_X, SES_FOOT_Y, SES_BTN_W, SES_BTN_H, dall_fill);
    tft.drawRect(SES_BTN_DELALL_X, SES_FOOT_Y, SES_BTN_W, SES_BTN_H, TFT_WHITE);
    tft.drawRect(SES_BTN_DELALL_X + 1, SES_FOOT_Y + 1, SES_BTN_W - 2, SES_BTN_H - 2, TFT_WHITE);
    tft.setFont(&fonts::Font4);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setTextColor(TFT_WHITE, up_fill);
    char upLabel[40];
    if (selected_n > 0) snprintf(upLabel, sizeof(upLabel), "Upload (%d)", selected_n);
    else                snprintf(upLabel, sizeof(upLabel), "Upload");
    tft.drawString(upLabel, SES_BTN_UP_X + SES_BTN_W / 2, SES_FOOT_Y + SES_BTN_H / 2);
    tft.setTextColor(TFT_WHITE, del_fill);
    char delLabel[40];
    if (selected_n > 0) snprintf(delLabel, sizeof(delLabel), "Delete (%d)", selected_n);
    else                snprintf(delLabel, sizeof(delLabel), "Delete");
    tft.drawString(delLabel, SES_BTN_DEL_X + SES_BTN_W / 2, SES_FOOT_Y + SES_BTN_H / 2);
    tft.setTextColor(TFT_WHITE, dall_fill);
    tft.drawString("Delete all", SES_BTN_DELALL_X + SES_BTN_W / 2, SES_FOOT_Y + SES_BTN_H / 2);
    tft.setTextDatum(textdatum_t::top_left);
}

static void handleSessionsTap(int x, int y) {
    // Footer buttons first.
    if (y >= SES_FOOT_Y && y <= SES_FOOT_Y + SES_FOOT_H) {
        if (x >= SES_BTN_UP_X && x < SES_BTN_UP_X + SES_BTN_W) {
            if (sl.state == SL_IDLE && sessions_selected_count() > 0 && !upload_active) {
                if (net_owner != NET_WIFI) {
                    // BT owns the radio — hand over first (coex), then upload.
                    openUploadModal("waiting for WiFi (BT paused)...", 0);
                    net_pending_upload = true;
                    net_pending_sel    = true;
                    btReleaseRadio();
                } else {
                    openUploadModal("", 0);
                    ufStartSelected();
                }
            }
            return;
        }
        if (x >= SES_BTN_DEL_X && x < SES_BTN_DEL_X + SES_BTN_W) {
            if (sl.state == SL_IDLE && sessions_selected_count() > 0) {
                slStartDelete(/*delete_all=*/false);
            }
            return;
        }
        if (x >= SES_BTN_DELALL_X && x < SES_BTN_DELALL_X + SES_BTN_W) {
            if (sl.state == SL_IDLE && sl.count > 0) {
                slStartDelete(/*delete_all=*/true);
            }
            return;
        }
    }
    // Body rows: tap toggles selection of the row whose box you hit.
    if (y < SES_BODY_TOP || y > SES_BODY_BOTTOM) return;
    if (sl.state != SL_IDLE) return;            // ignore taps mid-delete/list
    const int rel_y = y - SES_BODY_TOP - SES_ROW_PAD_Y + sessions_scroll_y;
    if (rel_y < 0) return;
    const int idx = rel_y / SES_ROW_H;
    if (idx < 0 || idx >= sl.count) return;
    sl.selected[idx] = !sl.selected[idx];
    sl.dirty = true;
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

// Why isn't the lap timer running? The chain has FIVE gates (recording, GPS
// fix, track match, S/F line present, crossing) and any one of them silently
// shows nothing on the dash. This names the one that's blocking, live, on the
// STATUS page — so a dead lap timer is diagnosable in the paddock in seconds
// instead of after the session.
static uint16_t lapTimerStatus(char* buf, size_t n) {
    if (!recording) { snprintf(buf, n, "idle - not recording"); return TFT_DARKGREY; }
    if (g.fix < 2)  { snprintf(buf, n, "WAITING: no GPS fix");  return TFT_YELLOW; }
    const int tIdx = lapTrackIdx();
    if (tIdx < 0 && !sf_unknown.used) {
        snprintf(buf, n, "NO TRACK MATCH + no S/F set");
        return TFT_RED;
    }
    float aLat, aLon, bLat, bLon; bool hasLine;
    effectiveSfLine(tIdx, &aLat, &aLon, &bLat, &bLon, &hasLine);
    const int sIdx = sfStorageIdx(tIdx);
    const char* tn = (sIdx >= 0 && sIdx < N_TRACKS) ? TRACKS[sIdx].name : "unknown trk";
    if (!hasLine) { snprintf(buf, n, "%s: NO S/F LINE", tn); return TFT_RED; }
    if (!lapTimer.active) { snprintf(buf, n, "%s: starting", tn); return TFT_YELLOW; }
    const int dm = (int)(trackDistanceKm(g.lat_deg, g.lon_deg,
                                         (aLat + bLat) * 0.5f,
                                         (aLon + bLon) * 0.5f) * 1000.0f);
    if (!lapTimer.timing_started) {
        snprintf(buf, n, "%s armed - S/F %dm", tn, dm);
        return TFT_CYAN;
    }
    char t[12];
    if (lapTimer.last_lap_ms) formatLapTime(lapTimer.last_lap_ms, t, sizeof(t));
    else                      strncpy(t, "--", sizeof(t));
    snprintf(buf, n, "L%d  last %s  S/F %dm", lapTimer.lap_number, t, dm);
    return TFT_GREEN;
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
        tft.drawString("UART",    15,  64);
        tft.drawString("IP",      15,  81);
        tft.drawString("BT",      15,  98);
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
        tft.drawString("LAP TMR", 15, 358);
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
        tft.drawString(live ? "LIVE" : "STALE", LV, 64);
    }
    // IP
    {
        tft.setTextColor(VAL, BG);
        {   // IP + live WiFi RSSI (dBm) — the "is the paddock WiFi any good"
            // number (v0.1.115; > -70 fine, < -80 = uploads will crawl).
            char ipbuf[40];
            if (wifiConnectedNow())
                snprintf(ipbuf, sizeof(ipbuf), "%s  %ddBm", active_ip, (int)WiFi.RSSI());
            else
                snprintf(ipbuf, sizeof(ipbuf), "%s", active_ip);
            tft.drawString(ipbuf, LV, 81);
        }
    }
    // BT — OBD dongle link (only meaningful with sensor source = Bluetooth)
    {
        char buf[40]; uint16_t col;
        if (s.sensor_type != 2) {
            strncpy(buf, "OFF", sizeof(buf)); col = TFT_DARKGREY;
        } else if (obd::connected()) {
            const char* nm = s.bt_name[0] ? s.bt_name : s.bt_addr;
            if (obd::coolantF_x10() >= 0 && obd::dataFresh()) {
                snprintf(buf, sizeof(buf), "%.14s  %dF", nm,
                         (int)((obd::coolantF_x10() + 5) / 10));
                col = TFT_GREEN;
            } else {
                snprintf(buf, sizeof(buf), "%.14s  no ECU data", nm);
                col = TFT_YELLOW;
            }
        } else if (net_owner != NET_BT) {
            strncpy(buf, "waiting (BT on at REC)", sizeof(buf)); col = TFT_DARKGREY;
        } else {
            snprintf(buf, sizeof(buf), "%.24s...", obd::stateStr());
            col = TFT_YELLOW;
        }
        buf[sizeof(buf) - 1] = '\0';
        tft.setTextColor(col, BG);
        tft.drawString(buf, LV, 98);
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
    {
        // Lap-timer gate diagnostic (v0.1.132) — wide padding: this string is
        // long and spans to just before the right column.
        char buf[48];
        const uint16_t col = lapTimerStatus(buf, sizeof(buf));
        tft.setTextPadding(305);
        tft.setTextColor(col, BG);
        tft.drawString(buf, LV, 358);
        tft.setTextPadding(LPAD);
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
            if (sd_err_hex[0])
                snprintf(buf, sizeof(buf), "No card (err 0x%s)", sd_err_hex);
            else
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

    // START/FINISH row (right column, above the firmware row). v0.1.129:
    // KNOWN track -> passive info only (baked S/F, web-managed via
    // /tools/sfpicker) with the live distance diagnostic. UNKNOWN track ->
    // the SET S/F capture button (the only place on-car capture still
    // exists) + DELETE for the captured slot.
    {
        const int bx = 410, by = 344, bw = 370, bh = 32;
        const int tIdx = lapTrackIdx();          // SELECTED track, not GPS-closest
        const int sIdx = sfStorageIdx(tIdx);     // config's S/F slot (baked coords)
        const bool unknown = (tIdx < 0);
        const bool armed   = unknown && sf_set_armed && (millis() - sf_set_arm_ms < 5000);
        const bool haveMsg = (sf_set_msg[0] != '\0') && (millis() - sf_set_msg_ms < 4000);
        uint16_t fill = haveMsg  ? TFT_DARKGREEN
                      : !unknown ? TFT_BLACK        // passive info, not a button
                      : armed    ? TFT_ORANGE
                                 : TFT_NAVY;
        tft.fillRect(bx, by, bw, bh, fill);
        if (unknown || haveMsg) tft.drawRect(bx, by, bw, bh, TFT_WHITE);
        tft.setFont(&fonts::Font2);
        tft.setTextDatum(textdatum_t::middle_center);
        tft.setTextColor(!unknown && !haveMsg ? TFT_LIGHTGREY : TFT_WHITE, fill);
        char lbl[56];
        if (haveMsg) snprintf(lbl, sizeof(lbl), "%s", sf_set_msg);
        else if (!unknown) {
            // Live distance to the BAKED S/F — the trackside "why aren't laps
            // ticking" diagnostic. If this never gets small while you lap,
            // the baked line is misplaced: fix it in /tools/sfpicker.
            float aLat, aLon, bLat, bLon; bool hasLine;
            effectiveSfLine(tIdx, &aLat, &aLon, &bLat, &bLon, &hasLine);
            const int dm = (int)(trackDistanceKm(g.lat_deg, g.lon_deg, aLat, aLon) * 1000.0f);
            snprintf(lbl, sizeof(lbl), "S/F %s: baked %s, %dm (web-managed)",
                     TRACKS[sIdx].name, hasLine ? "line" : "point", dm);
        }
        else if (armed) snprintf(lbl, sizeof(lbl), "TAP AGAIN: set S/F here");
        else if (sf_unknown.used) {
            const int dm = (int)(trackDistanceKm(g.lat_deg, g.lon_deg,
                                                 sf_unknown.lat, sf_unknown.lon) * 1000.0f);
            snprintf(lbl, sizeof(lbl), "SET S/F - unknown track (set, %dm)", dm);
        }
        else snprintf(lbl, sizeof(lbl), "SET S/F - unknown track (none set)");
        tft.drawString(lbl, bx + bw / 2, by + bh / 2);
        // DELETE S/F — only for the UNKNOWN-track captured slot now (known
        // tracks have nothing on-car to delete; overrides are ignored).
        if (unknown && sf_unknown.used && !armed && !haveMsg) {
            tft.fillRect(180, by, 224, bh, TFT_MAROON);
            tft.drawRect(180, by, 224, bh, TFT_WHITE);
            tft.setTextColor(TFT_WHITE, TFT_MAROON);
            tft.drawString("DELETE S/F", 180 + 112, by + bh / 2);
        } else {
            tft.fillRect(180, by, 224, bh, TFT_BLACK);
        }
        tft.setTextDatum(textdatum_t::top_left);
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

    // HEALTH bar — temps + battery for heat / brownout diagnosis. Full width
    // above the footer. RED if either MCU is hot (>80 C), grey if the Teensy
    // health line has gone stale (link down = the very failure we're chasing).
    {
        char t1[8], t2[8], t3[8], vb[10], hb[120];
        if (isnan(health_teensy_c)) strcpy(t1, "--"); else sprintf(t1, "%.0f", health_teensy_c);
        if (isnan(health_esp_c))    strcpy(t2, "--"); else sprintf(t2, "%.0f", health_esp_c);
        if (isnan(health_mpu_c))    strcpy(t3, "--"); else sprintf(t3, "%.0f", health_mpu_c);
        if (isnan(health_batt_v))   strcpy(vb, "--"); else sprintf(vb, "%.1fV", health_batt_v);
        char rr[40] = "";
        if (teensy_reset_reason[0]) snprintf(rr, sizeof(rr), "   Trst:%s", teensy_reset_reason);
        snprintf(hb, sizeof(hb), "HEALTH   Teensy %sC   Dash %sC   MPU %sC   Batt %s%s", t1, t2, t3, vb, rr);
        const bool hot = (!isnan(health_teensy_c) && health_teensy_c > 80.0f) ||
                         (!isnan(health_esp_c)    && health_esp_c    > 80.0f);
        const bool stale = (nowMs - health_last_hlth_ms > 4000);
        tft.setFont(&fonts::Font2); tft.setTextSize(1);
        tft.setTextDatum(textdatum_t::top_left);
        tft.setTextColor(hot ? TFT_RED : (stale ? TFT_DARKGREY : TFT_GREEN), BG);
        tft.setTextPadding(788);
        tft.drawString(hb, 10, 438);
        tft.setTextPadding(0);
    }
}

static void handleStatusTap(int x, int y) {
    // DELETE S/F button (left of SET): wipe the UNKNOWN-track captured S/F.
    // (Known tracks have no on-car override anymore — web-managed, v0.1.129.)
    if (x >= 180 && x <= 404 && y >= 344 && y <= 376) {
        const int tIdx = lapTrackIdx();   // SELECTED track, not GPS-closest
        if (tIdx < 0 && sf_unknown.used) {
            sf_unknown = SfOverride{};
            saveSfOverrides();
            sendSfToTeensy(-1);
            snprintf(sf_set_msg, sizeof(sf_set_msg), "unknown-track S/F deleted");
            sf_set_msg_ms = millis();
        }
        return;
    }
    // SET START/FINISH button (handled first so it works regardless of the
    // version-match early-return below). Two-tap: first tap arms, second tap
    // within 5 s stores the current GPS position as this track's S/F override.
    if (x >= 410 && x <= 780 && y >= 344 && y <= 376) {
        const int tIdx = lapTrackIdx();   // SELECTED track, not GPS-closest
        if (tIdx >= 0) {
            // v0.1.129: known tracks are web-managed — no on-car capture.
            snprintf(sf_set_msg, sizeof(sf_set_msg), "known track: set S/F on the web");
            sf_set_msg_ms = millis();
            return;
        }
        const bool wasArmed = sf_set_armed && (millis() - sf_set_arm_ms < 5000);
        if (wasArmed) {
            sf_set_armed = false;
            captureSfHere();   // shared with the dash-page SET S/F button
        } else {
            sf_set_armed  = true;
            sf_set_arm_ms = millis();
        }
        return;
    }

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

#if DASH_IS_ADVANCE
// CrowPanel Advance: the GT911 (0x5D) and a backlight/reset coprocessor (0x30)
// share Wire (I2C_NUM_0) on SDA 15 / SCL 16. Backlight + screen activation are
// I2C commands to 0x30 (0 = brightest, 245 = off, 250 = activate touch screen).
static bool advI2cPresent(uint8_t addr) {
    Wire.beginTransmission(addr);
    return Wire.endTransmission() == 0;
}
static void advCoproCmd(uint8_t cmd) {
    Wire.beginTransmission(0x30);
    Wire.write(cmd);
    Wire.endTransmission();
}
#endif

// ---------------------------------------------------------------------------
// Boot — same V3.0 init sequence that worked in PanelTest.ino.
// ---------------------------------------------------------------------------
void setup() {
    // Default ESP32 Serial RX buffer is 256 bytes; at 921 600 baud that's only
    // ~2.7 ms of slack. Bump to 4 KB BEFORE begin() so the bigger buffer is
    // allocated up front — keeps us safe across slow draw frames + WiFi work.
    // Big UART RX buffer. At 921 600 baud the line rate is ~100 KB/s; with a
    // 4 KB buffer any loop spike >40 ms (e.g. a slow draw frame or WiFi work)
    // dropped bytes mid-stream. 32 KB gives ~320 ms of slack — comfortably
    // wider than any loop spike the dash should ever take.
    Serial.setRxBufferSize(32768);
    Serial.begin(921600);
    rxBuf.reserve(UART_LINE_MAX);
    delay(800);
    // Discard whatever landed in the RX buffer during boot (v0.1.136). The ROM
    // bootloader runs UART0 at 115200 while the Teensy is already transmitting
    // at 921600, so the FIFO fills with framing garbage before our begin() —
    // starting the line parser on that means the first lines are junk.
    while (Serial.available()) (void)Serial.read();
    rxBuf = "";
    // We may have just rebooted (power blip / USB unplug) while the Teensy was
    // mid-upload. It would then be stuck in its blocking retransmit loop with
    // no telemetry for up to 120 s. Kill any such zombie stream immediately
    // (v0.1.136) — harmless when there isn't one.
    Serial.printf("Q,ABORT\n");
    Serial.flush();
    Serial.printf("\n=== racecar-35 dash %s boot, firmware v%s ===\n",
                  DASH_BOARD_NAME, FIRMWARE_VERSION);

    // Silence the ESP-IDF WiFi log output — it would otherwise spam Serial
    // (= UART0 = the Teensy bridge) once WiFi.begin() runs. Without this,
    // the Teensy parser sees junk lines and we get a noisy debug log.
    esp_log_level_set("*",       ESP_LOG_NONE);
    esp_log_level_set("wifi",    ESP_LOG_NONE);
    esp_log_level_set("wifi_init", ESP_LOG_NONE);

#if DASH_IS_ADVANCE
    // --- CrowPanel Advance 5" bring-up (Elecrow example V1.2_and_V1.3) ---
    // No PCA9557 and GPIO 38 is an RGB data line here, so none of the Basic
    // reset sequence applies. Touch + backlight live on a 0x30 coprocessor.
    pinMode(19, OUTPUT);                          // vendor strap (left LOW)
    Wire.begin(DASH_TOUCH_SDA, DASH_TOUCH_SCL);   // 15 / 16
    delay(50);
    {
        // Wait (<=3 s) for the coprocessor (0x30) + GT911 (0x5D). If they don't
        // answer, kick them awake (cmd 250 + GPIO 1 toggle) and retry.
        uint32_t t0 = millis();
        while (!(advI2cPresent(0x30) && advI2cPresent(0x5D))) {
            advCoproCmd(250);                     // activate touch screen
            pinMode(1, OUTPUT); digitalWrite(1, LOW);
            delay(120);
            pinMode(1, INPUT);
            delay(100);
            if (millis() - t0 > 3000) {
                Serial.println("Advance coprocessor (0x30/0x5D) not detected; continuing");
                break;
            }
        }
    }

    tft.begin();                                  // RGB only (no PCA9557)
    tft.fillScreen(TFT_BLACK);

    ts.begin(0x5D);                               // GT911 on Wire (15/16), addr 0x5D
    ts.setRotation(DASH_TOUCH_ROTATION);          // verify on first boot; flip if mirrored
    Serial.println("Advance 5\" panel + GT911 (0x5D) init");

    tft.setTextSize(1);
    delay(200);
#else
    // GPIO 38 = GT911 RST. Hold LOW during PCA9557 bring-up.
    pinMode(38, OUTPUT);
    digitalWrite(38, LOW);
    Wire.begin(19, 20);

    // PCA9557 — releases LCD from reset via IO0.
    Out.reset();
    Out.setMode(IO_OUTPUT);
    Out.setState(IO0, IO_LOW);
    Out.setState(IO1, IO_LOW);
    delay(20);
    Out.setState(IO0, IO_HIGH);
    delay(100);
    Out.setMode(IO1, IO_INPUT);
    Serial.println("PCA9557 init ok");

    // RGB display only — LovyanGFX does NOT touch I2C.
    tft.begin();
    tft.fillScreen(TFT_BLACK);

    // GT911 touch via TAMC on Wire (I2C_NUM_0) — vendor V3.0 proven path.
    // Auto-detect address: probe 0x5D then 0x14, init at whichever answers.
    {
        uint8_t a = 0;
        for (uint8_t cand : { (uint8_t)0x5D, (uint8_t)0x14 }) {
            Wire.beginTransmission(cand);
            if (Wire.endTransmission() == 0) { a = cand; break; }
        }
        ts.begin(a ? a : GT911_ADDR1);
        ts.setRotation(DASH_TOUCH_ROTATION);   // raw coords; per-board (flip if mirrored)
        Serial.printf("GT911 touch (TAMC/Wire) init at 0x%02X\n", a ? a : GT911_ADDR1);
    }

    tft.setTextSize(1);                 // explicit default; PanelTest's old size-3 leak made
                                         // the dash's first frame draw labels at size 3.
    delay(200);
#endif

    // Allocate sprite back-buffers for the dash page's tearing-prone regions.
    // Must run after tft.begin() because LGFX_Sprite borrows its color depth
    // and panel reference from the parent device.
    setupDashSprites();

#if DASH_IS_ADVANCE
    // Advance: backlight is an I2C command to the 0x30 coprocessor (0 =
    // brightest). Turned on AFTER sprites are ready so no garbage frame shows.
    advCoproCmd(0);
    Serial.println("backlight on (i2c 0x30)");
#else
    // Basic 7"/5": GPIO 2 PWM on ledc channel 1. Keep ledc attached (don't
    // override with digitalWrite) so applyBrightness() can dim it. Full on
    // during boot; the saved level is applied right after loadSettings().
    ledcSetup(1, 300, 8);
    ledcAttachPin(TFT_BL, 1);
    ledcWrite(1, 0);
    delay(500);
    ledcWrite(1, 255);
    Serial.println("backlight on (pwm)");
#endif

    loadSettings();
    applyBrightness(s.brightness);   // honour the saved brightness on both paths
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

    // BLE crash forensics: the obd PHASE breadcrumb records which BLE phase
    // was active (1=init 2=scan 3=connect 4=connected). If it's still set AND
    // the reset reason is a crash (brownout/panic/watchdog), that BLE phase
    // reset the board: show exactly which one + why, and BLOCK auto-init this
    // boot so a BT-as-saved-source can't boot-loop. A phase left behind by a
    // normal power cycle (reason = power-on / sw reset) is cleared silently —
    // being connected when the ignition goes off is not a crash.
    {
        Preferences p;
        if (p.begin("blediag", false)) {
            uint8_t ph = p.getUChar("ph", 0);
            if (!ph && p.getBool("try", false)) ph = 1;   // legacy 0.1.92 key
            if (ph) {
                esp_reset_reason_t rr = esp_reset_reason();
                const bool crashy = (rr == ESP_RST_PANIC || rr == ESP_RST_INT_WDT ||
                                     rr == ESP_RST_TASK_WDT || rr == ESP_RST_WDT ||
                                     rr == ESP_RST_BROWNOUT);
                if (crashy) {
                    static const char* PH[] = { "?", "init", "scan", "connect", "link" };
                    snprintf(ble_diag, sizeof(ble_diag), "BT %s reset the board: %s",
                             PH[ph <= 4 ? ph : 0], resetReasonStr(rr));
                    obd::setBlocked(true);
                    Serial.printf("[ble] %s -> BLE auto-init BLOCKED this boot\n", ble_diag);
                }
                p.putUChar("ph", 0);
                p.putBool("try", false);
            }
            p.end();
        }
    }
    Serial.printf("[boot] reset reason: %s\n", resetReasonStr(esp_reset_reason()));

    // If Bluetooth is the saved sensor source, bring up the BLE OBD-II client
    // and (re)connect to the paired dongle. All BLE work runs on its own core-0
    // task, so this never blocks the UI here or in loop(). Skipped if a prior
    // BLE init crashed the board (obd::blocked()).
    // NOTE (radio policy): BT is NOT brought up at boot even when it's the
    // saved source — WiFi owns the radio until recording starts (see the
    // NET_* arbiter comment). The dongle connects a few seconds after REC,1.

    pageJustEntered = true;
    // Ask Teensy for its firmware version. If Teensy booted earlier we missed
    // its VER,teensy,... line on its setup(). This nudge prompts a fresh emit.
    Serial.println("VER?");
    Serial.println("dash UI ready — listening on UART0");
}

// 1 Hz: read our ESP32-S3 die temp and report it to the Teensy (DTEMP) so both
// MCUs' temps land in the same health line / .dbg log for heat diagnosis.
static uint32_t dash_health_ms = 0;
static void dashHealthTick() {
    uint32_t now = millis();
    if (now - dash_health_ms < 1000) return;
    dash_health_ms = now;
    health_esp_c = temperatureRead();   // ESP32-S3 internal temp sensor (°C)
    Serial.printf("DTEMP,%.1f\n", health_esp_c);
    // Relay BLE OBD data to the Teensy (coolant source when srctyp==2 + battery
    // volts for HLTH/.dbg) so it lands in the recorded session, not just the UI.
    if (obd::connected() && obd::dataFresh()) {
        Serial.printf("BTD,%d,%d,%d,%d,%d\n",
                      (int)obd::coolantF_x10(), (int)obd::iatF_x10(), (int)obd::voltX10(),
                      (int)obd::tpsX10(), (int)obd::sparkX10());
    }
    // Adopt the dongle's REAL name once connected: pairing from the scan list
    // may have stored "(unnamed)" (name absent from the advertisement); after
    // connect obd reads the GAP Device Name (0x2A00) — persist it so the
    // Sensor page shows a recognisable device from then on.
    if (obd::connected() && obd::connName()[0] &&
        strcmp(obd::connName(), "(unnamed)") != 0 &&
        strcmp(s.bt_name, obd::connName()) != 0) {
        strncpy(s.bt_name, obd::connName(), sizeof(s.bt_name) - 1);
        s.bt_name[sizeof(s.bt_name) - 1] = '\0';
        saveSettings();
        sensor_page_dirty = true;
        Serial.printf("DBG,bt name adopted: %s\n", s.bt_name);
    }
}

void loop() {
    pumpUart();
    uartLinkTick();   // hard-reinit UART0 if the Teensy link goes silent (v0.1.122)
    handleTouch();
    dashHealthTick();  // 1 Hz ESP32 temp -> Teensy (heat diagnostics)
    netOwnerTick();   // WiFi<->BLE radio time-share arbiter (must run before wifiTick)
    wifiTick();   // WiFi state machine + one-shot NTP push to Teensy (1 Hz tick)
    uploadTick(); // Dash-initiated upload state machine (UF_*)

    // Periodically re-push the full config to the Teensy so it recovers its
    // settings after any UART hiccup / Teensy reboot with no user action.
    // Idempotent — the Teensy just re-applies the same values.
    // Re-push config to the Teensy periodically — but NEVER during an upload or
    // a sessions list/delete. The Teensy handles Q,GET/Q,DEL in a blocking
    // per-line-ACK loop; injecting a 12-line CFG burst into that exchange
    // desynced its ACK wait and aborted the transfer (~5 s in = ~67 lines).
    // v0.1.135: this used to fire EVERY 5 SECONDS — a 17-line, ~500-byte CFG
    // burst onto the Teensy link, forever. CLAUDE.md has warned since v0.1.79
    // that a CFG burst mid-ARQ desyncs the Teensy's per-line ACK wait and
    // aborts the transfer; the guard below only checked the DASH's own upload
    // state, which goes back to UF_IDLE the instant an upload fails while the
    // Teensy keeps retransmitting Q,L for up to 120 s. Result: every failed
    // upload got carpet-bombed with CFG until the whole link wedged.
    // Now: push on demand (Teensy reboot -> cfg_resend_req) with a slow 60 s
    // safety net, and only when the link has been Q-SILENT for 10 s.
    // Coach checklist fetch (v0.1.137): once shortly after boot/WiFi-up, again
    // ~25 s after an upload batch (server review is async), then lazily every
    // 5 min. Never while recording — the radio belongs to BT then, and the
    // button is hidden anyway.
    if (s.coach_show && !recording && !coach_busy
        && uf.state == UF_IDLE && sl.state == SL_IDLE && !upload_active) {
        const uint32_t nowc = millis();
        const bool due = (coach_refetch_at_ms != 0 && nowc >= coach_refetch_at_ms)
                      || (coach_last_fetch_ms == 0 && nowc > 15000)
                      || (coach_last_fetch_ms != 0 && nowc - coach_last_fetch_ms >= 300000);
        if (due) {
            coach_refetch_at_ms = 0;
            coachKick(nullptr);
        }
    }

    { static uint32_t lastCfgResend = 0;
      const bool link_busy = (uf.state != UF_IDLE) || (sl.state != SL_IDLE);
      const bool q_quiet   = (q_activity_ms == 0) || (millis() - q_activity_ms >= 10000);
      const bool due       = cfg_resend_req || (millis() - lastCfgResend >= 60000);
      if (due && !link_busy && q_quiet) {
          lastCfgResend  = millis();
          cfg_resend_req = false;
          sendCfgToTeensy();
      } }

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
        // Back to 50 Hz now that the tearing-prone regions are sprite-buffered.
        // The conditional repaint already short-circuits when no value changed,
        // and 50 Hz keeps the alert blink (and any future high-rate animation
        // like an oil-pressure warning flash) smooth.
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
    } else if (currentPage == PAGE_GPS) {
        // Redraw on entry, on any tap (gps_page_dirty), and 1 Hz for live status.
        if (pageJustEntered || gps_page_dirty || now - lastDraw >= 1000) {
            lastDraw = now;
            drawGpsPage();
        }
    } else if (currentPage == PAGE_SENSOR) {
        // 2 Hz refresh so the live BT connection status updates.
        if (pageJustEntered || sensor_page_dirty || now - lastDraw >= 500) {
            lastDraw = now;
            drawSensorPage();
        }
    } else if (currentPage == PAGE_BT_SCAN) {
        // Frequent refresh so the scanning spinner + result list populate live.
        // Drag-scroll sets bt_scan_dirty every touch sample — cap at ~30 Hz so
        // the LCD gets clean scan windows between renders (anti-tearing rule).
        if (pageJustEntered || ((bt_scan_dirty || now - lastDraw >= 400) && now - lastDraw >= 33)) {
            lastDraw = now;
            drawBtScanPage();
        }
    } else if (currentPage == PAGE_PID_SCAN) {
        // Same refresh policy as the BT scan page (results stream in live).
        if (pageJustEntered || ((pid_scan_dirty || now - lastDraw >= 400) && now - lastDraw >= 33)) {
            lastDraw = now;
            drawPidScanPage();
        }
    } else if (currentPage == PAGE_COACH) {
        if (pageJustEntered || coach_dirty) drawCoachPage();
    } else if (currentPage == PAGE_SESSIONS) {
        // On entry, kick a Q,LIST so the page populates from the SD queue.
        if (pageJustEntered) {
            sessionsRequestList();
        }
        if (pageJustEntered || sl.dirty || now - lastDraw >= 250) {
            lastDraw = now;
            drawSessionsPage();
        }
    } else if (currentPage == PAGE_OTA) {
        otaTick();
        if (pageJustEntered || ota_modal_dirty || now - lastDraw >= 250) {
            lastDraw = now;
            drawOtaModal();
        }
    } else if (currentPage == PAGE_UPLOAD) {
        // Cap the modal repaint at ~4 Hz REGARDLESS of the dirty flag. uploadTick
        // marks it dirty constantly (every staged line / posted chunk), and a
        // full RGB modal repaint is slow enough that redrawing per event drags
        // loop() down to ~4 Hz — which throttles the per-line UART ACK and the
        // POST chunks. A progress bar never needs >4 Hz.
        if (pageJustEntered || now - lastDraw >= 250) {
            lastDraw = now;
            upload_modal_dirty = false;
            drawUploadModal();
        }
        // Hold the result banner long enough to actually read it. OK results
        // close after 2 s; FAIL / CANCELLED hold for 10 s so the user can
        // read the failure reason (which can be a long server message like
        // 'FAIL: http 422: {"detail":[...]}').
        if (upload_result_msg[0] != '\0') {
            const bool is_ok = (upload_result_msg[0] == 'O' &&
                                upload_result_msg[1] == 'K' &&
                                (upload_result_msg[2] == '\0' ||
                                 upload_result_msg[2] == ':'));
            const uint32_t hold_ms = is_ok ? 2000UL : 10000UL;
            if ((int32_t)(millis() - upload_last_draw_ms) >= (int32_t)hold_ms) {
                closeUploadModal();
            }
        }
    }
}
