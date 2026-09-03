// ===========================================================================
// board_config.h — per-panel hardware bring-up for the racecar-35 dash.
//
// The dash UI / settings / protocol / OTA logic in RaceDash.ino is identical
// across every supported panel. ONLY the RGB pin map + timing + sync
// polarities, the touch I2C pins, the backlight mechanism and the LCD reset
// mechanism differ. Those board-specific values live here, selected at
// COMPILE time via -DDASH_BOARD:
//
//   DASH_BOARD == 51  -> CrowPanel Advance 5.0" (HMI IPS, S3)    -> "crowpanel5adv"
//   DASH_BOARD == 71  -> CrowPanel Advance 7.0" (HMI IPS, S3)    -> "crowpanel7adv"
//
//   RETIRED (hardware scrapped after v0.1.146 — no longer built/published):
//   DASH_BOARD == 7   -> CrowPanel ESP32 7.0" V3.0 (Basic RGB)  -> "crowpanel7"
//   DASH_BOARD == 5   -> CrowPanel ESP32 5.0" V3.0 (Basic RGB)  -> "crowpanel5"
//   Their pin maps are kept below for reference only and are a HARD BUILD
//   ERROR unless -DDASH_ALLOW_RETIRED_BOARDS is also given.
//
// IDENTITY IS BAKED AT THE FIRST USB FLASH. We never auto-detect the panel at
// runtime (a wrong RGB-timing guess = an unrecoverable black screen). The
// binary is built for exactly one panel, so OTA can — and does — only ever
// consume the manifest entry keyed by this board's DASH_BOARD_ID.
//
// Build (both Advance panels are 16 MB modules, so FlashSize=16M):
//   5" Adv:   --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=51"  FlashSize=16M
//   7" Adv:   --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=71"  FlashSize=16M
//
// Sources of truth:
//   7"/5" Basic: Elecrow CrowPanel V3.0 course file gfx_conf.h (CrowPanel_70 /
//                CrowPanel_50). 7" freq_write kept at our field-proven 15 MHz.
//   5" Advance:  Elecrow CrowPanel-Advance-5 repo, example V1.2_and_V1.3
//                (LovyanGFX_Driver.h + main.cpp). N16R8, OPI PSRAM, GT911 on
//                15/16, backlight via an I2C coprocessor at 0x30.
//   7" Advance:  Elecrow CrowPanel-Advance-7 repo. Its LovyanGFX_Driver.h is
//                the SAME shared "..._4_3_5_0_7_0" file as the 5" Advance —
//                identical RGB pin map, porches, polarities, PSRAM framebuffer,
//                GT911 pins and 0x30 coprocessor (verified 2026-09 across every
//                V1.0/V1.2/V1.3-1.5 example + factory source). Only the board id
//                differs; the 7" glass may want its own pclk — see the note.
// ===========================================================================
#pragma once

#ifndef DASH_BOARD
#define DASH_BOARD 71  // default to the 7" ADVANCE if the build flag is absent
#endif

// The Basic panels are retired hardware. Refuse to build them by accident —
// a Basic image flashed onto an Advance = dead touch + garbled RGB.
#if (DASH_BOARD == 7 || DASH_BOARD == 5) && !defined(DASH_ALLOW_RETIRED_BOARDS)
#error "DASH_BOARD 7/5 (CrowPanel Basic) are RETIRED. Build 51 (5\" Advance) or 71 (7\" Advance). Define DASH_ALLOW_RETIRED_BOARDS to override."
#endif

// Backlight is GPIO 2 PWM on the Basic V3.0 panels; the Advance drives it over
// I2C instead (see DASH_IS_ADVANCE). Declared for all; unused on the Advance.
#define DASH_PIN_BL GPIO_NUM_2

// ---------------------------------------------------------------------------
#if DASH_BOARD == 7
// ---------------------------------------------------------------------------
#define DASH_BOARD_ID   "crowpanel7"
#define DASH_BOARD_NAME "crowpanel-7.0"
#define DASH_IS_ADVANCE 0

