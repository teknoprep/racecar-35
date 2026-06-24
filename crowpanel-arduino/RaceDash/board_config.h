// ===========================================================================
// board_config.h — per-panel hardware bring-up for the racecar-35 dash.
//
// The dash UI / settings / protocol / OTA logic in RaceDash.ino is identical
// across every supported panel. ONLY the RGB pin map + timing + sync
// polarities, the touch I2C pins, the backlight mechanism and the LCD reset
// mechanism differ. Those board-specific values live here, selected at
// COMPILE time via -DDASH_BOARD:
//
//   DASH_BOARD == 7   -> CrowPanel ESP32 7.0" V3.0 (Basic RGB)  -> "crowpanel7"
//   DASH_BOARD == 5   -> CrowPanel ESP32 5.0" V3.0 (Basic RGB)  -> "crowpanel5"
//   DASH_BOARD == 51  -> CrowPanel Advance 5.0" (HMI IPS, S3)    -> "crowpanel5adv"
//
// IDENTITY IS BAKED AT THE FIRST USB FLASH. We never auto-detect the panel at
// runtime (a wrong RGB-timing guess = an unrecoverable black screen). The
// binary is built for exactly one panel, so OTA can — and does — only ever
// consume the manifest entry keyed by this board's DASH_BOARD_ID.
//
// Build (note the Advance is a 16 MB module, so FlashSize=16M):
//   7":       --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=7"   FlashSize=4M
//   5" Basic: --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=5"   FlashSize=4M
//   5" Adv:   --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=51"  FlashSize=16M
//
// Sources of truth:
//   7"/5" Basic: Elecrow CrowPanel V3.0 course file gfx_conf.h (CrowPanel_70 /
//                CrowPanel_50). 7" freq_write kept at our field-proven 15 MHz.
//   5" Advance:  Elecrow CrowPanel-Advance-5 repo, example V1.2_and_V1.3
//                (LovyanGFX_Driver.h + main.cpp). N16R8, OPI PSRAM, GT911 on
//                15/16, backlight via an I2C coprocessor at 0x30.
// ===========================================================================
#pragma once

#ifndef DASH_BOARD
#define DASH_BOARD 7   // default to the 7" if the build flag is absent
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
// The current "HMI" 5" (CrowPanel-5.0-HMI repo, V3.0) uses 9 MHz; the older
// course-file CrowPanel_50 used 12 MHz. Match 9 MHz — it's the value the panel
// in hand was shipped with, and lower pclk is safe on our 80 MHz-PSRAM build.
#define DASH_FREQ_WRITE  9000000

#define DASH_HSYNC_FRONT_PORCH 8
#define DASH_HSYNC_PULSE_WIDTH 4
#define DASH_HSYNC_BACK_PORCH  43
#define DASH_VSYNC_FRONT_PORCH 8
#define DASH_VSYNC_PULSE_WIDTH 4
#define DASH_VSYNC_BACK_PORCH  12

#define DASH_HSYNC_POLARITY  0
#define DASH_VSYNC_POLARITY  0
// Latch on the rising pclk edge (0). The vendor course used 1, but on our build
// that sampled data mid-transition -> blurry text / colour fringing on this TFT;
// 0 matches the crisp Advance. (pclk FREQUENCY doesn't affect sharpness, only
// refresh rate, so the edge is the lever here.)
#define DASH_PCLK_ACTIVE_NEG 0
#define DASH_DE_IDLE_HIGH    0
#define DASH_PCLK_IDLE_HIGH  0

#define DASH_TOUCH_SDA      19
#define DASH_TOUCH_SCL      20
#define DASH_TOUCH_ROTATION ROTATION_INVERTED

// ---------------------------------------------------------------------------
#elif DASH_BOARD == 51
// ---------------------------------------------------------------------------
// CrowPanel Advance 5.0" (HMI IPS, ESP32-S3-WROOM-1-N16R8). Verified against
// Elecrow's example V1.2_and_V1.3 (LovyanGFX_Driver.h + main.cpp). This is a
// different platform from the Basic panels: inverted sync polarities, GT911 on
// 15/16, backlight + reset via an I2C coprocessor at 0x30 (no PCA9557).
#define DASH_BOARD_ID   "crowpanel5adv"
#define DASH_BOARD_NAME "crowpanel-5.0-adv"
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

// ---------------------------------------------------------------------------
#else
#error "Unsupported DASH_BOARD — set -DDASH_BOARD=7, 5, or 51"
#endif

// Manifest section token for this board's OTA entry, e.g. "\"crowpanel5adv\"".
#define DASH_OTA_KEY "\"" DASH_BOARD_ID "\""
