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
|  - track picker   |           |  - MS3Pro CAN (CAN1, 22/23) |
|  - touch (GT911)  |           |  - W5500 ethernet (SPI0)    |
|  - keyboards      |           |  - SD card (built-in slot)  |
|  - REC commands ──+--UART---->|  - IMU (MPU-6050, Wire)     |
+-------------------+           |  - cloud uploader + queue   |
                                |  - NTP from 0.pool.ntp.org  |
                                +-----------------------------+
```

UART is **bidirectional**:
- Teensy → CrowPanel: `GPS,...`, `ENG,...`, `ECU,...`, `IMU,...`, `SD,...`, `CLD,...`, `CANSNIFF,...` lines (telemetry + status, 25 Hz with 1 Hz heartbeat)
- CrowPanel → Teensy: `REC,<0|1>`, `TRACK,<name>`, `CFG,<k>,<v>`, `CANSNIFF,<0|1>`, etc. (control)

> Most of the once-"planned" trunk features (W5500, SD logging, cloud queue, IMU, NTP) are
> **implemented now**. The newest additions (this work cycle) are the **MS3Pro CAN bus**
> (RPM/coolant/AFR) and a **CAN sniffer** for reverse-engineering the ECU broadcast layout.

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
- **GT911 capacitive touch — READ THE DEDICATED SECTION BELOW ("GT911 touch").** Short version: do **NOT** use LovyanGFX's built-in `Touch_GT911` — it runs on `I2C_NUM_1` and its init steals the GPIO 19/20 pin routing from `Wire`, killing touch. We drive the GT911 with **TAMC_GT911 on `Wire` (I2C_NUM_0)** instead, and the I²C address **varies by unit batch** (0x5D or 0x14) so we auto-detect it. SDA=19, SCL=20, RST not on a dedicated GPIO (GPIO 38 held low at boot per vendor).
- **Documents folder is OneDrive-redirected** to `C:\Users\ChrisRawlings\OneDrive - BlueCloud Consultants\Documents\` on this machine. Arduino libraries live there, NOT under `~/Documents/Arduino`. arduino-cli's `directories.user` is set to that OneDrive path.
- **PCA9557 library is not in the Arduino registry** — bundled in `_vendor/.../File/PCA9557/` and copied into the libraries dir with a hand-written `library.properties`.
- **COM port mapping on this machine:** COM3 = CrowPanel (CH340), COM4 = Teensy (USB-CDC native).
- **Teensy 4.1 pin assignments in use** — keep these straight before claiming a "free" pin:
  - **Serial2** (RX 7, TX 8): u-blox GNSS UART
  - **Serial3** (TX 14, RX 15): bidirectional dash link to CrowPanel UART0
  - **Pin 9**: tach input via opto (FreqMeasure input capture — *the only pin FreqMeasure works on for T4.x*). Used only when `sensor_type == 0` (Direct); MS3Pro CAN supplies RPM when `== 1`.
  - **CAN1** (TX 22, RX 23): **MS3Pro MegaSquirt CAN bus** via SN65HVD230 transceiver. See "MS3Pro CAN" section.
  - **Wire / I²C** (SDA 18, SCL 19): MPU-6050 IMU (AD0→GND ⇒ addr 0x68)
  - **A2** (pin 16): oil-pressure transducer (0.5–4.5 V via 10k/20k divider). **A3** (pin 17): coolant NTC thermistor (150 Ω pull-up). Used in Direct sensor mode.
  - **SPI0** (CS 10, MOSI 11, MISO 12, SCK 13): **W5500 ethernet** (when installed)
  - **Pin 5**: W5500 `/INT` (planned)
  - **Pin 6**: W5500 `/RESET` (planned)
  - **SDIO (built-in)**: SD card (pins are dedicated, not on the header)
- **Optocoupler tach front-end** — PC817-class is fine for typical 4-cyl × 2-pulse-per-rev (≤ 270 Hz at 8 k RPM). The Teensy side needs a 4.7–10 kΩ pull-up from `3V3` to pin 9. Output is *inverted* but FreqMeasure counts edges either way.

## Wire protocol (Teensy ↔ CrowPanel UART)

Bidirectional, 115200 8N1, line-oriented, `\n`-terminated.

Note: the live baud is **921600** (not 115200) for telemetry + file uploads — both sides must agree.

### Teensy → CrowPanel (telemetry + status)
Up to 25 Hz when GPS PVT is fresh; 1 Hz heartbeat fallback when not.
```
GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>,<gps_status>
ENG,<rpm>,<oil_psi_x10>,<coolant_f_x10>
ECU,<rpm>,<clt_f_x10>,<map_x10>,<tps_x10>,<afr_x10>,<iat_f_x10>,<bat_x10>
IMU,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
SD,REC,<0|1>,<file>,<samples>   (+ SD,READY/FMT/NONE/ERR/ACTIVE status forms)
CLD,<live_ok>,<queue_depth>
CANSNIFF,<0|1>,<file>,<frames>  (CAN sniffer status / live frame count)
TIME,<unix_epoch>               (RTC, 1 Hz)
VER,teensy,<semver>
```
- `gps_status`: `0=OFF`, `1=RAW`, `2=OK`, `3=STALE` (see `gpsStatus()`)
- **`ENG` rpm source depends on `sensor_type`**: Direct (0) = opto tach (FreqMeasure / `RPM_PULSES_PER_REV`); MegaSquirt (1) = MS3Pro CAN. `oil_psi_x10` is always the A2 ADC; `coolant_f_x10` is A3 NTC in Direct mode or CAN CLT in MS3 mode. The dash RPM bar always reads `eng.rpm`.
- **`ECU`** carries the full MS3Pro CAN dataset; the dash uses it for coolant/AFR/MAP/TPS when `sensor_type == 1`. `-1` in any field = not-received / fault → dash shows `---`.
- The dash parser tolerates short forms for back-compat (e.g. `ENG` with 1 or 3 fields).
- **Rate-limiting**: emit only when `myGNSS.getPVT(0)` returns true OR every 1 s. `getPVT(0)` is non-blocking — never use the blocking variant or the 25 Hz loop chokes.

### CrowPanel → Teensy (control)
```
REC,<0|1>          # start/stop recording
TRACK,<name>       # set the current track name (sent immediately before REC,1)
CFG,<key>,<value>  # push settings (incl. srctyp = sensor_type, cloud config, inet)
SETTIME,<unix>     # set Teensy RTC
TZ,<id>            # timezone id (for SD filenames / metadata)
SDFORMAT           # FAT32-format the SD card
CANSNIFF,<0|1>     # start/stop raw CAN capture to SD (see "CAN sniffer")
TESTSTART/TESTSTOP # synthetic-data session for pipeline testing
Q,LIST / Q,GET / Q,DEL / QUEUE,DRAIN / UPLOAD,CANCEL   # cloud queue control
FWUPDATE / VER?    # OTA + version query
```
Parsed in `handleDashCommand()` ([src/main.cpp](src/main.cpp)).

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
| `cl_email` / `cl_key` | string | Cloud user email (X-User-Email) + API key (X-API-Key, masked). Migrated from legacy `cl_user`/`cl_pass`. |
| `auto_trk` | bool | Auto-select closest track on START (skip picker if a clear match exists) |
| `inet` | uint8 | Internet routing: 0=Ethernet (Teensy/W5500), 1=WiFi (CrowPanel) |
| `wssid` / `wpass` | string | WiFi SSID / PSK |
| `s_temp` / `t_warn` / `t_col` | bool/uint16/uint8 | Coolant show / warn-°F / warn-colour |
| `s_psi` / `p_warn` / `p_col` | bool/uint16/uint8 | Oil-PSI show / warn-PSI / warn-colour |
| `srctyp` | uint8 | **Sensor source: 0=Direct (opto tach + ADC), 1=MegaSquirt (CAN)** |
| `s_afr` / `afr_lo` / `afr_hi` / `afr_col` | bool/uint16/uint16/uint8 | AFR show / rich-warn×10 / lean-warn×10 / colour (MS3 mode only) |
| `tz` | uint8 | Timezone index into `TIMEZONES[]` |

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

> Touch hardware is **TAMC_GT911 on Wire**, not LovyanGFX — see the **"GT911 touch"** section
> below. The 80 ms release debounce in `handleTouch()` is what makes swipes/drag work at all;
> don't remove it.

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

## GT911 touch — the hard-won truth (do not regress this)

Touch broke completely on a new CrowPanel unit after flashing, while the factory firmware
touched fine and our firmware touched fine on a *different* unit. After exhaustive
bus-level instrumentation, the real causes and the working solution:

### What does NOT work
- **LovyanGFX's built-in `Touch_GT911` (on `I2C_NUM_1`, SDA/SCL 19/20).** `tft.begin()` runs
  its touch init which routes `I2C_NUM_1` onto GPIO 19/20 via the GPIO matrix — **overwriting
  `Wire`'s `I2C_NUM_0` output routing on the same pins**. After that, *all* I²C reads return
  nothing. Empirically: GT911 product-ID read returned `"911"` **before** `tft.begin()`, then
  `got=0` / `eot=5` (timeout) **after**. This is why the original `cfg.i2c_addr=0x14`,
  `I2C_NUM_1`, `bus_shared=true` config silently fails on some units.
- Lowering the RGB pixel clock (15→12 MHz) or the I²C clock (400→100 kHz) does **not** help —
  it's a pin-routing conflict, not bandwidth.

### What DOES work (current implementation)
Replicate the **vendor V3.0 demo**: LovyanGFX for the RGB **display only** (no touch instance
in the `LGFX` class), and **TAMC_GT911 on `Wire` (I2C_NUM_0)** for touch. Only one I²C
peripheral ever owns pins 19/20, so there's no routing theft and reads work even with the
RGB DMA running.

Key points in `RaceDash.ino`:
- `#include <TAMC_GT911.h>`; global `TAMC_GT911 ts(19, 20, (uint8_t)-1, (uint8_t)-1, 800, 480);`
  (INT and RST = `-1`; the board handles reset, GPIO 38 held low at boot per vendor).
