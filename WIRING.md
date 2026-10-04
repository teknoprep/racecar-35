# racecar-35 — Wiring Reference

Wiring reference for the existing two-MCU system. **WiFi-only on the CrowPanel
since v0.1.148: no Ethernet module or Internet-route setting.**
The current **solder-it-yourself** build is a protoboard, not Rev C:
[hardware/breadboard/](hardware/breadboard/) (terminals on the LEFT edge,
Teensy on the right, Pi 5 video box on Serial1). Rev C Gerbers stay parked.
**For the wire-by-wire build sheet — power, hand-built PC817 tach opto, GPS, IMU,
CAN, the passive netlist and the pre-power meter checks — use
[hardware/breadboard/PINOUT.md](hardware/breadboard/PINOUT.md).** Sections 5/5b/5c
and 9 below still describe the circuits, but the pinout sheet is the authoritative
build order. v0.1.149 firmware supports ONLY an external AEM 30-0300 gauge's
WHITE/BROWN analogue output through a reviewed scaled/protected input. No direct
oxygen sensor, onboard heater/controller or source-selection jumper.
See [CLAUDE.md](CLAUDE.md) and
[AFR_INTERFACE.md](hardware/teensy-integrated-revc/AFR_INTERFACE.md).

**New Rev C reservations (NOT wired or driven by current firmware):** on-board
ESP32-S3 WiFi over SPI0 (10/11/12/13 + IRQ5/reset6) and CR2032 backup to dedicated
Teensy VBAT/GND. Screen WiFi/OTA stays. See
[WIFI_RTC_ARCHITECTURE.md](hardware/teensy-integrated-revc/WIFI_RTC_ARCHITECTURE.md).
Never attach a cell to a generic GPIO; add the missing auxiliary VBAT contact.

```
Cabin (driver)                         Trunk (data + connectivity)
+-------------------+                 +-----------------------------+
|  CrowPanel ESP32  | <==UART (3 wires) ==>  Teensy 4.1            |
|  (cabin display)  |                 |  GPS (NEO-M9N)              |
|                   |                 |  Tach input (opto)          |
|                   |                 |  IMU (MPU-6050)             |
|  WiFi / NTP      |                 |  No network hardware        |
|                   |                 |  SD card (built-in slot)    |
+-------------------+                 +-----------------------------+
```

---

## 1. Teensy 4.1 — full pin assignments

| Teensy 4.1 pin | Function                          | Notes |
|---------------:|-----------------------------------|-------|
| **3.3V**       | Legacy GPS/IMU and logic pull-ups | Respect the MCU current budget; proposed carrier has a separate auxiliary rail |
| **GND**        | Common ground for everything      | Multiple GND pins on the header — any will do |
| **VIN / 5V**   | Power in (5 V, USB or ext)        | Or use USB-C |
| **5V**         | Output — feed downstream 5 V devices if needed | Same rail as VIN |
| Pin 0          | Serial1 RX1 — **from Pi 5 TX**    | Video box UART, 3.3 V, 115200. See [hardware/breadboard](hardware/breadboard/) |
| Pin 1          | Serial1 TX1 — **to Pi 5 RX**      | Crossed with pin 0; common GND; NEVER 5 V |
| Pins 5 / 6     | Proposed NET READY / reset        | Reserved for new WiFi coprocessor; not implemented |
| Pin 7          | Serial2 RX2 — **GPS TX**          | u-blox NEO-M9N TX → here |
| Pin 8          | Serial2 TX2 — **GPS RX**          | Optional (config); often unused |
| Pin 9          | **Tach input via FreqMeasureMulti** | Conditioned opto output only; plain FreqMeasure uses pin 22 and is WRONG here |
| Pins 10–12     | Proposed NET CS / MOSI / MISO      | Reserved for local WiFi SPI; not yet driven |
| Pin 13         | Current heartbeat / proposed SCK  | MUST disable heartbeat GPIO writes before SPI |
| Pin 14         | Serial3 TX3 — to CrowPanel `RX`   | Dash telemetry out (921600 baud) |
| Pin 15         | Serial3 RX3 — from CrowPanel `TX` | Dash commands in (REC, TRACK, TZ, SDFORMAT) |
| Pin 16 / A2    | **Oil pressure ADC**              | 0.5–4.5 V transducer via 10 kΩ / 20 kΩ divider |
| Pin 17 / A3    | **Coolant temp ADC**              | NTC thermistor with 150 Ω pullup to 3.3 V |
| Pin 18         | Wire SDA — **IMU SDA**            | I²C data |
| Pin 19         | Wire SCL — **IMU SCL**            | I²C clock |
| Pin 20 / A6    | AEM 30-0300 gauge-output ADC       | Opt-in `afraem`; requires protected 0.500 gain front end; NEVER raw 5 V |
| Pins 22 / 23   | CAN1 TX / RX                      | Used by MS3Pro; requires a CAN transceiver |
| (built-in SDIO)| **SD card slot**                  | Dedicated socket on the board, no header pins |

