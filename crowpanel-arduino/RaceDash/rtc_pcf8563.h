// PCF8563-compatible RTC on the CrowPanel Advance's touch I2C bus (SDA 15 / SCL 16).
//
// Found by probing the actual panel, not from a datasheet guess: an I2C scan of that bus
// answers 0x30 (backlight coprocessor), 0x5D (GT911 touch) and **0x51** — and 0x51's
// register bank 0x02..0x08 decodes as BCD seconds/minutes/hours/dow/date/month/year with
// bit 7 of 0x02 acting as the VL "voltage low / time not valid" flag. That is the
// PCF8563/BM8563/HM8563 map. The Advance carries a coin cell for it.
//
// Why this exists: the panel should know the time WITHOUT NTP (at the track there is no
// WiFi). NTP or the Teensy's GPS-derived time seeds it; afterwards the coin cell keeps it
// across power cycles, and the dash re-broadcasts it to the Teensy via SETTIME so both
// sides agree without a network.
//
// Everything here is synchronous and called from the loop/setup on the same task that
// drives the GT911, so there is no bus concurrency to arbitrate.
#pragma once

#include <Wire.h>
#include <time.h>
#include <sys/time.h>

#define RTC_PCF8563_ADDR   0x51
#define RTC_EPOCH_MIN      1700000000UL   // 2023-11-14: anything below is "not set"

static bool rtc_present      = false;   // ACKed at 0x51 and looks like BCD
static bool rtc_time_valid   = false;   // VL clear and the year is sane
static uint32_t rtc_boot_epoch = 0;     // what we restored at boot (0 = nothing)

static inline uint8_t rtcBcd2Bin(uint8_t v) { return (uint8_t)((v & 0x0F) + ((v >> 4) * 10)); }

// UTC broken-down -> unix epoch, without timegm() (not declared on this newlib).
// Howard Hinnant's days_from_civil: exact for all years we care about.
static time_t rtcEpochFromUtc(int year, int mon, int mday, int hour, int min, int sec) {
    int y = year - (mon <= 2);
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);
    const unsigned doy = (153u * (unsigned)(mon + (mon > 2 ? -3 : 9)) + 2u) / 5u + (unsigned)mday - 1u;
    const unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    const long long days = (long long)era * 146097LL + (long long)doe - 719468LL;
    return (time_t)(days * 86400LL + hour * 3600LL + min * 60LL + sec);
}
static inline uint8_t rtcBin2Bcd(uint8_t v) { return (uint8_t)(((v / 10) << 4) | (v % 10)); }

// Probe: ACK at 0x51 AND the seconds register must look like BCD. That second test
// matters because 0x51 is also used by some EEPROMs/temperature sensors.
static bool rtcProbe() {
    Wire.beginTransmission(RTC_PCF8563_ADDR);
    if (Wire.endTransmission() != 0) return false;
    Wire.beginTransmission(RTC_PCF8563_ADDR);
    Wire.write((uint8_t)0x02);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((uint8_t)RTC_PCF8563_ADDR, (uint8_t)1) != 1) return false;
    const uint8_t s = Wire.read() & 0x7F;
    return ((s >> 4) <= 9) && ((s & 0x0F) <= 9);
}

// Read 0x02..0x08. Sets *vl when the chip reports "voltage low" (time untrustworthy).
static bool rtcRead(time_t *out, bool *vl) {
    Wire.beginTransmission(RTC_PCF8563_ADDR);
    Wire.write((uint8_t)0x02);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((uint8_t)RTC_PCF8563_ADDR, (uint8_t)7) != 7) return false;
    uint8_t r[7];
    for (int i = 0; i < 7; i++) r[i] = Wire.read();

    *vl = (r[0] & 0x80) != 0;                       // VL: 1 = cell has dipped / time invalid
    const int sec  = rtcBcd2Bin(r[0] & 0x7F);
    const int min  = rtcBcd2Bin(r[1] & 0x7F);
    const int hour = rtcBcd2Bin(r[2] & 0x3F);
    const int mday = rtcBcd2Bin(r[4] & 0x3F);
    const int mon  = rtcBcd2Bin(r[5] & 0x1F);
    const int year = 2000 + rtcBcd2Bin(r[6]);       // we only ever write 20xx
    if (sec > 59 || min > 59 || hour > 23 || mday < 1 || mday > 31 || mon < 1 || mon > 12)
        return false;                               // garbage: treat as no time

    *out = rtcEpochFromUtc(year, mon, mday, hour, min, sec);
    return true;
}

// Write UTC into 0x02..0x08. Writing the seconds register also clears VL, so a
// successful set is what marks the clock trustworthy from then on.
static bool rtcWrite(time_t t) {
    struct tm *g = gmtime(&t);
    if (!g) return false;
    const int year = g->tm_year + 1900;
    if (year < 2023 || year > 2099) return false;
    Wire.beginTransmission(RTC_PCF8563_ADDR);
    Wire.write((uint8_t)0x02);
    Wire.write(rtcBin2Bcd(g->tm_sec)  & 0x7F);      // also clears VL
    Wire.write(rtcBin2Bcd(g->tm_min));
    Wire.write(rtcBin2Bcd(g->tm_hour));
    Wire.write(rtcBin2Bcd((uint8_t)g->tm_wday));    // 0..6, unused by us
    Wire.write(rtcBin2Bcd(g->tm_mday));
    Wire.write(rtcBin2Bcd(g->tm_mon + 1));
    Wire.write(rtcBin2Bcd((uint8_t)(year % 100)));
    if (Wire.endTransmission() != 0) return false;
    rtc_time_valid = true;
    return true;
}

static void rtcBegin() {
    rtc_present = rtcProbe();
    if (!rtc_present) {
        Serial.println("[rtc] no PCF8563 at 0x51 (no coin cell fitted, or different panel)");
        return;
    }
    time_t rt = 0; bool vl = true;
    if (rtcRead(&rt, &vl) && !vl && (uint32_t)rt > RTC_EPOCH_MIN) {
        struct timeval tv = { rt, 0 };
        settimeofday(&tv, nullptr);
        rtc_time_valid   = true;
        rtc_boot_epoch   = (uint32_t)rt;
        Serial.printf("[rtc] PCF8563 0x51: time RESTORED %lu (VL clear) - no NTP needed\n",
                      (unsigned long)rt);
    } else {
        Serial.printf("[rtc] PCF8563 0x51 present but time INVALID (VL=%d) - waiting for NTP/GPS\n",
                      (int)vl);
    }
}
