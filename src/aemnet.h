// AEMnet decoder - AEM's CAN protocol. Header-only, no Arduino dependencies, so the SAME
// code runs in the Teensy firmware and in the host test (tests/test_aemnet.py).
//
// Sources (AEM's own documents, fetched 2026-10-20):
//   * "TA2 Infinity AEMnet Configuration", Rev. A 2016-02-19, data stream "AEMnet v150609"
//     https://documents.aemelectronics.com/techlibrary_ta2_infinity_aemnet_configuration.pdf
//   * 30-0300 X-Series UEGO gauge instructions, doc 10-0300 (2017-07-12), "AEMnet (CAN Bus)
//     Output" - the gauge this car already carries.  https://documents.holley.com/30-0300.pdf
//
// Every AEMnet frame: CAN 2.0B, 29-bit EXTENDED identifier, 500 kbit/s, DLC 8, multi-byte
// fields big-endian (Motorola, MSB first). A 29-bit id is NEVER the same frame as an 11-bit
// id with the same number - callers must check the extended flag (pumpCAN does).
//
// Frames decoded (field layouts copied from the documents; SI scaling column):
//   0x01F0A000  ECU, 50 Hz  [0:2] Engine Speed u16 0.39063 rpm/bit
//                            [4:6] Throttle     u16 0.0015259 %/bit
//                            [6]   Intake Air Temp s8 1 degC/bit   [7] Coolant Temp s8 1 degC/bit
//   0x01F0A003  ECU, 50 Hz  [0] Lambda #1 u8 0.00390625/bit +0.5   [5] Ign Timing u8 0.35156 deg/bit -17
//                            [6:8] Battery Volts u16 0.0002455 V/bit
//   0x01F0A004  ECU, 50 Hz  [0:2] MAP u16 0.1 kPa/bit   [4] OilPressure u8 0.580151 PSIg/bit
//               (Infinity V96.1 and later)
//   0x00000180..0x0000018F  X-Series UEGO gauge "ID 1".."ID 16", 100 Hz
//                            [0:2] Lambda u16 0.0001/bit   [2:4] Oxygen s16 0.001 %/bit
//                            [4] System Volts u8 0.1 V/bit
//                            [6] bit1 (0x02) Bosch LSU4.9 detected, bit5 (0x20) free-air cal in
//                                use, bit7 (0x80) Lambda Data Valid
//                            [7] bit6 (0x40) Sensor Fault
//
// AFR = lambda x 14.65 - AEM's gasoline constant (30-0300 spec table "Stoichiometric Constant
// 14.65"; the TA2 US AFR scaling 0.057227/bit = 0.00390625 x 14.65), the same convention the
// analogue AEM input (aem_afr.h) already uses.
//
// All outputs are the logger's usual fixed point (x10 of the natural unit, lambda x10000),
// -1 = not received / expired. Temperatures come from whole degC, so degF x10 = 18*C + 320
// can never equal the -1 sentinel.
#pragma once
#include <stdint.h>

