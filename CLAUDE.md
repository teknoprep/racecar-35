# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`racecar-35` is a two-MCU dash for a track car, with the **driver-facing display in the cabin** and **all sensing + connectivity in the trunk** so we run only one UART wire end-to-end.

```
Cabin (driver)                   Trunk (data + connectivity)
+-------------------+           +-----------------------------+
|  CrowPanel ESP32  | ---UART-> |  Teensy 4.1                 |
|  - dash UI        |           |  - GPS (NEO-M9N) @ 25 Hz    |
|  - settings page  |           |  - tach in (opto, pin 9)    |
|  - track picker   |           |  - W5500 ethernet (SPI0)*   |
|  - touch input    |           |  - SD card (built-in slot)* |
|  - keyboards      |           |  - JSON serializer @ 25 Hz* |
|  - REC commands ──+--UART---->|  - cloud uploader + queue*  |
+-------------------+           |  - NTP from 0.pool.ntp.org* |
                                +-----------------------------+
                                 (* = planned, lands when W5500 arrives)
```

UART is **bidirectional**:
- Teensy → CrowPanel: `GPS,...` and `ENG,...` lines (telemetry, 25 Hz with 1 Hz heartbeat)
- CrowPanel → Teensy: `REC,<0|1>` and `TRACK,<name>` (control, on demand from START/STOP)

[README.md](README.md) has the original wiring table — **architecture has expanded since**: there's now a planned W5500 ethernet module on the Teensy SPI0, and the dash sends control commands back to the Teensy.

## Critical: two MCUs, two build systems — and they are NOT interchangeable

| Side | Toolchain | Why |
| --- | --- | --- |
| Teensy 4.1 ([src/main.cpp](src/main.cpp), [platformio.ini](platformio.ini)) | **PlatformIO** | Works fine. |
| CrowPanel ESP32-S3 V3.0 ([crowpanel-arduino/RaceDash/RaceDash.ino](crowpanel-arduino/RaceDash/RaceDash.ino)) | **arduino-cli** with `esp32:esp32@2.0.14` | PIO's `espressif32@^6.7.0` bootloader **boot-loops on this specific board** with `rst:0x3 (RTC_SW_SYS_RST)` before user code runs. Do not waste time re-trying PIO for the CrowPanel. The arduino-cli toolchain produces a bootloader that does work. |

Directories `crowpanel-ui/` and `crowpanel-baseline/` are **dead PIO experiments** kept for reference only. Live CrowPanel code is `crowpanel-arduino/RaceDash/`.

## Build & flash commands

### Teensy (PlatformIO)
```powershell
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" run -t upload   # build + flash
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" device monitor  # USB serial (COM4)
```
`monitor_port = COM4` is pinned in [platformio.ini](platformio.ini) so the monitor doesn't grab the CrowPanel's CH340 instead.

### CrowPanel (arduino-cli)
The CLI ships with Arduino IDE 2.x at `C:\Users\ChrisRawlings\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`. The exact FQBN matters — every option is required:

```
esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=4M,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none
```

```powershell
$cli = "C:\Users\ChrisRawlings\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
& $cli compile --fqbn $fqbn $sketch
& $cli upload  --fqbn $fqbn -p COM3 $sketch
```

**Always disconnect the Teensy↔CrowPanel UART jumpers before flashing the CrowPanel.** UART0 (GPIO 43/44) is shared with the CH340 used for upload — Teensy contention silently corrupts the flash, or you get `The serial TX path seems to be down` from esptool.

## Hardware constants that bit us repeatedly

