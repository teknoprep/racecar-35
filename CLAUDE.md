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

**Multi-board (one source tree, THREE display panels).** `RaceDash.ino` is panel-agnostic; the
only board-specific values (RGB pin map, panel timing + sync polarities, touch I2C pins,
backlight method, LCD-reset method) live in `crowpanel-arduino/RaceDash/board_config.h`,
selected at compile time by `-DDASH_BOARD`. **Each panel also needs its own FlashSize** in the
FQBN:

| `-DDASH_BOARD` | board id | panel | FlashSize | notes |
| --- | --- | --- | --- | --- |
| `7` (default) | `crowpanel7` | CrowPanel 7.0" V3.0 (Basic RGB) | `4M` | PCA9557 reset, touch 19/20, GPIO2 PWM backlight |
| `5` | `crowpanel5` | CrowPanel 5.0" V3.0 (Basic RGB) | `4M` | same family as 7", different pin map/porches |
| `51` | `crowpanel5adv` | CrowPanel **Advance** 5.0" (HMI IPS, N16R8) | `16M` | inverted sync polarity, touch 15/16, **I2C 0x30 coprocessor** backlight, NO PCA9557, GPIO 38 is an RGB data pin |

Use `compiler.cpp.extra_flags` (empty by default), NOT `build.extra_flags` (which carries the
required USB-mode flags — overriding it bricks the build). Use a distinct `--build-path` per
board so the define can't cross-contaminate the cache. Sources of truth for the pin maps:
Elecrow V3.0 course file `gfx_conf.h` (`CrowPanel_70`/`CrowPanel_50`) for the Basic panels, and
the `Elecrow-RD/CrowPanel-Advance-5-...` repo (`example/V1.2_and_V1.3/.../LovyanGFX_Driver.h` +
`main.cpp`) for the Advance. The 7" `freq_write` is kept at our field-proven 15 MHz (vendor uses
12 MHz). **Identity is baked at the first USB flash** — there is no runtime panel auto-detect (a
wrong RGB-timing guess = an unrecoverable black screen). OTA only ever pulls the manifest entry
keyed by the binary's own board id, so one panel can never flash another panel's image.

