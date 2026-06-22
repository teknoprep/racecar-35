// ===========================================================================
// board_config.h — per-panel hardware bring-up for the racecar-35 dash.
//
// The dash UI / settings / protocol / OTA logic in RaceDash.ino is identical
// across every CrowPanel size (they're all 800x480 ESP32-S3). ONLY the RGB
// panel pin map + timing, the backlight pin and the touch rotation differ.
// Those board-specific values live here, selected at COMPILE time.
//
//   DASH_BOARD == 7  -> CrowPanel ESP32 7.0" V3.0   (board id "crowpanel7")
//   DASH_BOARD == 5  -> CrowPanel ESP32 5.0" V3.0   (board id "crowpanel5")
//
// IDENTITY IS BAKED AT THE FIRST USB FLASH. We never auto-detect the panel at
// runtime (a wrong RGB-timing guess = an unrecoverable black screen). Because
// the binary is built for exactly one panel, OTA can — and does — only ever
// consume the manifest entry keyed by this board's DASH_BOARD_ID, so a 5" can
// never pull a 7" image and vice-versa. Worst-case recovery is always a USB
// reflash, which re-establishes identity.
//
// Build:
//   7" (default):  --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=7"
//   5":            --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=5"
//
// Source of truth for the pin maps / timing is Elecrow's V3.0 course file
// (gfx_conf.h, blocks CrowPanel_70 / CrowPanel_50). NOTE: the 7" freq_write is
// kept at 15 MHz here (our field-proven value) rather than the vendor's 12 MHz.
// ===========================================================================
#pragma once

#ifndef DASH_BOARD
#define DASH_BOARD 7   // default to the 7" if the build flag is absent
#endif

// Backlight is GPIO 2 on every CrowPanel V3.0 size.
#define DASH_PIN_BL GPIO_NUM_2

// GT911 touch on Wire (I2C_NUM_0) — same pins (SDA 19 / SCL 20) and auto-
// detected address on every size. ROTATION_INVERTED gives raw (x,y) on the
// 7"; flip to ROTATION_NORMAL if a panel reads mirrored.
#ifndef DASH_TOUCH_ROTATION
#define DASH_TOUCH_ROTATION ROTATION_INVERTED
#endif

// ---------------------------------------------------------------------------
#if DASH_BOARD == 7
// ---------------------------------------------------------------------------
#define DASH_BOARD_ID   "crowpanel7"
#define DASH_BOARD_NAME "crowpanel-7.0"

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

// ---------------------------------------------------------------------------
#elif DASH_BOARD == 5
// ---------------------------------------------------------------------------
#define DASH_BOARD_ID   "crowpanel5"
#define DASH_BOARD_NAME "crowpanel-5.0"

// Elecrow CrowPanel_50: completely different RGB pin map vs the 7", and
// HE/VSYNC are SWAPPED (HE=40, VSYNC=41). Verified against the vendor course
// file gfx_conf.h. Touch rotation may need flipping on first boot — see note.
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
#define DASH_FREQ_WRITE  12000000   // vendor value for the 5" V3.0

#define DASH_HSYNC_FRONT_PORCH 8
#define DASH_HSYNC_PULSE_WIDTH 4
#define DASH_HSYNC_BACK_PORCH  43
#define DASH_VSYNC_FRONT_PORCH 8
#define DASH_VSYNC_PULSE_WIDTH 4
#define DASH_VSYNC_BACK_PORCH  12

// ---------------------------------------------------------------------------
#else
#error "Unsupported DASH_BOARD — set -DDASH_BOARD=7 or -DDASH_BOARD=5"
#endif

// Manifest section token for this board's OTA entry, e.g. "\"crowpanel5\"".
#define DASH_OTA_KEY "\"" DASH_BOARD_ID "\""