- **CrowPanel rev:** V3.0 (silkscreened on the back). V3.0 has a **PCA9557 I²C IO expander at 0x18** that holds the LCD in reset until IO0 is driven high. V1.X / V2.X don't. Source per rev lives at `_vendor/CrowPanel-ESP32-Display-Course-File/CrowPanel_ESP32_Tutorial/Code/{V1.X,V2.X,7.0 v3.0 touch new code}/`. **V3.0 uses 15 MHz pclk** (V2.X uses 12 MHz — the configs look interchangeable but are not).
- **UART0 (GPIO 43/44) on the CrowPanel is shared with the CH340** used for USB upload.
- **Backlight (GPIO 2)** must be initialised after the PCA9557 init releases LCD reset. Pattern: `ledcSetup(1, 300, 8); ledcAttachPin(2, 1); ledcWrite(1, 0);` then `pinMode(2, OUTPUT); digitalWrite(2, LOW); delay(500); digitalWrite(2, HIGH);`. The LEDC setup is effectively a no-op once `pinMode` runs — backlight is GPIO HIGH.
- **GT911 capacitive touch** is at I²C address **0x14** (NOT the more common 0x5D), shared with the panel's I²C bus on SDA=19, SCL=20, port `I2C_NUM_1`.
- **Documents folder is OneDrive-redirected** to `C:\Users\ChrisRawlings\OneDrive - BlueCloud Consultants\Documents\` on this machine. Arduino libraries live there, NOT under `~/Documents/Arduino`. arduino-cli's `directories.user` is set to that OneDrive path.
- **PCA9557 library is not in the Arduino registry** — bundled in `_vendor/.../File/PCA9557/` and copied into the libraries dir with a hand-written `library.properties`.
- **COM port mapping on this machine:** COM3 = CrowPanel (CH340), COM4 = Teensy (USB-CDC native).
- **Teensy 4.1 pin assignments in use** — keep these straight before claiming a "free" pin:
  - **Serial2** (RX 7, TX 8): u-blox GNSS UART
  - **Serial3** (TX 14, RX 15): bidirectional dash link to CrowPanel UART0
  - **Pin 9**: tach input via opto (FreqMeasure input capture — *the only pin FreqMeasure works on for T4.x*)
  - **SPI0** (CS 10, MOSI 11, MISO 12, SCK 13): **W5500 ethernet** (when installed)
  - **Pin 5**: W5500 `/INT` (planned)
  - **Pin 6**: W5500 `/RESET` (planned)
  - **SDIO (built-in)**: SD card (pins are dedicated, not on the header)
- **Optocoupler tach front-end** — PC817-class is fine for typical 4-cyl × 2-pulse-per-rev (≤ 270 Hz at 8 k RPM). The Teensy side needs a 4.7–10 kΩ pull-up from `3V3` to pin 9. Output is *inverted* but FreqMeasure counts edges either way.

## Wire protocol (Teensy ↔ CrowPanel UART)

Bidirectional, 115200 8N1, line-oriented, `\n`-terminated.

### Teensy → CrowPanel (telemetry)
Up to 25 Hz when GPS PVT is fresh; 1 Hz heartbeat fallback when not.
```
GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>,<gps_status>
ENG,<rpm>
```
- `gps_status`: `0=OFF` (no bytes from GPS UART), `1=RAW` (bytes flowing but lib couldn't UBX-handshake), `2=OK` (lib connected, fresh PVT), `3=STALE` (lib was connected but no fresh PVT in 1.5 s)
- `rpm`: integer, derived from tach pulse frequency / `RPM_PULSES_PER_REV` (default 2.0; calibrate if it reads 2× / ½×). Reports 0 when no pulses for 750 ms.
- The dash parser tolerates 6 OR 7 fields on the GPS line for back-compat.
- **Rate-limiting**: emit only when `myGNSS.getPVT(0)` returns true (fresh data) OR every 1 s heartbeat. `getPVT(0)` is non-blocking — DO NOT use the blocking variant or the loop chokes at 25 Hz.

### CrowPanel → Teensy (control)
```
REC,<0|1>          # start/stop recording
TRACK,<name>       # set the current track name (sent immediately before REC,1)
```
The Teensy parses these in `handleDashCommand()` ([src/main.cpp](src/main.cpp)). Recording state is a flag today — actual SD logging and cloud upload arrive when the W5500 lands.

**Boot-time gotcha** (Teensy): calling `myGNSS.getFixType()` before the first successful `getPVT()` returns uninitialised heap memory. The emit path gates all PVT reads on `gnss_lib_ok && gnss_last_fresh_ms != 0`.

## Settings persistence (NVS via Arduino `Preferences`)

Namespace `"dash"`. Keys are short to fit NVS limits. Saved on every dash entry (when you swipe back from settings → dash). Full list:

| Key | Type | Purpose |
| --- | --- | --- |
| `rpm_min` / `rpm_max` | uint16 | RPM bar range |
| `alerts` | bool | Master alerts on/off |
| `a1_rpm` / `a1_col` / `a1_hz` | uint16/uint8/uint8 | Alert 1 threshold/colour/blink-Hz |
| `am_rpm` / `am_col` / `am_hz` | uint16/uint8/uint8 | MAX alert ditto |
| `rec_sd` / `rec_cl` | bool | Record-to-SD / record-to-cloud master switches |
| `cl_host` | string | Cloud DNS or IP |
| `cl_port` | uint16 | Cloud port |
| `cl_proto` | uint8 | 0=HTTP, 1=HTTPS, 2=FTP |
| `cl_strm` | uint8 | 0=Live, 1=AfterRace |
| `cl_user` / `cl_pass` | string | Cloud Basic-auth credentials (pass shown masked as `*****`) |
| `auto_trk` | bool | Auto-select closest track on START (skip picker if a clear match exists) |

`clampInvariants()` enforces `rpm_min < rpm_max`, `alert1_rpm < alertmax_rpm`, `alert1_hz < alertmax_hz` after every mutation.

## Dash UI architecture (RaceDash.ino)

### Pages
| Page | Entered via | Purpose |
| --- | --- | --- |
| `PAGE_DASH` | swipe ←-direction from settings | RPM bar (top), HUGE Font7 speed (right side at x=600), HDG/LAT/LON left column, FIX/SATS/GPS right column, START/STOP button left of speed |
| `PAGE_SETTINGS` | swipe →-direction from dash | Scrollable list of 18 settings rows |
| `PAGE_NUM_KB` | tap on cloud port value | Numeric keypad (3 cols × 4 rows + DONE/CANCEL) |
| `PAGE_TEXT_KB` | tap on cloud host / auth user / auth pass | Full lowercase keyboard (10 × 4 letters/digits + .-_/ + BACK/SPACE/DONE/CANCEL) |
| `PAGE_TRACK_PICKER` | tap START button (when not auto-confirming) | Modal list of tracks; closest GPS match auto-bumped to top with distance label |

### Track picker
Pre-seeded with 15 common US road courses (`TRACKS[]` near the top of `RaceDash.ino`). To add tracks, extend that array (TODO: editable from settings).

The `Auto select by GPS` toggle determines whether the picker actually opens or auto-confirms the closest match in range:
- **ON** + GPS fix + a track within its `radius_km` → skip picker, immediate `TRACK,<name>` + `REC,1`
- **OFF** or no match → open picker; user taps a row, taps CONFIRM
- The synthetic `(no track / unknown)` row is always last in the list and emits `TRACK,UNKNOWN`
- Closest track is **highlighted green at the top** of the picker with a `closest · X.X km` distance label

### Touch handling
Single touch handler at the top of `loop()` distinguishes:
- **Tap** (|dx|, |dy| < 30 px, < 600 ms): button press / row select / key press
- **Horizontal swipe** (|dx| > 120 px, |dy| < 100 px, < 800 ms): page navigation between dash and settings
- **Vertical drag** on PAGE_SETTINGS or PAGE_TRACK_PICKER: scroll content, header/footer stay sticky

Keyboard pages use a tap-only model (CANCEL is the way out, no swipe-back).

### Anti-flicker rendering rules — DO NOT remove
1. **Conditional redraws on dash page.** Each dynamic element caches its last-drawn value in `LastDrawn ld`. Drawing skips when the new value matches.
2. **`setTextColor(fg, bg)`** so each character cell paints its own background. No `fillRect → blanked frame → drawString` sequences ever.
3. **`setTextPadding()` for centred dynamic text** like the speed: clears any leftover from a longer previous draw atomically while keeping the visible digits centred at the draw position.
4. **Settings + picker pages: per-row repaint, NOT full-band wipe.** Full-band wipes (~624 KB to PSRAM) take ~12 ms and the LCD scan-out catches them mid-render → visible flashing. Instead we wipe only narrow strip bands (40 px each at the body's top and bottom edges, ~64 KB each = 1.3 ms) on scroll-position change. Tap mutations skip the wipe entirely.
5. **Scroll redraw rate cap = 30 Hz max** during active drag (`now - lastDraw >= 33 ms`). At ~200 Hz the LCD never gets a clean scan window between renders and tearing accumulates. 30 Hz gives the LCD two full scan periods per render.

The settings page uses a `settingsDirty` flag, picker uses `tp.dirty`. Drawing is skipped between mutations.

**Scroll flicker is reduced but not zero.** If a perfect fix is needed, the next step is a sprite back-buffer for each scrollable body — render to PSRAM sprite first, `pushSprite` once. That's deferred for now (current behaviour was deemed acceptable).

### Speed format
Speed renders at Font7 size 4 (~192 px tall) centred at x=600 (right side of screen, away from the START/STOP button on the left). **The decimal drops at >=100 mph** so 3-digit values stay narrow enough to fit the 400-px bg pad without clipping.

### Setting up new ENUM controls
Add to `enumValue()`, the appropriate `_NAMES[]` array, and the cycle case in `handleSettingsTap()` ENUM branch. `PROTOCOL_NAMES = {"HTTP", "HTTPS", "FTP"}`, `STREAM_NAMES = {"Live Stream", "After Race"}`.

## Arduino IDE auto-prototype gotcha when editing RaceDash.ino

Arduino IDE injects auto-generated function prototypes at the top of the translation unit, immediately after `#include` lines. Any type used in a function signature must be **declared above the first function**, or compilation fails with "X was not declared in this scope" on the auto-prototypes.