---

## 2. CrowPanel ESP32-S3 V3.0 — relevant pins

The CrowPanel uses most of its GPIOs internally for the LCD, touch, and
expander. The only thing **we** wire on the back-side header is UART0 to the
Teensy.

| CrowPanel pin | Function                       | Notes |
|--------------:|--------------------------------|-------|
| **3.3V / 5V** | Power in (USB-C or 5V pin)     | Don't share with Teensy unless you tie the grounds first |
| **GND**       | Must be **common with Teensy GND** | Critical — UART won't work without shared ground |
| GPIO 43       | UART0 TX → Teensy pin 15 (RX3) | Dash → trunk commands |
| GPIO 44       | UART0 RX ← Teensy pin 14 (TX3) | Trunk → dash telemetry |

> **Critical — UART0 is shared with USB upload.** Disconnect the two UART
> jumpers (43 / 44) before flashing the CrowPanel via `arduino-cli upload`.
> If the Teensy is driving GPIO 44 while esptool is trying to upload, the
> upload silently corrupts or fails with `The serial TX path seems to be down`.

---

## 3. UART telemetry link (Teensy ↔ CrowPanel)

Three wires total. **The TX/RX pair is crossed.**

On the prototype carrier / protoboard this lands on the screen's **J10** (XH2.54)
header, not bare GPIO pins:

| Teensy 4.1     | Direction | CrowPanel J10      |
|---------------:|:---------:|:-------------------|
| Pin 14 (TX3)   | →         | RXD0_H             |
| Pin 15 (RX3)   | ←         | TXD0_H             |
| GND            | —         | GND                |

