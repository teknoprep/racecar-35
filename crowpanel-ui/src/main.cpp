// racecar-35 dash UI — CrowPanel ESP32-S3 7" 800x480 (DIS08070H, V2.X)
//
// Listens on UART0 (Serial, GPIO43 TX / GPIO44 RX) for GPS lines from
// the Teensy 4.1 and renders the dash on the LCD using LovyanGFX.
//
// LGFX panel config below is copied verbatim from Elecrow's V2.X
// "Lesson 2 Draw GUI with LovyanGFX" gfx_conf.h (CrowPanel_70 branch).
// DO NOT change pin assignments, timings, or freq_write without
// confirming the board revision — V1.X/V2.X are NOT V3.0 and the V3.0
// PCA9557 init does not apply here.
//
// Wire format (from Teensy Serial3, 115200 8N1, '\n'-terminated):
//   GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>

#include <Arduino.h>
#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <lgfx/v1/platforms/esp32s3/Panel_RGB.hpp>
#include <lgfx/v1/platforms/esp32s3/Bus_RGB.hpp>
#include <driver/i2c.h>

// ---------------------------------------------------------------------------
// LovyanGFX driver class — Elecrow V2.X CrowPanel_70 (7" 800x480) verbatim.
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
            cfg.memory_width  = 800;
            cfg.memory_height = 480;
            cfg.panel_width   = 800;
            cfg.panel_height  = 480;
            cfg.offset_x = 0;
            cfg.offset_y = 0;
            _panel_instance.config(cfg);
        }
        {
            auto cfg = _bus_instance.config();
            cfg.panel = &_panel_instance;

            cfg.pin_d0  = GPIO_NUM_15;  // B0
            cfg.pin_d1  = GPIO_NUM_7;   // B1
            cfg.pin_d2  = GPIO_NUM_6;   // B2
            cfg.pin_d3  = GPIO_NUM_5;   // B3
            cfg.pin_d4  = GPIO_NUM_4;   // B4

            cfg.pin_d5  = GPIO_NUM_9;   // G0
            cfg.pin_d6  = GPIO_NUM_46;  // G1
            cfg.pin_d7  = GPIO_NUM_3;   // G2
            cfg.pin_d8  = GPIO_NUM_8;   // G3
            cfg.pin_d9  = GPIO_NUM_16;  // G4
            cfg.pin_d10 = GPIO_NUM_1;   // G5

            cfg.pin_d11 = GPIO_NUM_14;  // R0
            cfg.pin_d12 = GPIO_NUM_21;  // R1
            cfg.pin_d13 = GPIO_NUM_47;  // R2
            cfg.pin_d14 = GPIO_NUM_48;  // R3
            cfg.pin_d15 = GPIO_NUM_45;  // R4

            cfg.pin_henable = GPIO_NUM_41;
            cfg.pin_vsync   = GPIO_NUM_40;
            cfg.pin_hsync   = GPIO_NUM_39;
            cfg.pin_pclk    = GPIO_NUM_0;
            cfg.freq_write  = 12000000;          // 12 MHz — DO NOT change

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
            cfg.x_min      = 0;
            cfg.x_max      = 799;
            cfg.y_min      = 0;
            cfg.y_max      = 479;
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
// GPS state — populated by the UART parser, drawn by the render loop.
// ---------------------------------------------------------------------------
struct GpsState {
    uint8_t  fix      = 0;
    uint8_t  sats     = 0;
    float    lat_deg  = 0.0f;
    float    lon_deg  = 0.0f;
    float    mph      = 0.0f;
    float    hdg_deg  = 0.0f;
    uint32_t last_ms  = 0;
};
static GpsState g;

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

static String rxBuf;
static bool parseLine(const String& line) {
    if (!line.startsWith("GPS,")) return false;
    int idx[7], n = 0;
    for (int i = 0; i < (int)line.length() && n < 7; ++i) {
        if (line[i] == ',') idx[n++] = i;
    }
    if (n < 6) return false;
    idx[6] = line.length();
    auto field = [&](int k) { return line.substring(idx[k] + 1, idx[k + 1]); };
    g.fix     = (uint8_t)field(0).toInt();
    g.sats    = (uint8_t)field(1).toInt();
    g.lat_deg = field(2).toFloat();
    g.lon_deg = field(3).toFloat();
    g.mph     = field(4).toFloat();
    g.hdg_deg = field(5).toFloat();
    g.last_ms = millis();
    return true;
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
// Rendering — direct lcd.* calls, no sprite.
// ---------------------------------------------------------------------------
static void drawStaticChrome() {
    tft.fillScreen(TFT_BLACK);
    tft.setTextColor(TFT_DARKGREY);
    tft.setTextDatum(textdatum_t::top_center);
    tft.setFont(&fonts::Font4);
    tft.drawString("S P E E D", 400, 10);
    tft.setTextColor(TFT_LIGHTGREY);
    tft.drawString("mph", 400, 270);
    tft.drawFastHLine(40, 320, 720, TFT_DARKGREY);
}

static void drawDynamic() {
    // big speed
    tft.fillRect(100, 60, 600, 200, TFT_BLACK);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextDatum(textdatum_t::middle_center);
    tft.setFont(&fonts::Font7);
    tft.setTextSize(2);
    char buf[16]; snprintf(buf, sizeof(buf), "%.1f", g.mph);
    tft.drawString(buf, 400, 160);
    tft.setTextSize(1);

    // middle row: HDG / LAT / LON
    tft.setFont(&fonts::Font4);
    tft.setTextDatum(textdatum_t::top_left);
    auto cell = [](int x, int y, const char* label, const char* value) {
        tft.fillRect(x, y, 250, 60, TFT_BLACK);
        tft.setTextColor(TFT_DARKGREY);
        tft.drawString(label, x, y);
        tft.setTextColor(TFT_WHITE);
        tft.drawString(value, x, y + 26);
    };
    char hdg[16], lat[16], lon[16];
    snprintf(hdg, sizeof(hdg), "%.1f deg", g.hdg_deg);
    snprintf(lat, sizeof(lat), "%.6f", g.lat_deg);
    snprintf(lon, sizeof(lon), "%.6f", g.lon_deg);
    cell(40,  340, "HDG", hdg);
    cell(290, 340, "LAT", lat);
    cell(540, 340, "LON", lon);

    // bottom row: FIX / SATS / LINK
    auto smallCell = [](int x, int y, const char* label, const char* value, uint16_t color) {
        tft.fillRect(x, y, 250, 36, TFT_BLACK);
        tft.setTextColor(TFT_DARKGREY);
        tft.drawString(label, x, y);
        tft.setTextColor(color);
        tft.drawString(value, x + 90, y);
    };
    char satsStr[8]; snprintf(satsStr, sizeof(satsStr), "%u", (unsigned)g.sats);
    uint16_t fixColor = (g.fix >= 3) ? TFT_GREEN : (g.fix >= 2) ? TFT_YELLOW : TFT_RED;
    smallCell(40,  430, "FIX",  fixName(g.fix), fixColor);
    smallCell(290, 430, "SATS", satsStr, TFT_WHITE);
    bool linkOk = (millis() - g.last_ms) < 1500;
    smallCell(540, 430, "LINK", linkOk ? "OK" : "STALE", linkOk ? TFT_GREEN : TFT_RED);
}

// ---------------------------------------------------------------------------
// Arduino entry points — match Elecrow's V2.X order: tft.begin(),
// fillScreen, then everything else. No PCA9557, no GPIO 38, no manual BL.
// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(300);
    Serial.println(F("\n[crowpanel] boot (V2.X config)"));

    tft.begin();
    tft.fillScreen(TFT_BLACK);
    tft.setTextSize(3);
    delay(100);

    // Panel-alive smoke test — same sequence Elecrow uses in their Lesson 2.
    Serial.println(F("[crowpanel] color flash"));
    tft.fillScreen(TFT_BLUE);   delay(600);
    tft.fillScreen(TFT_YELLOW); delay(600);
    tft.fillScreen(TFT_GREEN);  delay(600);
    tft.fillScreen(TFT_BLACK);

    tft.setBrightness(192);

    drawStaticChrome();
    drawDynamic();
    Serial.println(F("[crowpanel] setup done"));
}

void loop() {
    pumpUart();
    static uint32_t lastDraw = 0;
    if (millis() - lastDraw >= 100) {
        lastDraw = millis();
        drawDynamic();
    }
}