In RaceDash.ino: `enum SettingId`, `struct NumBounds`, and `struct KbKey` are forward-declared right after the includes for this reason. **`TrackPickerState` (and the `tp` global) is also defined early** — adjacent to the `TRACKS` table — so `handleTouch()` can reference `tp.scrollY` and `tp.dirty` for drag-scroll without ordering tricks.

`I2C_NUM_1` requires `#include <driver/i2c.h>` even though Wire.h transitively pulls it in some paths but not others.

## Planned: cloud upload pipeline (W5500 incoming)

When the W5500 module arrives, the **Teensy** side gets:

- **`NativeEthernet` lib** (paulstoffregen) for the W5500 — stable on Teensy 4.1
- **`OPEnSLab-OSU/SSLClient`** for HTTPS — wraps mbedtls, works with NativeEthernet
- **`bblanchon/ArduinoJson`** for the per-sample JSON serializer
- DHCP on boot, then NTP from `0.pool.ntp.org` once link-up
- Per-sample serializer producing **descriptive-key NDJSON** at 25 Hz:
  ```json
  {"t":1714942567.234,"fix":3,"sats":12,"lat":40.123456,"lon":-74.12345,
   "alt_m":123.4,"speed_mph":67.5,"heading_deg":123.4,"rpm":5800}
  ```
  (~220 bytes/sample × 25 Hz = ~5.5 KB/s = 20 MB/hr — fine over LTE/wired)
