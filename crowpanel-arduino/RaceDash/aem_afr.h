#pragma once
// AEM 30-0300 ONLY. Manual 10-0300, 2017-07-12, "0-5V Analog Output":
// https://documents.holley.com/30-0300.pdf
// White = +, brown = signal reference. Never connect an unscaled 5V signal
// directly to a Teensy ADC. Hardware contract: reviewed 1:2 input scaling,
// external pulldown, filtering/protection and power-off isolation; A6/pin 20.
// Arduino-independent so conversions, wire parsing and logging are host-tested.
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

namespace aemafr {
enum Status : uint8_t { OFF = 0, VALID = 1, NOT_READY = 2, ERROR = 3 };
struct Reading {
    Status status = OFF;
    int afr_x100 = -1;
    int lambda_x10000 = -1;
    int mv = -1;                         // gauge output, BEFORE the divider
};

inline const char* statusName(Status status) {
    switch (status) {
        case OFF: return "off";
        case VALID: return "valid";
        case NOT_READY: return "not_ready";
        default: return "error";
    }
}

inline Reading fromMillivolts(int mv) {
    Reading r;
    r.status = ERROR;
    if (mv < 0 || mv > 6600) return r;
    r.mv = mv;
    if (mv < 500) { r.status = NOT_READY; return r; }
    if (mv > 4500) return r;
    r.status = VALID;
    // Rounded fixed point; use BOTH published formulas, not AFR / 14.7.
    // AFR (gasoline equivalent) = 2.3750 * V + 7.3125
    // Lambda                   = 0.1621 * V + 0.4990
    r.afr_x100 = (2375 * mv + 7312500 + 5000) / 10000;
    r.lambda_x10000 = (1621 * mv + 4990000 + 500) / 1000;
    return r;
}

inline Reading fromAdc(int raw) {
    if (raw < 0 || raw > 4095) return fromMillivolts(-1);
    // 12-bit ADC, nominal 3.300 V reference, hardware gain = 0.500.
    // 5.0 V gauge output -> 2.5 V ADC. No internal GPIO pull resistor.
    return fromMillivolts((raw * 6600 + 2047) / 4095);
}

// AFR,<status>,<afr_x100>,<lambda_x10000>,<gauge_mV>
// Strict bounded integer parsing + internal consistency rejects damaged UART
// lines before they become plausible-looking AFR. Invalid messages don't commit.
inline bool parseFrame(const char* p, Reading& out) {
    if (strncmp(p, "AFR,", 4)) return false;
    p += 4;
    int v[4];
    for (int i = 0; i < 4; ++i) {
        bool neg = (*p == '-');
        if (neg) ++p;
        if (*p < '0' || *p > '9') return false;
        int n = 0, digits = 0;
        while (*p >= '0' && *p <= '9') {
            if (++digits > 5) return false;
            n = n * 10 + (*p++ - '0');
        }
        v[i] = neg ? -n : n;
        if (i < 3) { if (*p++ != ',') return false; }
        else if (*p) return false;
    }
    Reading expected;
    if (v[0] != OFF) expected = fromMillivolts(v[3]);
    if (v[0] != expected.status || v[1] != expected.afr_x100 ||
        v[2] != expected.lambda_x10000 || v[3] != expected.mv) return false;
    out = expected;
    return true;
}

// A complete, trailing-comma NDJSON fragment. Caller MUST check snprintf's
// return before appending anything else. Invalid measurement fields are null.
inline int jsonFragment(char* dst, size_t cap, const Reading& r) {
    char volts[16];
    if (r.mv < 0) snprintf(volts, sizeof(volts), "null");
    else snprintf(volts, sizeof(volts), "%d.%03d", r.mv / 1000, r.mv % 1000);
    if (r.status == VALID) {
        return snprintf(dst, cap,
            "\"afr_source\":\"aem30-0300\",\"afr\":%d.%02d,\"lambda\":%d.%04d,"
            "\"afr_v\":%s,\"afr_status\":\"valid\",",
            r.afr_x100 / 100, r.afr_x100 % 100,
            r.lambda_x10000 / 10000, r.lambda_x10000 % 10000, volts);
    }
    return snprintf(dst, cap,
        "\"afr_source\":\"aem30-0300\",\"afr\":null,\"lambda\":null,"
        "\"afr_v\":%s,\"afr_status\":\"%s\",", volts, statusName(r.status));
}
} // namespace aemafr
