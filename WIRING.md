# racecar-35 — Wiring Reference

Complete pin-by-pin wiring for both MCUs. Use this when rebuilding on a new
board. The architecture lives in [CLAUDE.md](CLAUDE.md) — this file is just
the connections.

```
Cabin (driver)                         Trunk (data + connectivity)
+-------------------+                 +-----------------------------+
|  CrowPanel ESP32  | <==UART (3 wires) ==>  Teensy 4.1            |
|  (cabin display)  |                 |  GPS (NEO-M9N)              |
|                   |                 |  Tach input (opto)          |
|                   |                 |  IMU (MPU-6050)             |
|                   |                 |  W5500 ethernet             |
|                   |                 |  SD card (built-in slot)    |
+-------------------+                 +-----------------------------+
```

---

## 1. Teensy 4.1 — full pin assignments

| Teensy 4.1 pin | Function                          | Notes |
|---------------:|-----------------------------------|-------|
| **3.3V**       | Power out — to GPS, IMU, W5500, tach pull-up | Internal regulator, ~250 mA budget |
| **GND**        | Common ground for everything      | Multiple GND pins on the header — any will do |
| **VIN / 5V**   | Power in (5 V, USB or ext)        | Or use USB-C |
| **5V**         | Output — feed downstream 5 V devices if needed | Same rail as VIN |
| Pin 5          | W5500 `/INT`                      | Open-drain interrupt from W5500 |
| Pin 6          | W5500 `/RST`                      | Active-low reset to W5500 |
| Pin 7          | Serial2 RX2 — **GPS TX**          | u-blox NEO-M9N TX → here |
| Pin 8          | Serial2 TX2 — **GPS RX**          | Optional (config); often unused |
| Pin 9          | **Tach input** (FreqMeasure)      | Only pin FreqMeasure works on for T4.x |
| Pin 10         | W5500 `CS` / `SCS`                | SPI chip-select (CS_PIN) |
| Pin 11         | W5500 `MOSI`                      | SPI0 MOSI |
| Pin 12         | W5500 `MISO`                      | SPI0 MISO |
| Pin 13         | W5500 `SCK` / `SCLK`              | SPI0 SCK — *also* the on-board LED |
| Pin 14         | Serial3 TX3 — to CrowPanel `RX`   | Dash telemetry out |
| Pin 15         | Serial3 RX3 — from CrowPanel `TX` | Dash commands in (REC, TRACK, TZ, SDFORMAT) |
| Pin 16 / A2    | **Oil pressure ADC**              | 0.5–4.5 V transducer via 10 kΩ / 20 kΩ divider |
| Pin 17 / A3    | **Coolant temp ADC**              | NTC thermistor with 150 Ω pullup to 3.3 V |
| Pin 18         | Wire SDA — **IMU SDA**            | I²C data |
| Pin 19         | Wire SCL — **IMU SCL**            | I²C clock |
| (built-in SDIO)| **SD card slot**                  | Dedicated socket on the board, no header pins |

> **Warning — pin 13 LED:** since pin 13 is also `SCK`, the on-board LED won't
> blink as a heartbeat once the W5500 is wired. The Teensy is still alive —
> watch the USB serial output instead. (If you really want a blink LED, wire
> an external one to any free pin.)

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

| Teensy 4.1     | Direction | CrowPanel ESP32-S3 |
|---------------:|:---------:|:-------------------|
| Pin 14 (TX3)   | →         | GPIO 44 (RX0)      |
| Pin 15 (RX3)   | ←         | GPIO 43 (TX0)      |
| GND            | —         | GND                |

Baud: 115200 8N1, line-oriented with `\n` terminators. Wire format is
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

Bauds tried at boot: **38400** (SparkFun RTK default), then **9600** (bare
module factory default). If neither handshakes the lib goes "raw bytes" mode
and just observes incoming data on Serial2.

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

## 7. W5500 Ethernet module

8 wires. Note that **pin 13 doubles as SCK and the Teensy on-board LED** —
once SPI is active, the LED won't blink as a heartbeat anymore.