- **SD logging filename convention**: `/sessions/session_<unixtime>_<trackname>.ndjson`. Track name comes from the dash's `TRACK,<name>` line right before `REC,1`; falls back to `UNKNOWN` when not set.

### Cloud strategy

| Setting | Live (HTTP/HTTPS) | After Race (HTTP/HTTPS/FTP) |
| --- | --- | --- |
| Endpoint | `https://<host>:<port>/stream` per-sample POST | `https://<host>:<port>/upload` whole-file POST, OR FTP PUT to `/incoming/<filename>` |
| Auth | `Authorization: Basic <user:pass>` | same (or anonymous FTP) |
| Headers | `Content-Type: application/x-ndjson` | + `X-Session-Id: <unixtime>`, `X-Track-Name: <approx>` |
| Failure | each failed POST falls into the queue | move file to `/queue/` |

**FTP only supports After Race mode** (FTP isn't designed for streaming). If `Live Stream` is selected with FTP, fall through to After Race semantics.

### Outbound queue (offline / poor-connectivity tolerance)
`/queue/session_*.ndjson` on SD card. On every boot or link-up event:
1. Walk the queue oldest-first
2. Attempt upload of each file
3. Delete on success; leave for next time on failure
4. Mid-session live-stream failures also dump the rest of the file into `/queue/`

This means the car can have zero connectivity at the track and still get all data — power on at home with internet and the dash flushes the backlog.

### NTP sync
On Teensy boot: DHCP → NTP query to `0.pool.ntp.org` → set the Teensy's RTC. GPS time is the secondary source (UTC from PVT). When neither NTP nor GPS-fix is available, fall back to `millis()`-relative timestamps until one becomes available.

## Layout

```
src/main.cpp                              Teensy: GNSS + tach + REC/TRACK consumer + (TODO) W5500 cloud client
platformio.ini                            Teensy build (do NOT add a CrowPanel env)
crowpanel-arduino/RaceDash/RaceDash.ino   CrowPanel: dash UI + settings + keyboards + track picker (LIVE)
crowpanel-arduino/PanelTest/PanelTest.ino Bare panel bring-up sketch — known-good baseline
crowpanel-baseline/                       Dead PIO experiments — do not touch
crowpanel-ui/                             Dead PIO experiments — do not touch
_vendor/CrowPanel-ESP32-Display-Course-File/   Elecrow's reference source, all revs (cloned for offline use)
LVGL_Library.pdf                          Generic upstream LVGL 9.0 docs (NOT Elecrow-specific, useless)
```