#define DASH_PIN_D0  GPIO_NUM_15   // B0
#define DASH_PIN_D1  GPIO_NUM_7    // B1
#define DASH_PIN_D2  GPIO_NUM_6    // B2
#define DASH_PIN_D3  GPIO_NUM_5    // B3
#define DASH_PIN_D4  GPIO_NUM_4    // B4
#define DASH_PIN_D5  GPIO_NUM_9    // G0
#define DASH_PIN_D6  GPIO_NUM_46   // G1
#define DASH_PIN_D7  GPIO_NUM_3    // G2
#define DASH_PIN_D8  GPIO_NUM_8    // G3
#define DASH_PIN_D9  GPIO_NUM_16   // G4
#define DASH_PIN_D10 GPIO_NUM_1    // G5
#define DASH_PIN_D11 GPIO_NUM_14   // R0
#define DASH_PIN_D12 GPIO_NUM_21   // R1
#define DASH_PIN_D13 GPIO_NUM_47   // R2
#define DASH_PIN_D14 GPIO_NUM_48   // R3
#define DASH_PIN_D15 GPIO_NUM_45   // R4

#define DASH_PIN_HENABLE GPIO_NUM_41
#define DASH_PIN_VSYNC   GPIO_NUM_40
#define DASH_PIN_HSYNC   GPIO_NUM_39
#define DASH_PIN_PCLK    GPIO_NUM_0
#define DASH_FREQ_WRITE  15000000   // field-proven on our 7" V3.0 (vendor uses 12 MHz)

#define DASH_HSYNC_FRONT_PORCH 40
#define DASH_HSYNC_PULSE_WIDTH 48
#define DASH_HSYNC_BACK_PORCH  40
#define DASH_VSYNC_FRONT_PORCH 1
#define DASH_VSYNC_PULSE_WIDTH 31
#define DASH_VSYNC_BACK_PORCH  13

#define DASH_HSYNC_POLARITY  0
#define DASH_VSYNC_POLARITY  0
#define DASH_PCLK_ACTIVE_NEG 1
#define DASH_DE_IDLE_HIGH    0
#define DASH_PCLK_IDLE_HIGH  0

#define DASH_TOUCH_SDA      19
#define DASH_TOUCH_SCL      20
#define DASH_TOUCH_ROTATION ROTATION_INVERTED

// Touch gesture tuning. The 7" GT911 reports fast, so keep the snappy defaults
// (short release debounce, normal swipe distance/time).
#define DASH_TOUCH_RELEASE_MS 80
#define DASH_SWIPE_DX_MIN     120
#define DASH_SWIPE_MS_MAX     800

// ---------------------------------------------------------------------------
#elif DASH_BOARD == 5
// ---------------------------------------------------------------------------
#define DASH_BOARD_ID   "crowpanel5"
#define DASH_BOARD_NAME "crowpanel-5.0"
#define DASH_IS_ADVANCE 0

// Elecrow CrowPanel_50 (Basic RGB): different pin map vs 7", HE/VSYNC swapped.
#define DASH_PIN_D0  GPIO_NUM_8    // B0
#define DASH_PIN_D1  GPIO_NUM_3    // B1
#define DASH_PIN_D2  GPIO_NUM_46   // B2
#define DASH_PIN_D3  GPIO_NUM_9    // B3
#define DASH_PIN_D4  GPIO_NUM_1    // B4
#define DASH_PIN_D5  GPIO_NUM_5    // G0
#define DASH_PIN_D6  GPIO_NUM_6    // G1
#define DASH_PIN_D7  GPIO_NUM_7    // G2
#define DASH_PIN_D8  GPIO_NUM_15   // G3
#define DASH_PIN_D9  GPIO_NUM_16   // G4
#define DASH_PIN_D10 GPIO_NUM_4    // G5
#define DASH_PIN_D11 GPIO_NUM_45   // R0
#define DASH_PIN_D12 GPIO_NUM_48   // R1
#define DASH_PIN_D13 GPIO_NUM_47   // R2
#define DASH_PIN_D14 GPIO_NUM_21   // R3
#define DASH_PIN_D15 GPIO_NUM_14   // R4