Baud: **921600** 8N1 (not 115200 — both sides must agree), line-oriented with `\n` terminators. Wire format is
documented in [CLAUDE.md](CLAUDE.md#wire-protocol-teensy--crowpanel-uart).

---

## 4. GPS — u-blox NEO-M9N (or any UBX-compatible u-blox)

4 wires.

| u-blox module pin | Teensy 4.1 pin | Notes |
|------------------:|:---------------|-------|
| `VCC` (3V3)       | **3.3V**       | NEO-M9N is 3.3 V; SparkFun RTK boards have onboard regulator and accept 3.3–5 V |
| `GND`             | **GND**        |       |
| `TX`              | **Pin 7** (RX2) | Module sends UBX/NMEA *to* Teensy |
| `RX`              | **Pin 8** (TX2) | Optional — only used if you want to send config to the module |

Bauds tried at boot: **230400** (persisted by `saveConfiguration()` on a module we
have already configured, and the target rate), **38400** (SparkFun RTK default),
then **9600** (bare module factory default). The firmware then raises the link to
230400 so 25 Hz UBX-NAV-PVT has headroom (38400 was ~65 % utilised → chronic STALE).
If none handshakes the lib goes "raw bytes" mode and just observes Serial2.

Antenna: external active GPS antenna recommended for moving vehicle. SMA
connector on most modules.

---

## 5. Tach input — opto-isolated pulse to pin 9

The Teensy measures pulse frequency on pin 9 via `FreqMeasure` (FlexPWM
input capture — pin 9 is the *only* pin this works on for T4.x).

### Recommended front-end

```
Engine tach signal (12 V noisy)         Teensy 4.1
┌──────────────────────────┐            ┌──────────┐
│  Coil-neg / ECU tach out │            │          │
│         ─o─              │            │          │
│          │               │            │   3.3V o─┴──┐
│  ┌───────┴──────┐        │            │          │  │
│  │  PC817 opto  │        │            │   Pin 9  │  │
│  │   1───┐ ┌──4 │────────┼────────────┤──────────│──┤  
│  │       │ │    │        │            │          │  R = 4.7 kΩ – 10 kΩ pull-up
│  │   2───┘ └──3 │────────┼────────────┤   GND    │  │  (3.3V → pin 9)
│  └──────────────┘        │            │          ├──┘
│  R_in: 1k from tach      │            └──────────┘
│       → opto pin 1       │
│  Pin 2 → engine GND      │
└──────────────────────────┘
```

| Component           | Value                  | Notes |
|---------------------|------------------------|-------|
| Optocoupler         | PC817-class            | Plenty of margin for ≤270 Hz (4-cyl × 2-pulse-per-rev @ 8k RPM) |
| `R_in` (input side) | 1 kΩ                   | Limits LED current at 12 V (~11 mA) |
| `R_pullup` (output) | **4.7 kΩ – 10 kΩ**     | From 3.3 V to pin 9; required, opto output is open-collector |

Output is **inverted** (opto pulls pin 9 low when tach pulses), but
FreqMeasure counts edges either way — no software change needed.

`RPM_PULSES_PER_REV` in [src/main.cpp](src/main.cpp) defaults to **2.0**
(typical 4-cyl 4-stroke from coil-neg or ECU tach output). Calibrate against
a known idle RPM if it reads 2× / ½×.

---

## 5b. Oil pressure — generic 5 V 0.5–4.5 V transducer to pin 16 (A2)

3 wires from the transducer; **divider on the signal line is mandatory** (sensor is 5 V output, Teensy ADC is not 5 V tolerant).

```
Sensor 5V ──── Teensy 5V (or external clean 5V rail)
Sensor GND ─── Teensy GND (star point)
Sensor SIG ──┬─── R1 = 10 kΩ ──── Teensy pin 16 (A2)
             │                    │
             │                    R2 = 20 kΩ
             │                    │
             │                   GND
             └─── (optional 10 nF cap from A2 to GND, right at the pin)
```

Sensor output 0.5 V (0 PSI) → ADC 0.33 V; sensor 4.5 V (full scale) → ADC 3.00 V. Conversion math lives in `readOilPsiX10()` in [src/main.cpp](src/main.cpp); change `OIL_PSI_FULL_SCALE` if you swap to a 100 PSI or 200 PSI variant.

Wire colour check before trusting the listing photo: signal-to-GND should read **~0.5 V at atmosphere** when powered. If it reads ~0 V instead, you have a 0–5 V (not 0.5–4.5 V) variant — change `OIL_V_AT_ZERO_PSI` to 0.0f.

## 5c. Coolant temp — NTC thermistor (VDO 1600–22 Ω curve) to pin 17 (A3)

2-wire (with dedicated ground if the sender is single-terminal — do NOT rely on engine-block grounding through threads, the noise will trash readings).

```
                    Teensy 3.3V ─┬─── R_pullup = 150 Ω
                                 ├─── Teensy pin 17 (A3)
Sender signal ───────────────────┘
Sender body  ────── dedicated 18 AWG wire ──── Teensy GND (star)
```

Pullup is sized for ~22–700 Ω working range (full-scale span 100–250 °F). If using a different thermistor (GM-style ~3.3 kΩ at 100 °F, AEM 30-2014), bump the pullup to ~2.2 kΩ.

Calibration: the Steinhart-Hart `COOLANT_SH_A/B/C` constants in [src/main.cpp](src/main.cpp) are fitted to the typical VDO 1600–22 Ω curve. For your specific sender, measure R at three known temps (ice bath, room, boiling), feed into [the SRS NTC calculator](https://www.thinksrs.com/downloads/programs/therm%20calc/ntccalibrator/ntccalculator.html), and replace the coefficients.

## 6. IMU — MPU-6050 (GY-521 module)

4 wires. I²C address `0x68` (with AD0 tied low — most GY-521 boards have
this internally).

| GY-521 pin | Teensy 4.1 pin | Notes |
|-----------:|:---------------|-------|
| `VCC`      | **3.3V**       | Module has onboard regulator and accepts 3.3 or 5 V; 3.3 V is safer |
| `GND`      | **GND**        |       |
| `SCL`      | **Pin 19**     | Wire SCL |
| `SDA`      | **Pin 18**     | Wire SDA |
| `XDA`, `XCL`, `AD0`, `INT` | **leave floating** | Not used |

### Mounting orientation

For the dash to read forward/lateral G correctly, mount the GY-521 with:
- **board flat** (component side up)
- **header pins facing the rear** of the car

Then:
- **+X axis = forward** (longitudinal — braking/accel)
- **+Y axis = right** (lateral — right-hand turns read **negative** Ay)
- **+Z axis = up** (vertical — gravity ≈ −1.0 g on Az when level)

---

## 7. Networking — WiFi only (v0.1.148)

No Ethernet/W5500 module is used or probed. Set WiFi SSID/password on the
CrowPanel; those settings are always visible. Uploads and OTA use that WiFi link.
NTP runs on the screen and relays `SETTIME` over UART. Current source does NOT
set the RTC from GPS UTC (older documentation overstated that behaviour).
The WiFi/BLE arbiter still turns WiFi off while Bluetooth owns the radio.

Old saved `inet=0` selections are ignored. The new dash sends `CFG,inet,1` only
for compatibility with old Teensy firmware. Retired `ETH,` lines cannot overwrite
the screen's WiFi address/status.

---

## 8. SD card

The Teensy 4.1 has a **built-in SDIO socket** on the back of the board.
Insert a FAT32-formatted micro-SD card. No external wiring needed.

If the card has no filesystem, the dash settings page surfaces a
"Format SD card" action. The Teensy formats it in place via `FatFormatter`
on `SDFORMAT` command.

---

## 9. Power

| Source        | Goes to                                           |
|---------------|---------------------------------------------------|
| 12 V battery  | DC-DC step-down to 5 V (~2 A)                     |
| 5 V (regulated) | Teensy `VIN` (or USB-C if bench testing)        |
| 5 V (regulated) | CrowPanel USB-C input (or 5 V pin)              |
| Teensy `3.3V` | Legacy GPS, IMU and tach pull-up within the rail budget |
| **Common GND**| **All grounds tied together** — engine, Teensy, CrowPanel, every module |

> **Star-ground rule of thumb:** run separate ground wires from each module
> back to a single common point at the Teensy GND header. Don't daisy-chain
> grounds through the engine block; tach noise will couple into the GPS.

---

## 10. Quick wiring checklist

When rebuilding:

- [ ] 5 V supply common to Teensy + CrowPanel
- [ ] All grounds tied together at one star point
- [ ] UART crossover: T14↔C44, T15↔C43, plus shared GND (3 wires)
- [ ] GPS: 4 wires (VCC, GND, TX→T7, RX←T8)
- [ ] Tach: opto front-end + 4.7–10 kΩ pull-up from 3.3 V to T9
- [ ] Oil PSI: 5V, GND, signal via 10 kΩ/20 kΩ divider → T16 (A2)
- [ ] Coolant temp: 150 Ω pullup from 3.3 V → T17 (A3); dedicated body ground
- [ ] IMU: 4 wires (VCC, GND, SCL→T19, SDA→T18)
- [ ] WiFi SSID/password configured on the CrowPanel; no Ethernet module
- [ ] SD card inserted in Teensy 4.1 built-in socket
- [ ] **Disconnect the UART jumpers before flashing the CrowPanel**

---

## 11. COM port mapping (Windows dev machine)

| COM | Device                         | Used by                           |
|-----|--------------------------------|-----------------------------------|
| COM3| CrowPanel CH340 USB-UART       | `arduino-cli upload`, ESP32 monitor |
| COM4| Teensy native USB-CDC          | PlatformIO upload + monitor       |

Pinned in [platformio.ini](platformio.ini) (`monitor_port = COM4`) so the
PIO monitor can't grab the wrong port when both are plugged in.

---

## 12. Teensy 4.1 pinout — quick reference

Orientation: USB-C at the top, component side facing you, SD card slot at the
bottom (built-in SDIO socket on the back of the board). Pin numbers are the
GPIO numbers used in code (`pinMode(N, ...)`). Labels on the outside are the
peripheral alt-functions; `[bracketed]` callouts are the pins **this project
currently uses**.

```
                              +-------[USB-C]-------+
                         GND -|                     |- 5V (VIN)
     [Video Pi] RX1    0 -|                     |- GND
     [Video Pi] TX1    1 -|                     |- 3.3V
                           2 -|                     |- 23   A9          [CAN1 RX]
                           3 -|                     |- 22   A8          [CAN1 TX]
                           4 -|                     |- 21   A7 / RX5
                           5 -|                     |- 20   A6 / TX5    [AEM AFR via scaled/protected input]
                           6 -|                     |- 19   A5 / SCL    [IMU SCL]
       [GPS] RX2           7 -|                     |- 18   A4 / SDA    [IMU SDA]
       [GPS] TX2           8 -|     T E E N S Y     |- 17   A3 / TX4    [Coolant temp ADC]
     [Tach in]             9 -|        4 . 1        |- 16   A2 / RX4    [Oil PSI ADC]
                          10 -|                     |- 15   A1 / RX3    [Dash RX  <- CrowPanel TX]
                          11 -|                     |- 14   A0 / TX3    [Dash TX  -> CrowPanel RX]
                          12 -|                     |- 13   SCK / LED   [Heartbeat LED]
                        3.3V -|                     |- GND
                          24 -|                     |- 41   A17
      Serial6 TX          25 -|                     |- 40   A16
      Serial6 RX          26 -|                     |- 39   A15
                          27 -|                     |- 38   A14
      Serial7 RX          28 -|                     |- 37
      Serial7 TX          29 -|                     |- 36
                          30 -|                     |- 35
                          31 -|                     |- 34
                          32 -|                     |- 33
                              |                     |
                              |   +-------------+   |
                              |   |             |   |
                              |   |     S D     |   |
                              |   |   (SDIO,    |   |
                              |   |   built-in) |   |
                              |   +-------------+   |
                              +---------------------+
```

### What's free vs spoken for

| Status | Pins | Notes |
|--------|------|-------|
| **In use** | 7, 8, 9, 13, 14, 15, 16, 17, 18, 19, 22, 23, SDIO | CAN1 uses 22/23; network module removed |
| **Opt-in AFR** | 20 / A6 | AEM gauge output through reviewed 0.500 gain front end; no internal pull |
| **Reserved for new WiFi** | 5, 6, 10–13 | Local SPI/READY/reset proposal; no new driver yet |
| **No firmware assignment** | 0–4, 21, 24–41 | Review carrier/peripheral wiring before reuse |
| **Caveat** | Pins 16/17 used as analog (A2/A3) | Serial4 (RX4/TX4) is no longer available |
| **Caveat** | Pin 13 doubles as SPI0 SCK and the on-board LED | Stop heartbeat GPIO writes before enabling new WiFi SPI |
| **FreqMeasureMulti** | Pin 9 | The configured tach pin; plain FreqMeasure incorrectly uses CAN TX pin 22 |

### Common alt-functions for currently-unused pins

If you need to add something, these are the natural pin choices:

| Function | Best free pin | Alt |
|----------|---------------|-----|
| Another UART | Serial6 (pins 24=TX, 25=RX) | Serial7 (28=RX, 29=TX) |
| Another I²C bus | Wire1 (SDA 17 / SCL 16) is already occupied by the ADC inputs | Wire2 (SDA 25 / SCL 24); verify any other allocations first |
| Another SPI bus | SPI1 (pins 26=MOSI1, 27=SCK1, 39=MISO1) | — |
| More ADC inputs | A7 (21) | A6 (20) reserved for AEM AFR; 22/23 already used by CAN1 |
| CAN bus | CAN1 (TX 22 / RX 23) is ALREADY IN USE | Verify PJRC pin mapping before assigning another controller |
| PWM output | Most pins support FlexPWM; 2, 3, 4, 33 are clean | See PJRC PWM table |

Full reference: [Teensy 4.1 pinout card](https://www.pjrc.com/store/teensy41.html).