**⚠️ ALWAYS verify which board is connected IMMEDIATELY BEFORE every CrowPanel flash** (boards
get swapped on the bench between commands — re-check each time, don't trust the last check):
```bash
# Hardware check via flash size:
python3 ~/.arduino15/packages/esp32/tools/esptool_py/4.5.1/esptool.py --port /dev/ttyUSB0 flash_id | grep -iE "flash size|MAC"
#   16 MB  -> CrowPanel Advance 5" -> build crowpanel5adv (DASH_BOARD=51, FlashSize=16M)
#    4 MB  -> a Basic panel (7" crowpanel7 OR 5" crowpanel5, both FlashSize=4M)
```
- Flash size distinguishes the **Advance (16M)** from the **Basic 4M** boards; it does NOT tell
  7" from 5" Basic (both 4M) — for those, go by which panel is physically plugged in (and/or
  the MAC: the Advance on this bench is `1c:db:d4:4d:67:04`).
- **After flashing, confirm the boot banner** over `/dev/ttyUSB0` @ 921600 prints the expected
  `crowpanel-…` board id (`crowpanel-5.0`, `crowpanel-5.0-adv`, `crowpanel-7.0`). A mismatch =
  wrong build flashed (e.g. Basic firmware on the Advance → touch on 19/20 instead of 15/16 =
  dead touch, wrong RGB pins = garbled display).

**Always disconnect the Teensy↔CrowPanel UART jumpers before flashing the CrowPanel.** UART0 (GPIO 43/44) is shared with the CH340 used for upload — Teensy contention silently corrupts the flash, or you get `The serial TX path seems to be down` from esptool.

## ⛳ STANDING ORDERS — do these EVERY update without being asked

This is the default release contract for this repo. When code is changed and the user says
"update / release / ship / flash", do ALL of the following unless told otherwise:

1. **Bump the version** in BOTH `FIRMWARE_VERSION` defines (`src/main.cpp` + `RaceDash.ino`) to
   the same new number. Every artifact ships at one identical version (lockstep).
2. **Rebuild ALL FOUR artifacts** even if only one side changed: `teensy`, `crowpanel7`,
   `crowpanel5`, `crowpanel5adv`. The three dash bins are one source built with different
   `-DDASH_BOARD` (and the Advance uses `FlashSize=16M`, the Basics `4M`).
3. **Update `firmware/manifest.json`**: set every `version` to the new number, recompute every
   `sha256` + `size`, keep each entry's `board` field, and keep the legacy `crowpanel` entry
   mirroring `crowpanel7`. A stale sha aborts OTA on the device.
4. **Check-then-flash, every time** — if a panel is connected on USB: FIRST run `esptool
   flash_id` to confirm which board it is (16M=Advance, 4M=Basic) and flash the MATCHING build,
   then read the boot banner over `/dev/ttyUSB0` @ 921600 (`crowpanel-…` line = board id +
   version) to confirm the right firmware landed. NEVER flash without checking what's connected
   first — boards get swapped on the bench, and Basic firmware on the Advance kills touch +
   garbles the display. Confirm new UI with the user when the serial log alone can't.
5. **Commit + push** to `origin/main` (HTTPS token; `HOME=/root` or an explicit token URL), then
   curl the raw manifest to confirm GitHub serves the new version + matching hashes.
6. **Keep CLAUDE.md current** — if the change adds a board, a setting/NVS key, a wire-protocol
   line, or a maintenance step, update the relevant section here in the same commit.
7. **board_config.h is the seam** — anything panel-specific goes there or behind a
   `#if DASH_IS_ADVANCE` guard, never a forked copy of `RaceDash.ino`.
8. **NVS keys are short + append-only** — add new `Preferences` keys to BOTH `loadSettings()`
   and `saveSettings()`; never repurpose an existing key's meaning.

## OTA release — ⚠️ ALWAYS bump ALL FOUR artifacts to the SAME version

The dash OTA pulls `firmware/manifest.json` from GitHub raw and flashes whichever artifact's
manifest `version` is newer than what's installed. There are **FOUR** release artifacts —
`teensy`, `crowpanel7` (7" Basic), `crowpanel5` (5" Basic), `crowpanel5adv` (5" Advance IPS) —
plus a legacy `crowpanel` alias (below). **They MUST always be released at the SAME version
number, even if only one side's code changed.** If you touch *any* of `src/main.cpp`,
`RaceDash.ino`, or `board_config.h`, you rebuild *all four*, bump *both* `FIRMWARE_VERSION`
defines to the same number, and republish *all four* artifacts.

Each CrowPanel binary carries its board id (`crowpanel7`/`crowpanel5`/`crowpanel5adv`) baked in
at compile time, reads only the manifest entry keyed by that id, and refuses an entry whose
`board` field doesn't match (belt-and-suspenders against a mis-pointed URL). The legacy
`crowpanel` key mirrors `crowpanel7` so already-deployed 0.1.60 7" units (whose old firmware
reads the bare `crowpanel` key) can still OTA forward; keep it pointing at the 7" bin.

Full publish procedure (Linux host; `$HOME` is unset in this shell — prefix git ops with
`HOME=/root` OR push to an explicit token URL):
```bash
NEW=0.1.63   # pick the next version

# 1. bump BOTH version defines to the same number
sed -i "s/#define FIRMWARE_VERSION .*/#define FIRMWARE_VERSION \"$NEW\"/" src/main.cpp
sed -i "s/#define FIRMWARE_VERSION .*/#define FIRMWARE_VERSION \"$NEW\"/" crowpanel-arduino/RaceDash/RaceDash.ino

# 2. build Teensy -> firmware/teensy41-dash.hex
~/.local/bin/pio run && cp .pio/build/teensy41/firmware.hex firmware/teensy41-dash.hex

# 3. build ALL THREE dash variants (APP bin RaceDash.ino.bin). Distinct --build-path each.
#    NOTE the per-board FlashSize: Advance=16M, Basics=4M.
cli=~/.local/bin/arduino-cli
base="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none"
$cli compile --fqbn "${base},FlashSize=4M"  --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=7"  --build-path /tmp/rd7_build  --output-dir /tmp/rd7_out  crowpanel-arduino/RaceDash
$cli compile --fqbn "${base},FlashSize=4M"  --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=5"  --build-path /tmp/rd5_build  --output-dir /tmp/rd5_out  crowpanel-arduino/RaceDash
$cli compile --fqbn "${base},FlashSize=16M" --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=51" --build-path /tmp/rdadv_build --output-dir /tmp/rdadv_out crowpanel-arduino/RaceDash
cp /tmp/rd7_out/RaceDash.ino.bin   firmware/crowpanel7-dash.bin
cp /tmp/rd5_out/RaceDash.ino.bin   firmware/crowpanel5-dash.bin
cp /tmp/rdadv_out/RaceDash.ino.bin firmware/crowpanel5adv-dash.bin

# 4. update firmware/manifest.json: every version=$NEW, recompute every sha256+size, keep each
#    entry's "board" field, keep legacy "crowpanel" == "crowpanel7".
sha256sum firmware/teensy41-dash.hex firmware/crowpanel7-dash.bin firmware/crowpanel5-dash.bin firmware/crowpanel5adv-dash.bin
stat -c%s  firmware/teensy41-dash.hex firmware/crowpanel7-dash.bin firmware/crowpanel5-dash.bin firmware/crowpanel5adv-dash.bin

# 5. commit + push, then verify GitHub raw serves the new manifest + matching hashes
HOME=/root git add src/main.cpp crowpanel-arduino/RaceDash/RaceDash.ino crowpanel-arduino/RaceDash/board_config.h firmware/
HOME=/root git commit -m "Release v$NEW: <what changed>"
HOME=/root git push origin main
curl -s https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/manifest.json
```
The manifest's version checks are independent in code, but **operationally they are
lockstep** — keep all four (+ the legacy alias) equal.

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
  - **Pin 9**: tach input via opto (`FreqMeasureMulti` FlexPWM2_2_B input capture). Used only when `sensor_type == 0` (Direct); MS3Pro CAN supplies RPM when `== 1`. **⚠️ Do NOT use the plain `FreqMeasure` lib here** — on T4.x (`__IMXRT1062__`) it is hard-wired to **pin 22** (FlexPWM4 CH0-A, = our CAN1 TX) and silently ignores pin 9, so it reads 0 forever. `FreqMeasureMulti.begin(9)` is the only thing that actually captures on pin 9. (This bit us hard: looked like a wiring problem for ages.)
  - **CAN1** (TX 22, RX 23): **MS3Pro MegaSquirt CAN bus** via SN65HVD230 transceiver. See "MS3Pro CAN" section.
  - **Wire / I²C** (SDA 18, SCL 19): MPU-6050 IMU (AD0→GND ⇒ addr 0x68)
  - **A2** (pin 16): oil-pressure transducer (0.5–4.5 V via 10k/20k divider). **A3** (pin 17): coolant NTC thermistor (150 Ω pull-up). Used in Direct sensor mode.
  - **SPI0** (CS 10, MOSI 11, MISO 12, SCK 13): **W5500 ethernet** (when installed)
  - **Pin 5**: W5500 `/INT` (planned)
  - **Pin 6**: W5500 `/RESET` (planned)
  - **SDIO (built-in)**: SD card (pins are dedicated, not on the header)
- **Optocoupler tach front-end** — PC817-class is fine for typical 4-cyl × 2-pulse-per-rev (≤ 270 Hz at 8 k RPM). The Teensy side needs a 4.7–10 kΩ pull-up from `3V3` to pin 9. Output is *inverted* but `FreqMeasureMulti` counts edges either way. **The signal at pin 9 must be a clean 0→3.3 V logic swing referenced to Teensy GND**: LOW must drop below ~0.8 V (opto transistor fully saturating) and HIGH above ~2.3 V, and it must never go negative. A symptom we hit: the opto not pulling fully low (line sitting ~1.5 V), which reads as a constant HIGH and RPM=0 even though a scope shows a waveform — verify the LOW level and a common ground, not just "there's a signal."

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
- **GPS UART RX buffer (v0.1.66 fix) — DO NOT remove.** Serial2's default RX ring is only tens of bytes — SMALLER than one ~100-byte UBX-NAV-PVT frame. GPS is parsed by *polling* `getPVT()` in `loop()` (RPM is NOT — CAN + `FreqMeasureMulti` are interrupt/FIFO-driven). So any loop stall >~25 ms overflows the ring, desyncs the autoPVT stream, and with a sub-frame buffer it can NEVER re-sync → GPS "freezes at last position" (STALE) while RPM keeps running. The stalls come from **SD `sync()`/`write()` latency during recording** (this is why it only happened once a session started, after "half a lap or two"). Fix: `Serial2.addMemoryForRead(gpsRxBuf, 32768)` in `setup()` BEFORE `begin()` (mirrors the dash Serial3 buffer) — ~8.5 s of slack at 38400 baud so the parser rides through SD spikes. `addMemoryForRead()` is on the concrete `Serial2`, not the `HardwareSerial&` alias.

### CrowPanel → Teensy (control)
```
REC,<0|1>          # start/stop recording
TRACK,<name>       # set the current track name (sent immediately before REC,1)
CFG,<key>,<value>  # push settings (incl. srctyp = sensor_type, cloud config, inet).
                   #   NOTE: cl_strm REMOVED in v0.1.66 (live "stream to cloud"
                   #   deleted). Teensy ignores cl_strm if an old dash sends it.
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
| ~~`cl_strm`~~ | — | **REMOVED in v0.1.66.** Live "stream to cloud" was deleted (not ready, and its mid-session POSTs stalled the loop → GPS STALE). Cloud recording is now **always After Race**. Old stored key is orphaned/harmless. |
| `cl_email` / `cl_key` | string | Cloud user email (X-User-Email) + API key (X-API-Key, masked). Migrated from legacy `cl_user`/`cl_pass`. |
| `auto_trk` | bool | Auto-select closest track on START (skip picker if a clear match exists) |
| `inet` | uint8 | Internet routing: 0=Ethernet (Teensy/W5500), 1=WiFi (CrowPanel) |
| `wssid` / `wpass` | string | WiFi SSID / PSK |
| `s_temp` / `t_warn` / `t_col` | bool/uint16/uint8 | Coolant show / warn-°F / warn-colour |
| `s_psi` / `p_warn` / `p_col` | bool/uint16/uint8 | Oil-PSI show / warn-PSI / warn-colour |
| `srctyp` | uint8 | **Sensor source: 0=Direct (opto tach + ADC), 1=MegaSquirt (CAN)** |
| `rpmppr` | uint16 | **Tach pulses/rev ×10** (Direct-mode RPM divider). 20=2.0. Sent to Teensy as `CFG,rpmppr,<x10>`; Teensy divides the opto-tach frequency by `rpmppr/10`. Ignored in MegaSquirt mode (RPM is straight from CAN). |
| `s_afr` / `afr_lo` / `afr_hi` / `afr_col` | bool/uint16/uint16/uint8 | AFR show / rich-warn×10 / lean-warn×10 / colour (MS3 mode only) |
| `tz` | uint8 | Timezone index into `TIMEZONES[]` |
| `sf_ovr` | blob | **Per-track start/finish overrides** — array of `{used,lat,lon}` sized `N_TRACKS`, keyed by `TRACKS[]` index. Set from the STATUS-page **SET START/FINISH** button (captures current GPS as that track's S/F line); `effectiveSf()` prefers it over the baked approximate `sf_lat/sf_lon`. Loaded in `loadSettings()`, written by a dedicated `saveSfOverrides()` (NOT `saveSettings()`, since it's mutated from the status page, not the settings-save path). Blob is restored only if its byte length still matches `sizeof(sfOverride)` — **TRACKS[] is append-only** (inserting a track mid-array shifts existing overrides onto the wrong track). |

`clampInvariants()` enforces `rpm_min < rpm_max`, `alert1_rpm < alertmax_rpm`, `alert1_hz < alertmax_hz` after every mutation.

## Dash UI architecture (RaceDash.ino)

### Pages
| Page | Entered via | Purpose |
| --- | --- | --- |
| `PAGE_DASH` | swipe ←-direction from settings | RPM bar (top), HUGE Font7 speed (right side at x=600), HDG/LAT/LON left column, FIX/SATS/GPS right column, START/STOP button left of speed |
| `PAGE_SETTINGS` | swipe →-direction from dash | Scrollable list of settings rows |
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

### Lap timer / predictive / delta (dash-only, GPS-derived)
Lap timing lives entirely in `RaceDash.ino` (`updateLapTimer()`), runs whenever
there's a GPS fix at a known track regardless of REC state, and is **display-only**
(it is NOT written into the Teensy SD/cloud NDJSON). Middle dash column shows
`PRED` / `LAP` / `DELTA`.
- **Start/finish detection**: distance to the track's S/F line (override if set,
  else baked) within `LAP_RADIUS_KM` (75 m). First clean crossing arms timing;
  each subsequent crossing records a lap. `MIN_LAP_MS` (15 s) floor.
- **Predictive = "ghost lap" method.** The session-best lap is snapshotted as a
  time-vs-distance table (`lapRefBt[]`, ~8 m buckets, `LAP_BUCKET_MI`). The live
  `liveDeltaMs()` compares the current lap's elapsed time to the ghost at the
  **same distance into the lap** (interpolated); `PRED = best_lap + delta`. Needs
  one complete lap to seed the ghost (lap 1 shows `--`). Bucket tables are kept
  **outside** the `LapTimer` struct so `lapTimer = LapTimer{}` stays a cheap
  scalar reset (a ~10 KB temporary on the loopTask stack would risk overflow).
- **Colours (PRED + DELTA): green/white/red** = faster / same (within
  `DELTA_SAME_MS` = 50 ms) / slower; grey `--` until a ghost lap exists.
- The **baked `sf_lat/sf_lon` are approximate** — use the STATUS-page SET
  START/FINISH capture (NVS `sf_ovr`) to pin the real line per track on-site.

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
Add to `enumValue()`, the appropriate `_NAMES[]` array, and the cycle case in `handleSettingsTap()` ENUM branch. `PROTOCOL_NAMES = {"HTTP", "HTTPS", "FTP"}`. (The `STREAM_NAMES` / `ST_CL_STREAM` Live-vs-AfterRace enum was removed in v0.1.66.)

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
resets fields to `-1` if the bus goes silent. **FreqMeasureMulti (opto tach) is kept alongside CAN**
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

## IMU (MPU-6050) — wiring + boot auto-calibration

GY-521 / MPU-6050 on the Teensy's **default `Wire` bus (I2C0) = pin 18 SDA, pin 19 SCL**,
400 kHz, address **0x68** (`AD0` → GND). Read as a raw 14-byte burst from `0x3B` at ~250 Hz,
averaged into each emit window (`readIMU()` / `flushImu()` in [src/main.cpp](src/main.cpp)),
emitted as the `IMU,ax,ay,az,gx,gy,gz` wire line. Accel ±2 g (16384 LSB/g), gyro ±250 °/s
(131 LSB/°s), DLPF 44 Hz (`CONFIG=0x03`).

**Wiring (the part that bit us — SDA/SCL get swapped, or wired to the wrong I2C pair):**

| GY-521 | → Teensy 4.1 | note |
| --- | --- | --- |
| VCC | **3.3V** | NOT 5V — Teensy GPIO isn't 5V-tolerant; the module's pull-ups reference VCC |
| GND | GND | common ground is mandatory |
| **SDA** | **pin 18** | I2C0 SDA (not 17=Wire1, not 25=Wire2) |
| **SCL** | **pin 19** | I2C0 SCL (not 16=Wire1, not 24=Wire2) |
| AD0 | GND | leaving it high/floating → addr 0x69 → "MPU-6050 NOT found" |

The Teensy 4.1 has **three** SDA/SCL pairs; only `Wire`=18/19 works here. Pins 16/17 (`Wire1`)
are already the oil/coolant ADC (A2/A3). Boot banner says exactly which way it went:
`MPU-6050 ready ...` (detected) vs `MPU-6050 NOT found on Wire (SDA=18, SCL=19)`.
**Detection is one-shot at boot** — if the MPU doesn't ACK that boot, `imu_present` stays false
and every `IMU` line is `0.00,...,0.0` for the whole session (the all-zeros signature = not
detected, NOT a calibration artifact). We saw intermittent non-detection on a jostled DuPont
jumper — if zeros show up sporadically, re-seat/solder the IMU header before suspecting code.

### Boot auto-calibration (`calibrateIMU()`) — gyro bias + accel scale, every boot
Runs once right after `setupIMU()` in `setup()`. **Chosen design (Option B):**
- **Gyro: full bias removal, every boot.** True at-rest value is 0,0,0, so bias = mean of the
  window, SUBTRACTED on every read. Re-done each boot because MPU-6050 gyro zero-rate drifts
  with temperature (a stored one-time cal would be wrong on the next cold start). We measured
  ~26 °/s bias on this unit → cal drives it to ~0.
- **Accel: scale-normalize to 1 g, every boot.** `a_scale = 1 / |a_measured|`, MULTIPLIED on
  every read so gravity reads exactly 1.00 g. **Orientation-independent** — it does NOT assume
  the car is level or that any particular axis is "up", so a boot on a slope is fine. It fixes
  the magnitude (we measured 1.41 g → 1.00 g) but does NOT remove per-axis offset (that needs a
  6-position cal — deliberately deferred; can be added later as a stored EEPROM cal + dash cmd).
- **Stillness gate (the critical safeguard).** ~0.5 s window; if any axis peak-to-peak exceeds
  `GYRO_STILL_DPS` (2 °/s) or `ACCEL_STILL_G` (0.10 g) the device is moving → the fresh cal is
  **REJECTED** and the last-good offsets stand. This stops a cal taken while idling/driving/
  bumped from corrupting the whole session. Banner prints `IMU cal ABORTED — device moving ...`
  when it trips.
- **EEPROM persistence.** Last-good offsets live in the Teensy's flash-emulated **EEPROM at
  address 0** (`ImuCalStore` = magic `0xCA15` + 3 gyro offsets + accel scale, 18 bytes).
  `loadImuCal()` runs first as the fallback; `saveImuCal()` (`EEPROM.put` == update) writes back
  only on an accepted cal. **EEPROM addr 0 is now reserved for IMU cal — nothing else on the
  Teensy uses EEPROM; if you add EEPROM storage, start past `sizeof(ImuCalStore)`.**
- **Bonus fix:** `readImuRaw()` buffers all 14 bytes before assembling the int16s, removing the
  old `(read()<<8)|read()` reliance on unspecified C++ operand evaluation order.

Verified at rest after flashing: `gyro ~0 °/s`, `|a| = 1.00 g`. If you ever need to wipe the
stored cal, reflash won't clear EEPROM — power-cycle with the IMU still and it self-recomputes,
or zero `ImuCalStore.magic`.

Still on the table (not done): lowering the on-chip DLPF (44 Hz → 10/21 Hz) for better
anti-aliasing before the 25 Hz decimation, and using the discarded MPU temperature bytes for
gyro temp-drift compensation. Heavy filtering / IMU↔GPS fusion is intended for the Docker
post-processing side, not the Teensy (keep the logged stream calibrated-but-raw).

## Libraries added this cycle
- **`TAMC_GT911`** (Arduino registry) — GT911 touch on Wire. Installed via
  `arduino-cli lib install "TAMC_GT911"`.
- **`FlexCAN_T4`** — ships with the Teensy core (no `lib_deps` entry needed). `FreqMeasureMulti`
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

### Cloud strategy — After Race only (live streaming removed v0.1.66)

Live "stream to cloud" (per-sample POST to `/stream` mid-session) was **deleted**
in v0.1.66: it wasn't ready, and its blocking POSTs stalled the Teensy loop long
enough to desync the GPS UART (see the GPS RX-buffer note above). The dash no
longer exposes a stream-mode setting and the Teensy has no live-POST code.

| | After Race (HTTP today; HTTPS/FTP NYI) |
| --- | --- |
| Endpoint | `http://<host>:<port>/upload` whole-file POST |
| Headers | `Content-Type: application/x-ndjson`, `X-API-Key`, `X-User-Email`, `X-Session-Id`, `X-Track-Name` |
| Flow | session file written into `/queue/` during recording (kill-switch insurance); uploaded after the session ends, **dash-driven** (`Q,LIST`/`Q,GET`/`Q,DEL`) or via the queue drain |
| Failure | file stays in `/queue/`, retried on the next dash-requested drain |

### Outbound queue (offline / poor-connectivity tolerance)
`/queue/session_*.ndjson` on SD card. On every boot or link-up event:
1. Walk the queue oldest-first
2. Attempt upload of each file
3. Delete on success; leave for next time on failure
4. Cloud-recorded sessions are written straight into `/queue/` (so a mid-session power cut still leaves a valid, uploadable file)

This means the car can have zero connectivity at the track and still get all data — power on at home with internet and the dash flushes the backlog.

### NTP sync
On Teensy boot: DHCP → NTP query to `0.pool.ntp.org` → set the Teensy's RTC. GPS time is the secondary source (UTC from PVT). When neither NTP nor GPS-fix is available, fall back to `millis()`-relative timestamps until one becomes available.

## Layout

```
src/main.cpp                              Teensy: GNSS + tach + REC/TRACK consumer + (TODO) W5500 cloud client
platformio.ini                            Teensy build (do NOT add a CrowPanel env)
crowpanel-arduino/RaceDash/RaceDash.ino   CrowPanel: dash UI + settings + keyboards + track picker + GT911 touch (LIVE, panel-agnostic)
crowpanel-arduino/RaceDash/board_config.h Per-panel RGB pin map/timing/backlight/touch (DASH_BOARD 7|5); 7"=crowpanel7, 5"=crowpanel5
crowpanel-arduino/RaceDash_v0139_orig/    Pre-touch-rework backup of RaceDash (swap/revert screens easily)
crowpanel-arduino/PanelTest/PanelTest.ino Bare panel bring-up sketch — display only, NO touch (not a touch baseline)
crowpanel-baseline/                       Dead PIO experiments — do not touch
crowpanel-ui/                             Dead PIO experiments — do not touch
firmware/                                 OTA artifacts + manifest.json: teensy41-dash.hex, crowpanel7-dash.bin, crowpanel5-dash.bin
_vendor/CrowPanel-ESP32-Display-Course-File/   Elecrow's reference source, all revs (cloned for offline use; gfx_conf.h block CrowPanel_50 = the 5" pin map/timing)
LVGL_Library.pdf                          Generic upstream LVGL 9.0 docs (NOT Elecrow-specific, useless)
```
