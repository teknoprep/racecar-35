// Boot smoke test — absolute minimum. No LovyanGFX, no Wire, no PSRAM.
// If this prints "alive" once and heartbeats every second, the chip
// is booting and Serial routing works. Then we add things back layer
// by layer until we find what makes it crash.

#include <Arduino.h>

void setup() {
    Serial.begin(115200);
    delay(800);
    Serial.println();
    Serial.println(F("[smoke] alive — minimal boot test, no PSRAM, no LCD"));
    Serial.printf("[smoke] CPU freq = %d MHz\n", (int)getCpuFrequencyMhz());
    Serial.printf("[smoke] free heap = %u bytes\n", (unsigned)ESP.getFreeHeap());
    Serial.printf("[smoke] free PSRAM = %u bytes\n", (unsigned)ESP.getFreePsram());
    Serial.printf("[smoke] flash chip size = %u bytes\n", (unsigned)ESP.getFlashChipSize());
    Serial.printf("[smoke] sketch size = %u bytes\n", (unsigned)ESP.getSketchSize());
}

void loop() {
    static uint32_t last = 0;
    if (millis() - last >= 1000) {
        last = millis();
        Serial.printf("[smoke] uptime=%lus\n", millis() / 1000);
    }
}