- The `LGFX` class has **no `Touch_GT911 _touch_instance`** and no touch config block.
- `setup()`: `Wire.begin(19,20)` → PCA9557 → `tft.begin()` (RGB only) → **then** probe I²C for
  `0x5D` then `0x14` and `ts.begin(<detected addr>)`. **Address varies by unit batch** —
  auto-detect, never hardcode.
- `ts.setRotation(ROTATION_INVERTED)` gives raw coords (x=x, y=y). `ROTATION_NORMAL` mirrors
  to the opposite corner (looks like "touch doesn't work"). If a future unit reads mirrored,
  switch the rotation constant.
- **TAMC's `read()` has an array-overflow bug** if I²C returns garbage (`touches = buf & 0x0F`
  can be 15, writing past `points[5]`). It only bites when reads fail — which they won't with
  the correct single-bus setup.

### Swipe / drag debounce (also critical)
The GT911 clears its data-ready flag on each read, so `ts.isTouched` **flickers false between
the controller's sample periods (~5–10 ms)** while a finger is held. Without handling this,
every flicker looks like a finger-up: the swipe/drag state machine resets and movement never
accumulates past the 120 px threshold — **taps work, swipes/scroll don't.** Fix in
`handleTouch()`: an **80 ms release debounce** — hold the touch "down" (retaining last X/Y)
for up to `TOUCH_RELEASE_MS` after the last real sample. Do not remove this.

