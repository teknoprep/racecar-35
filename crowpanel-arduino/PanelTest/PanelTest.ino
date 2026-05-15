// CrowPanel ESP32-S3 7" V3.0 — minimal panel bring-up test
// Built via arduino-cli with esp32:esp32@2.0.14, NOT PlatformIO
// (PlatformIO's bootloader was boot-looping on this hardware).
//
// V3.0 specifics:
//   - PCA9557 IO expander @ 0x18 holds LCD in reset until IO0 driven high
//   - 15 MHz pixel clock
//   - GPIO 38 driven low at boot
//   - Backlight on GPIO 2 via direct digitalWrite

#include <Wire.h>
#include <PCA9557.h>
#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <lgfx/v1/platforms/esp32s3/Panel_RGB.hpp>
#include <lgfx/v1/platforms/esp32s3/Bus_RGB.hpp>

#define TFT_BL 2

PCA9557 Out;

class LGFX : public lgfx::LGFX_Device {
public:
    lgfx::Bus_RGB     _bus_instance;
    lgfx::Panel_RGB   _panel_instance;
    lgfx::Light_PWM   _light_instance;

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
        setPanel(&_panel_instance);
    }
};

LGFX tft;

void setup() {
    Serial.begin(115200);
    delay(800);
    Serial.println();
    Serial.println("=========================================");
    Serial.println("CrowPanel V3.0 7\" panel test (Arduino-CLI)");
    Serial.println("=========================================");

    Serial.print("[1] GPIO 38 LOW... ");
    pinMode(38, OUTPUT);
    digitalWrite(38, LOW);
    Serial.println("ok");

    Serial.print("[2] Wire.begin(19, 20)... ");
    Wire.begin(19, 20);
    Serial.println("ok");

    Serial.print("[3] PCA9557 reset + outputs + IO0 high... ");
    Out.reset();
    Out.setMode(IO_OUTPUT);
    Out.setState(IO0, IO_LOW);
    Out.setState(IO1, IO_LOW);
    delay(20);
    Out.setState(IO0, IO_HIGH);
    delay(100);
    Out.setMode(IO1, IO_INPUT);
    Serial.println("ok");

    Serial.print("[4] tft.begin()... ");
    tft.begin();
    tft.fillScreen(TFT_BLACK);
    tft.setTextSize(3);
    delay(200);
    Serial.println("ok");

    Serial.print("[5] LEDC + backlight HIGH... ");
    ledcSetup(1, 300, 8);
    ledcAttachPin(TFT_BL, 1);
    ledcWrite(1, 0);
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, LOW);
    delay(500);
    digitalWrite(TFT_BL, HIGH);
    Serial.println("ok");

    Serial.println("[6] Color flash test (5 s)...");
    tft.fillScreen(TFT_BLUE);   delay(800);
    tft.fillScreen(TFT_YELLOW); delay(800);
    tft.fillScreen(TFT_GREEN);  delay(800);
    tft.fillScreen(TFT_RED);    delay(800);
    tft.fillScreen(TFT_WHITE);  delay(800);

    tft.fillScreen(TFT_BLACK);
    tft.fillCircle(100, 100, 50, TFT_YELLOW);
    tft.setCursor(200, 240);
    tft.setTextColor(TFT_WHITE);
    tft.print("CrowPanel V3.0 alive!");

    Serial.println("[7] setup() complete");
}

void loop() {
    static uint32_t last = 0;
    if (millis() - last >= 2000) {
        last = millis();
        Serial.printf("[heartbeat] uptime=%lus, free heap=%u\n",
                      millis() / 1000, (unsigned)ESP.getFreeHeap());
    }
}
