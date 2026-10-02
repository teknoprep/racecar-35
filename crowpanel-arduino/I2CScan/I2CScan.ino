// CrowPanel Advance — I2C bus probe (diagnostic only, not part of the dash firmware).
//
// Purpose: find out whether the panel really carries an RTC chip behind its coin cell,
// and at which address, on which bus. Scans ONLY pins that are safe on the Advance:
//   15/16 = the GT911 touch + 0x30 backlight coprocessor bus (known good)
//   19/20 = not used by the RGB data/control pins on this panel
// The Advance's RGB pins are 21,47,48,45,38,9..14,7,17,18,3,46 + 42,41,40,39, so 19/20
// are safe to probe. We deliberately do NOT scan any RGB pin: driving an LCD data line as
// I2C would feed garbage into the panel.
#include <Wire.h>

static const int SDA_1 = 15, SCL_1 = 16;   // GT911 / 0x30 coprocessor
static const int SDA_2 = 19, SCL_2 = 20;   // spare pair on this panel

static void scan(const char *name, int sda, int scl) {
    Wire.end();
    delay(10);
    Wire.begin(sda, scl, 100000);          // 100 kHz: be kind to unknown devices
    delay(10);
    Serial.printf("bus %-10s (SDA %d / SCL %d):", name, sda, scl);
    int n = 0;
    for (uint8_t a = 1; a < 127; a++) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) {
            Serial.printf(" 0x%02X", a);
            n++;
        }
    }
    if (!n) Serial.print(" (no devices)");
    Serial.println();
}

void setup() {
    Serial.begin(921600);
    delay(400);
    Serial.println("\n=== CrowPanel Advance I2C probe ===");
    scan("touch/bl", SDA_1, SCL_1);
    scan("spare",    SDA_2, SCL_2);
    // Read a PCF8563/RX8025-style register bank if something answered at 0x51/0x32,
    // so a real RTC is distinguished from a same-address impostor.
    Wire.end(); delay(10); Wire.begin(SDA_1, SCL_1, 100000); delay(10);
    for (uint8_t a : {0x51, 0x32, 0x68}) {
        Wire.beginTransmission(a);
        Wire.write((uint8_t)0x02);
        if (Wire.endTransmission(false) != 0) continue;
        if (Wire.requestFrom((int)a, 7) == 7) {
            uint8_t v[7] = {0};
            for (int i = 0; i < 7; i++) v[i] = Wire.read();
            Serial.printf("  0x%02X regs 02..08: %02X %02X %02X %02X %02X %02X %02X\n",
                          a, v[0], v[1], v[2], v[3], v[4], v[5], v[6]);
        }
    }
    Serial.println("=== probe done ===");
}

void loop() { delay(2000); }