#define DASH_PIN_HENABLE GPIO_NUM_40
#define DASH_PIN_VSYNC   GPIO_NUM_41
#define DASH_PIN_HSYNC   GPIO_NUM_39
#define DASH_PIN_PCLK    GPIO_NUM_0
// EXACT vendor values for this panel: CrowPanel-5.0-HMI repo V3.0 demo
// (example/V3.0/Arduino/Course/LVGL_Arduino5.0) ships freq_write=9 MHz with
// pclk_active_neg=1. Every data pin + porch already matches that file.
#define DASH_FREQ_WRITE  9000000

#define DASH_HSYNC_FRONT_PORCH 8
#define DASH_HSYNC_PULSE_WIDTH 4
#define DASH_HSYNC_BACK_PORCH  43
#define DASH_VSYNC_FRONT_PORCH 8
#define DASH_VSYNC_PULSE_WIDTH 4
#define DASH_VSYNC_BACK_PORCH  12

#define DASH_HSYNC_POLARITY  0
#define DASH_VSYNC_POLARITY  0
// Latch data on the FALLING pclk edge (1) — the vendor value for this panel.
// A previous build set this to 0 ("matches the Advance / crisper") but that was
// wrong: 0 latches the RGB bus mid-transition on this TFT, giving red/colour
// fringing on thin strokes (small text) while thick strokes (big fonts) mask
// it. 1 samples in the stable window -> clean small text. (pclk FREQUENCY only
// affects refresh rate, not sharpness; the latch EDGE is the lever here.)
#define DASH_PCLK_ACTIVE_NEG 1
#define DASH_DE_IDLE_HIGH    0
#define DASH_PCLK_IDLE_HIGH  0

#define DASH_TOUCH_SDA      19
#define DASH_TOUCH_SCL      20
#define DASH_TOUCH_ROTATION ROTATION_INVERTED

// Touch gesture tuning — LOOSENED for this panel. The 5" Basic GT911 delivers
// position updates very slowly (~7-12 Hz even while a finger is dragging, vs
// the Advance which is fast). With the snappy 80 ms release debounce a swipe
// gets "released" before the GT911's 2nd sample lands, so dx never accumulates
// and the swipe is mis-read as a tap (taps work, swipes don't). A longer
// release debounce BRIDGES the inter-sample gaps so one continuous gesture
// stays alive across the sparse samples; a shorter swipe distance + longer
// time window let a 2-3 sample swipe still register. Cost: ~200 ms of tap-
// release latency, acceptable for the payoff of working swipes. (Advance/7"
// keep the snappy defaults.)
#define DASH_TOUCH_RELEASE_MS 200
#define DASH_SWIPE_DX_MIN     60
#define DASH_SWIPE_MS_MAX     1500

// ---------------------------------------------------------------------------
#elif DASH_BOARD == 51 || DASH_BOARD == 71
// ---------------------------------------------------------------------------
// CrowPanel Advance 5.0" / 7.0" (HMI IPS, ESP32-S3-WROOM-1-N16R8). Verified
// against Elecrow's example V1.2_and_V1.3 (5") / V1.3_and_V1.4_and_V1.5 (7")
// (LovyanGFX_Driver.h + main.cpp) — one shared vendor driver file, identical
// electrical config on both sizes. This is a different platform from the Basic
// panels: inverted sync polarities, GT911 on 15/16, backlight + reset via an
// I2C coprocessor at 0x30 (no PCA9557). Coprocessor DIALECT differs by board
// revision (5" V1.1 / 7" V1.2 = ladder; 5" V1.2+ / 7" V1.3+ = linear) — see
// Settings::adv_rev in RaceDash.ino.
#if DASH_BOARD == 71
#define DASH_BOARD_ID   "crowpanel7adv"
#define DASH_BOARD_NAME "crowpanel-7.0-adv"
#else
#define DASH_BOARD_ID   "crowpanel5adv"
#define DASH_BOARD_NAME "crowpanel-5.0-adv"
#endif
#define DASH_IS_ADVANCE 1