namespace aemnet {

constexpr uint32_t ID_ENGINE   = 0x01F0A000UL;   // rpm, throttle, IAT, coolant
constexpr uint32_t ID_LAMBDA   = 0x01F0A003UL;   // lambda 1/2, speed, gear, timing, battery
constexpr uint32_t ID_PRESSURE = 0x01F0A004UL;   // MAP, VE, fuel/oil pressure, flags
constexpr uint32_t ID_UEGO_1   = 0x00000180UL;   // X-Series UEGO gauge, menu "ID 1"
constexpr uint32_t ID_UEGO_16  = 0x0000018FUL;   //                       ...   "ID 16"
constexpr int32_t  STOICH_X100 = 1465;           // AEM gasoline constant 14.65

// UEGO byte 6 / byte 7 flag bits (30-0300 manual table).
constexpr uint8_t UEGO_LSU49     = 0x02;
constexpr uint8_t UEGO_FREE_AIR  = 0x20;
constexpr uint8_t UEGO_VALID     = 0x80;
constexpr uint8_t UEGO_FAULT     = 0x40;          // byte 7

// UEGO status, as carried on the wire / in the log.
enum UegoStatus : uint8_t { UEGO_NONE = 0, UEGO_OK = 1, UEGO_NOT_READY = 2, UEGO_FAULTED = 3 };

struct State {
    // ECU stream (an AEM Infinity / EMS V2 / EMS-4)
    int32_t  rpm            = -1;   // rpm
    int16_t  tps_x10        = -1;   // % x10
    int16_t  iat_f_x10      = -1;   // degF x10
    int16_t  clt_f_x10      = -1;   // degF x10
    int16_t  lambda1_x10000 = -1;   // Lambda #1 x10000 (0.5000 .. 1.4961)
    int16_t  adv_x10        = -1000;// ignition timing deg x10 (can be negative; -1000 = none)
    int16_t  bat_x10        = -1;   // V x10
    int16_t  map_x10        = -1;   // kPa x10
    int16_t  oil_x10        = -1;   // psi x10
    uint32_t engine_ms      = 0;    // millis() of the last frame of each group (0 = never)
    uint32_t lambda_ms      = 0;
    uint32_t pressure_ms    = 0;
    // X-Series UEGO gauge
    int32_t  uego_lambda_x10000 = -1;
    int16_t  uego_o2_x1000      = 0;    // oxygen % x1000 (signed)
    int16_t  uego_sysv_x10      = -1;   // gauge supply V x10
    uint8_t  uego_flags6        = 0;
    uint8_t  uego_flags7        = 0;
    uint8_t  uego_index         = 0;    // 1..16 (the gauge's menu ID), 0 = none seen
    uint32_t uego_ms            = 0;
};

inline uint16_t be16(const uint8_t* b) { return (uint16_t)(((uint16_t)b[0] << 8) | b[1]); }
inline int32_t  roundDiv(int64_t num, int64_t den) {      // round half away from zero
    return (int32_t)(num >= 0 ? (num + den / 2) / den : -((-num + den / 2) / den));
}

inline bool isUegoId(uint32_t id) { return id >= ID_UEGO_1 && id <= ID_UEGO_16; }
inline bool isAemnetId(uint32_t id) {
    return id == ID_ENGINE || id == ID_LAMBDA || id == ID_PRESSURE || isUegoId(id);
}

// Fixed-point conversions, all exact integer maths (the scales are AEM's, expressed as
// rationals so host and target produce bit-identical results):
inline int32_t rpmFromRaw(uint16_t raw)        { return roundDiv((int64_t)raw * 39063, 100000); }
inline int16_t tpsX10FromRaw(uint16_t raw)     { return (int16_t)roundDiv((int64_t)raw * 15259, 1000000); }
inline int16_t tempFX10FromRaw(uint8_t raw)    { return (int16_t)(18 * (int8_t)raw + 320); }
inline int16_t lambda1X10000FromRaw(uint8_t r) { return (int16_t)(5000 + roundDiv((int64_t)r * 390625, 10000)); }
inline int16_t advX10FromRaw(uint8_t raw)      { return (int16_t)(roundDiv((int64_t)raw * 35156, 10000) - 170); }
inline int16_t batX10FromRaw(uint16_t raw)     { return (int16_t)roundDiv((int64_t)raw * 2455, 1000000); }
inline int16_t mapX10FromRaw(uint16_t raw)     { return (int16_t)(raw > 32767 ? 32767 : raw); }
inline int16_t oilX10FromRaw(uint8_t raw)      { return (int16_t)roundDiv((int64_t)raw * 580151, 100000); }
inline int16_t afrX10FromLambda(int32_t lx10000) {
    return lx10000 < 0 ? (int16_t)-1 : (int16_t)roundDiv((int64_t)lx10000 * STOICH_X100, 100000);
}

// Decode one received frame. `extended` MUST be the frame's IDE flag: only 29-bit frames are
// AEMnet. Returns true when the frame was an AEMnet frame we use (and updated `s`).
inline bool decode(uint32_t id, bool extended, const uint8_t* b, uint8_t len, State& s, uint32_t now_ms) {
    if (!extended || len < 8 || !isAemnetId(id)) return false;
    if (now_ms == 0) now_ms = 1;                       // 0 means "never" in the timestamps
    if (id == ID_ENGINE) {
        s.rpm       = rpmFromRaw(be16(b + 0));
        s.tps_x10   = tpsX10FromRaw(be16(b + 4));
        s.iat_f_x10 = tempFX10FromRaw(b[6]);
        s.clt_f_x10 = tempFX10FromRaw(b[7]);
        s.engine_ms = now_ms;
    } else if (id == ID_LAMBDA) {
        s.lambda1_x10000 = lambda1X10000FromRaw(b[0]);
        s.adv_x10        = advX10FromRaw(b[5]);
        s.bat_x10        = batX10FromRaw(be16(b + 6));
        s.lambda_ms      = now_ms;
    } else if (id == ID_PRESSURE) {
        s.map_x10     = mapX10FromRaw(be16(b + 0));
        s.oil_x10     = oilX10FromRaw(b[4]);
        s.pressure_ms = now_ms;
    } else {                                           // X-Series UEGO
        s.uego_lambda_x10000 = be16(b + 0);
        s.uego_o2_x1000      = (int16_t)be16(b + 2);
        s.uego_sysv_x10      = b[4];
        s.uego_flags6        = b[6];
        s.uego_flags7        = b[7];
        s.uego_index         = (uint8_t)(id - ID_UEGO_1 + 1);
        s.uego_ms            = now_ms;
    }
    return true;
}

// Expire every group that has been silent for longer than stale_ms (fields -> -1), so a
// device that stops broadcasting reads as "no data", never as a frozen last value.
inline void expire(State& s, uint32_t now_ms, uint32_t stale_ms) {
    if (s.engine_ms && now_ms - s.engine_ms > stale_ms) {
        s.rpm = -1; s.tps_x10 = s.iat_f_x10 = s.clt_f_x10 = -1; s.engine_ms = 0;
    }
    if (s.lambda_ms && now_ms - s.lambda_ms > stale_ms) {
        s.lambda1_x10000 = -1; s.adv_x10 = -1000; s.bat_x10 = -1; s.lambda_ms = 0;
    }
    if (s.pressure_ms && now_ms - s.pressure_ms > stale_ms) {
        s.map_x10 = s.oil_x10 = -1; s.pressure_ms = 0;
    }
    if (s.uego_ms && now_ms - s.uego_ms > stale_ms) {
        s.uego_lambda_x10000 = -1; s.uego_o2_x1000 = 0; s.uego_sysv_x10 = -1;
        s.uego_flags6 = s.uego_flags7 = 0; s.uego_index = 0; s.uego_ms = 0;
    }
}

inline bool anyLive(const State& s) {
    return s.engine_ms || s.lambda_ms || s.pressure_ms || s.uego_ms;
}

inline UegoStatus uegoStatus(const State& s) {
    if (!s.uego_ms) return UEGO_NONE;
    if (s.uego_flags7 & UEGO_FAULT) return UEGO_FAULTED;
    if (!(s.uego_flags6 & UEGO_VALID)) return UEGO_NOT_READY;   // e.g. heater warm-up
    return UEGO_OK;
}

// The lambda the logger reports, x10000. The UEGO gauge OWNS lambda while it is broadcasting:
// valid -> its reading; warming up / faulted -> -1 (no silent fall-back to another value).
// With no gauge on the bus, the ECU's Lambda #1 is used.
inline int32_t lambdaX10000(const State& s) {
    const UegoStatus st = uegoStatus(s);
    if (st == UEGO_OK) return s.uego_lambda_x10000;
    if (st != UEGO_NONE) return -1;
    return s.lambda1_x10000;
}
inline int16_t afrX10(const State& s) { return afrX10FromLambda(lambdaX10000(s)); }

// Battery: the ECU's measurement; with no ECU stream, the UEGO gauge's own supply voltage
// (it runs on the switched 12 V, so it reads the car's battery).
inline int16_t batteryX10(const State& s) {
    if (s.lambda_ms && s.bat_x10 > 0) return s.bat_x10;
    if (s.uego_ms && s.uego_sysv_x10 > 0) return s.uego_sysv_x10;
    return -1;
}

}  // namespace aemnet