| W5500 module pin* | Teensy 4.1 pin | Notes |
|------------------:|:---------------|-------|
| `VCC` (3V3)       | **3.3V**       | WIZnet modules need 3.3 V; some Chinese clones have a 5 V regulator and accept 5 V |
| `GND`             | **GND**        | |
| `SCS` / `CS` / `NSS` | **Pin 10**  | SPI chip-select |
| `MOSI` / `SI`     | **Pin 11**     | SPI MOSI |
| `MISO` / `SO`     | **Pin 12**     | SPI MISO |
| `SCK` / `SCLK`    | **Pin 13**     | SPI clock |
| `INT` / `IRQ`     | **Pin 5**      | Open-drain interrupt; pulled up by Teensy internally |
| `RST` / `RESET`   | **Pin 6**      | Active-low reset; held low briefly at boot |

\* Different W5500 modules use different silkscreen labels — common
aliases shown. Consult your specific module's pinout if unsure.

### Diagnostics at boot

The Teensy's USB-serial output prints diagnostics:

```
[eth] miso pin test: CSlo:U=0,D=0  CShi:U=1,D=0     ← chip is driving MISO ✓
[eth] raw VERSIONR=0x04 (3 reads: 0x04,0x04,0x04)   ← W5500 signature, stable bus ✓
[eth] DHCP... OK  IP: 192.168.1.42  (chip=W5500, link=UP)
[ntp] querying 0.pool.ntp.org ...
[ntp] OK  unix=1746715847
```

If anything fails, see the `[eth-dbg]` line printed every 3 s for live state.

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
| Teensy `3.3V` | GPS, IMU, W5500, tach pull-up                     |
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
- [ ] W5500: 8 wires (VCC, GND, CS→T10, MOSI→T11, MISO→T12, SCK→T13, INT→T5, RST→T6)
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
            Serial1 RX     0 -|                     |- GND
            Serial1 TX     1 -|                     |- 3.3V
                           2 -|                     |- 23   A9
                           3 -|                     |- 22   A8
                           4 -|                     |- 21   A7 / RX5
 [W5500 /INT]              5 -|                     |- 20   A6 / TX5
 [W5500 /RST]              6 -|                     |- 19   A5 / SCL    [IMU SCL]
       [GPS] RX2           7 -|                     |- 18   A4 / SDA    [IMU SDA]
       [GPS] TX2           8 -|     T E E N S Y     |- 17   A3 / TX4    [Coolant temp ADC]
     [Tach in]             9 -|        4 . 1        |- 16   A2 / RX4    [Oil PSI ADC]
   [W5500 CS]             10 -|                     |- 15   A1 / RX3    [Dash RX  <- CrowPanel TX]
 [W5500 MOSI]             11 -|                     |- 14   A0 / TX3    [Dash TX  -> CrowPanel RX]
 [W5500 MISO]             12 -|                     |- 13   SCK / LED   (SPI0 SCK; on-board LED)
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
| **In use** | 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, SDIO | See the project annotations in the diagram above |
| **Free** | 0, 1, 2, 3, 4, 20, 21, 22, 23, 24-32, 33-41 | All available for future expansion |
| **Caveat** | Pins 16/17 used as analog (A2/A3) | Serial4 (RX4/TX4) is no longer available |
| **Caveat** | Pin 13 doubles as SPI0 SCK and the on-board LED | LED won't blink as a heartbeat once W5500 is wired |
| **FreqMeasure only** | Pin 9 | The *only* pin FreqMeasure works on for T4.x — don't reassign |

### Common alt-functions for currently-unused pins

If you need to add something, these are the natural pin choices:

| Function | Best free pin | Alt |
|----------|---------------|-----|
| Another UART | Serial6 (pins 24=TX, 25=RX) | Serial7 (28=RX, 29=TX) |
| Another I²C bus | Wire1 (pins 16=SDA1, 17=SCL1)... but those are in use as ADCs now | Wire2 (24=SCL2, 25=SDA2) |
| Another SPI bus | SPI1 (pins 26=MOSI1, 27=SCK1, 39=MISO1) | — |
| More ADC inputs | A6 (pin 20), A7 (21), A8 (22), A9 (23) | A14–A17 (pins 38–41) |
| CAN bus | CAN1 (pin 22=TX1, 23=RX1), CAN2 (pin 0=RX2, 1=TX2), CAN3 (pin 30=TX3, 31=RX3) | T4.1 has FlexCAN on multiple pins |
| PWM output | Most pins support FlexPWM; 2, 3, 4, 33 are clean | See PJRC PWM table |

Full reference: [Teensy 4.1 pinout card](https://www.pjrc.com/store/teensy41.html).