#define DASH_PIN_D0  GPIO_NUM_21   // B0
#define DASH_PIN_D1  GPIO_NUM_47   // B1
#define DASH_PIN_D2  GPIO_NUM_48   // B2
#define DASH_PIN_D3  GPIO_NUM_45   // B3
#define DASH_PIN_D4  GPIO_NUM_38   // B4
#define DASH_PIN_D5  GPIO_NUM_9    // G0
#define DASH_PIN_D6  GPIO_NUM_10   // G1
#define DASH_PIN_D7  GPIO_NUM_11   // G2
#define DASH_PIN_D8  GPIO_NUM_12   // G3
#define DASH_PIN_D9  GPIO_NUM_13   // G4
#define DASH_PIN_D10 GPIO_NUM_14   // G5
#define DASH_PIN_D11 GPIO_NUM_7    // R0
#define DASH_PIN_D12 GPIO_NUM_17   // R1
#define DASH_PIN_D13 GPIO_NUM_18   // R2
#define DASH_PIN_D14 GPIO_NUM_3    // R3
#define DASH_PIN_D15 GPIO_NUM_46   // R4

#define DASH_PIN_HENABLE GPIO_NUM_42
#define DASH_PIN_VSYNC   GPIO_NUM_41
#define DASH_PIN_HSYNC   GPIO_NUM_40
#define DASH_PIN_PCLK    GPIO_NUM_39
// Vendor uses 18 MHz, but that assumes their 120 MHz-PSRAM / IDF5.3 build. On
// our stock arduino-cli build (80 MHz PSRAM) 18 MHz starves the framebuffer DMA
// -> diagonal scan-out tearing; 12 MHz is below this panel's lock threshold
// (drifting solid colors). 15 MHz is the search midpoint (also our 7" value).
// 7" Advance starts at the same 15 MHz (its Basic 7" sibling is field-proven
// there); if the 7" IPS glass tears or drifts, tune THIS value per board.
#define DASH_FREQ_WRITE  15000000

#define DASH_HSYNC_FRONT_PORCH 8
#define DASH_HSYNC_PULSE_WIDTH 4
#define DASH_HSYNC_BACK_PORCH  8
#define DASH_VSYNC_FRONT_PORCH 8
#define DASH_VSYNC_PULSE_WIDTH 4
#define DASH_VSYNC_BACK_PORCH  8

#define DASH_HSYNC_POLARITY  1
#define DASH_VSYNC_POLARITY  1
#define DASH_PCLK_ACTIVE_NEG 0
#define DASH_DE_IDLE_HIGH    0
#define DASH_PCLK_IDLE_HIGH  1

#define DASH_TOUCH_SDA      15
#define DASH_TOUCH_SCL      16
#define DASH_TOUCH_ROTATION ROTATION_INVERTED   // verify on first boot; flip if mirrored

// Touch gesture tuning. The Advance GT911 reports fast (the user confirms its
// swipes feel good) — keep the snappy defaults.
#define DASH_TOUCH_RELEASE_MS 80
#define DASH_SWIPE_DX_MIN     120
#define DASH_SWIPE_MS_MAX     800

// ---------------------------------------------------------------------------
#else
#error "Unsupported DASH_BOARD — set -DDASH_BOARD=7, 5, 51, or 71"
#endif

// Manifest section token for this board's OTA entry, e.g. "\"crowpanel5adv\"".
#define DASH_OTA_KEY "\"" DASH_BOARD_ID "\""