### Backup
The pre-touch-work dash is preserved at `crowpanel-arduino/RaceDash_v0139_orig/` so a screen
can be swapped/reverted easily.

## MS3Pro CAN (MegaSquirt RPM / coolant / AFR)

The Teensy reads the MS3Pro ECU over **CAN1 (TX 22, RX 23)** via an **SN65HVD230 ("VP230")**
3.3 V transceiver — **NOT** an MCP2551 (that's 5 V and would damage the Teensy). The blue
breakout has an **onboard 120 Ω terminator** (silkscreen `R2 = 121`), so no external resistor
is needed; it must sit at the **end** of the bus (Teensy ↔ MS3Pro = correct).

Wiring: `3V3→3.3V, GND→GND, CTX(TXD)→pin 22, CRX(RXD)→pin 23, CANH→MS3Pro CAN-H,
CANL→MS3Pro CAN-L`.

Software (`src/main.cpp`): `FlexCAN_T4<CAN1, RX_SIZE_256, TX_SIZE_16> Can1;` at
`CAN_BAUD = 500000`, `CAN_BASE_ID = 0x5E8` (1512, MS3 "Simplified Dash" base). `pumpCAN()` parses frames and `CAN_STALE_MS = 2000`
resets fields to `-1` if the bus goes silent. **FreqMeasure (opto tach) is kept alongside CAN**
— both RPM sources are always running; `emitToDash()` picks one based on `sensor_type`.

### sensor_type switch (Direct vs MegaSquirt)
`Settings → Sensor Type` (NVS key `srctyp`, synced to Teensy via `CFG,srctyp,<0|1>` in
`sendCfgToTeensy()`):
- **0 = Direct**: RPM from opto tach (pin 9), coolant from A3 NTC. Oil PSI always from A2.
- **1 = MegaSquirt**: RPM + coolant + AFR + MAP + TPS + IAT + battery from CAN. AFR is only
  shown in MS3 mode. Oil PSI still from A2 (MS3Pro typically has no oil-pressure input).

### ✅ Broadcast byte offsets — VERIFIED (2026-06-03)
Layout confirmed against the **official MegaSquirt CAN Broadcast spec**
(`msextra.com/doc/pdf/Megasquirt_CAN_Broadcast.pdf`, "Simplified Dash Broadcasting") AND a
real sniffer capture: frame `0x5E8 = 03 FD 00 00 05 62 FF FF` decoded cleanly to MAP 102.1 kPa
/ RPM 0 / CLT 137.8 °F / TPS ~0 (a key-on, engine-off snapshot — that's why the payload was
static across the whole capture). Base ID is **1512 (0x5E8)**, NOT 1520/0x5F0 — sequential
addressing, big-endian, 500 kbps. `pumpCAN()` now parses:

| ID | bytes | field | scale |
|---|---|---|---|
| `0x5E8` | 0-1 | map | ÷10 kPa (int16) |
| `0x5E8` | 2-3 | **rpm** | ×1 (uint16) |
| `0x5E8` | 4-5 | **clt** | ÷10 °F (int16) |
| `0x5E8` | 6-7 | tps | ÷10 % (int16) |
| `0x5E9` | 4-5 | mat (IAT) | ÷10 °F (int16) |
| `0x5EA` | 1 | **AFR1** | ÷10 (uint8! 0-255 = 0.0-25.5) |
| `0x5EB` | 0-1 | batt | ÷10 V (int16) |

**AFR + AFR-target are single bytes**, not 16-bit (byte 0 = afrtgt1, byte 1 = AFR1). The rest
are 16-bit big-endian. TunerStudio setup: `CAN-Bus/Testmodes → Dash Broadcasting → Enable=On`,
`Automatic` mode (locks base to 1512, broadcasts 50 Hz). Temps assumed °F × 10 (depends on
TunerStudio units — re-verify if it's ever switched to °C).

> Note: only `0x5E8` was seen in the engine-off capture. The 1513-1516 (`0x5E9`-`0x5EC`)
> frames (IAT/AFR/batt) still want a **running-engine capture** to fully confirm, but they
> follow the same authoritative spec table above.

## CAN sniffer (Tools page → "Start CAN capture")

De-risks the byte-offset guessing: captures **every raw CAN frame** to SD for offline analysis.
- Dash: 4th button on `PAGE_TOOLS`. Tap toggles; sends `CANSNIFF,1` / `CANSNIFF,0`; shows live
  frame count. Greyed out without a mounted SD card.
- Teensy: `openCanSniff()` writes `/cansniff/cansniff_<unixtime>.csv`; `pumpCAN()` logs every
  frame (any ID) when active; 1 Hz flush + `CANSNIFF` status heartbeat back to the dash.
- CSV columns: `t_ms,id,ext,dlc,d0..d7` (data bytes in hex), e.g. `12,0x5F0,0,8,00,1A,0B,4E,...`.

Workflow: wire transceiver → enable TunerStudio broadcast → run engine → Tools → Start CAN
capture → vary RPM ~30 s → Stop → pull SD → decode which byte pairs track RPM/CLT/AFR → lock in
the real offsets in `pumpCAN()`.

## Libraries added this cycle
- **`TAMC_GT911`** (Arduino registry) — GT911 touch on Wire. Installed via
  `arduino-cli lib install "TAMC_GT911"`.
- **`FlexCAN_T4`** — ships with the Teensy core (no `lib_deps` entry needed). `FreqMeasure`
  is also a Teensy-core lib.

## Toolchain note (Linux build host)
This session built/flashed on Linux, not the Windows paths below:
- PlatformIO: `~/.local/bin/pio` (or `~/.local/share/pipx/venvs/platformio/bin/platformio`)
- arduino-cli: `~/.local/bin/arduino-cli`, `directories.user = ~/Arduino`
- Ports: **`/dev/ttyACM0` = Teensy**, **`/dev/ttyUSB0` = CrowPanel (CH340)**
- Teensy enumerates as HID/HalfKay for flashing (auto-detected by teensy-cli; press the
  button if "Error opening USB device").

### git push — `$HOME` is UNSET in this shell (the gotcha that bites every push)
The GitHub token is already stored at `/root/.git-credentials` with `helper = store` in
`/root/.gitconfig`. **But this shell starts with `$HOME` empty**, so git can't find either
file and you get `fatal: could not read Username for 'https://github.com'`. The fix is just
to set HOME for the git command:
```bash
HOME=/root git push origin main        # works — store helper supplies the token
HOME=/root git pull / fetch / etc.     # same for any auth'd git op
```
Remote is `https://github.com/teknoprep/racecar-35.git` (HTTPS, token auth). Don't bother
re-pasting the token or rewriting the remote URL — it's all configured; you only ever need
the `HOME=/root` prefix. (Or `export HOME=/root` once at the top of a session.)

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
crowpanel-arduino/RaceDash/RaceDash.ino   CrowPanel: dash UI + settings + keyboards + track picker + GT911 touch (LIVE)
crowpanel-arduino/RaceDash_v0139_orig/    Pre-touch-rework backup of RaceDash (swap/revert screens easily)
crowpanel-arduino/PanelTest/PanelTest.ino Bare panel bring-up sketch — display only, NO touch (not a touch baseline)
crowpanel-baseline/                       Dead PIO experiments — do not touch
crowpanel-ui/                             Dead PIO experiments — do not touch
_vendor/CrowPanel-ESP32-Display-Course-File/   Elecrow's reference source, all revs (cloned for offline use)
LVGL_Library.pdf                          Generic upstream LVGL 9.0 docs (NOT Elecrow-specific, useless)
```
