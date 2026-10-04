# Solderable breadboard — complete wire-by-wire pinout

**This is the hand-soldered protoboard build (Teensy 4.1 + discrete parts), NOT the
Rev C/Rev D PCBway board and NOT the Rev A carrier.** Everything the car connects to
lands on screw terminals along the **left edge**; the Teensy sits on the **right**,
USB-C facing out. Picture: [BREADBOARD.svg](BREADBOARD.svg). Build order/parts:
[README.md](README.md).

Firmware target: **v0.1.150** (`FIRMWARE_VERSION` in [src/main.cpp](../../src/main.cpp)).

```
         12 V battery
              |
      [fuse]  |  5 V buck  3 A+        (Pi 5 has its OWN 5 V 5 A supply)
              v
   J1 5V ──┬── Teensy VIN
           ├── J2 5V   ──> CrowPanel J10 +5V_IN
           └── J5 5V   ──> oil transducer

   Teensy 3.3V ──┬── GPS VCC
                 ├── IMU VCC
                 ├── 4.7k tach pull-up
                 ├── 150R coolant pull-up
                 └── SN65HVD230 VCC (optional)

   GND RAIL (star) ── every GND below + Teensy GND + J1 GND
```

---

## 1. Teensy 4.1 pin map — every pin this build touches

`[bracketed]` = wire on this board. Verify against the silkscreen on *your* board
before soldering; pin numbers below are the GPIO numbers used in code.

```
                              +-------[USB-C]-------+      <- USB outboard (flash here)
                         GND -|                     |- 5V (VIN)   [<- J1 5V]
        [J3 RX  <- Pi TX]  0 -|                     |- GND         [-> GND rail]
        [J3 TX  -> Pi RX]  1 -|                     |- 3.3V        [-> 3.3V rail]
                           2 -|                     |- 23  A9      [CAN1 RX  (opt)]
                           3 -|                     |- 22  A8      [CAN1 TX  (opt)]
                           4 -|                     |- 21  A7
                           5 -|  [RESERVED NET]     |- 20  A6      [J7 AEM (divided)]
                           6 -|  [RESERVED NET]     |- 19  SCL     [IMU SCL]
        [GPS TX -> RX2]     7 -|                     |- 18  SDA     [IMU SDA]
        [GPS RX <- TX2]     8 -|     T E E N S Y     |- 17  A3      [J6 coolant]
        [J4 TACH (opto out)]9 -|        4 . 1        |- 16  A2      [J5 oil]
                          10 -|  [RESERVED NET]     |- 15  RX3     [J2 RX <- screen TX]
                          11 -|  [RESERVED NET]     |- 14  TX3     [J2 TX -> screen RX]
                          12 -|  [RESERVED NET]     |- 13  LED     [heartbeat - LEAVE ALONE]
                        3.3V -|                     |- GND         [-> GND rail]
                          24 -|                     |- 41
                          25 -|                     |- 40
                          26 -|                     |- 39
                          27 -|                     |- 38
                          28 -|                     |- 37
                          29 -|                     |- 36
                          30 -|                     |- 35
                          31 -|                     |- 34
                          32 -|                     |- 33
                              |   +-------------+   |
                              |   |  SD (SDIO,  |   |
                              |   |   built-in) |   |
                              +---+  no wiring  +---+
```

| Pin | Net on this board | Don't |
| --- | --- | --- |
| 0 / 1 | Serial1 → Pi 5 video box (RX / TX), 115200, **3.3 V only** | never 5 V |
| 5, 6, 10, 11, 12, 13 | **Reserved** for the unimplemented trunk WiFi/SPI module | don't reuse, don't remove the LED heartbeat yet |
| 7 / 8 | Serial2 ← GPS TX / → GPS RX, raised to 230400 at boot | don't skip pin 8 — firmware configures the module over it |
| 9 | Tach, `FreqMeasureMulti` (opto output only) | plain `FreqMeasure` uses pin 22 — wrong here |
| 14 / 15 | Serial3 → screen RX / ← screen TX, **921600** | unplug before USB-flashing the screen |
| 16 (A2) | Oil pressure ADC | Serial4 lost |
| 17 (A3) | Coolant NTC ADC | Serial4 lost |
| 18 / 19 | Wire SDA / SCL → MPU-6050 (0x68) | 3.3 V only, keep wires short |
| 20 (A6) | AEM 30-0300 gauge output through the 20k/20k divider | **never** AEM WHITE directly |
| 22 / 23 | CAN1 TX / RX → SN65HVD230 (only for MegaSquirt mode) | never an MCP2551 (5 V — it will damage the Teensy) |
| 3.3V | GPS, IMU, tach pull-up, coolant pull-up, CAN transceiver | don't hang the screen or a 5 V load off it |
| 5V (VIN) | J1 5 V in | cut **VUSB–VIN** (pad on the Teensy underside) if USB and J1 are both live |

Alt-function notes for anything you add later: Serial6 = 24 TX / 25 RX, Serial7 = 28 RX /
29 TX, SPI1 = 26 MOSI / 27 SCK / 39 MISO, extra ADC = A7 (pin 21).

---

## 2. J1 POWER — do this first

| From | To | Note |
| --- | --- | --- |
| 12 V battery (+), fused 5 A | buck IN+ | buck must be **5 V out, 3 A or better** |
| buck OUT+ | **J1 pin `5V`** | ~5.0 V measured before connecting the Teensy |
| buck OUT− | **J1 pin `GND`** | also the buck's reference for OUT |
| battery (−) | buck IN− | ground the buck to the car at one point only |

Then, with J1 still **off**:

| From | To | Note |
| --- | --- | --- |
| J1 `5V` | Teensy **5V (VIN)** | top-right header pin |
| J1 `5V` | J2 `5V` | feeds the CrowPanel J10 +5V_IN |
| J1 `5V` | J5 `5V` | oil transducer supply |
| J1 `GND` | **GND rail** | fat jumper — this is the star point |
| Teensy GND (both header GNDs) | GND rail | |

- **Never 12 V on J1. Never 5 V on the Teensy 3.3V pin.**
- Powering from USB on the bench with J1 also live requires the **VUSB–VIN cut pad**
  on the Teensy's underside, or the two 5 V sources fight.
- The Teensy 3.3V rail feeds GPS + IMU + pull-ups only. Don't add servos/displays.
- The Raspberry Pi 5 gets its **own** 5 V 5 A USB-C supply; J3 is a 3-wire 3.3 V UART,
  no power.

---

## 3. J4 TACH — PC817 optocoupler you build by hand

**Diagram: [OPTO_WIRING.svg](OPTO_WIRING.svg)** (open in a browser) — physical pinout,
full schematic, bench check, and the weak-tach-line variant.

### 3.0 What "by hand" means here — the LED is INSIDE the chip

The PC817 is a **bare DIP-4 chip**, not a module. It contains everything: an infra-red
**LED across pins 1 (+) and 2 (−)** and a **phototransistor across pins 4 (collector) and
3 (emitter)**. You add **no LED** and there is nothing to configure or range-jumper.

- A plain silicon diode (1N4148 / 1N4007 / any rectifier) **emits no light** and can never
  be the input of an optocoupler. The only diode you add here is **D1**, the *optional*
  reverse clamp across pins 1–2.
- If you literally have no PC817 and want to build the isolation from loose parts you need
  an **IR LED (≈940 nm)** facing a **phototransistor or photodiode**, black-heatshrunk face
  to face. A PC817 is cheaper, matched, and far more reliable than that.

**Source limit: ECU / instrument-cluster conditioned tach ONLY.** Never coil negative,
ignition lead, spark, or injector drive. Read
[TACH_INTERFACE.md](../teensy-integrated-revc/TACH_INTERFACE.md) before wiring it to a car.

### 3a. Pinout — DIP-4, TOP view, notch up (pin 1 is left of the notch)

```
        ┌───── notch ─────┐
        │   ┌─────┐       │
   ─────┤1  │     │      4├─────       1 = LED anode     (+)    ← R1 1 kΩ ← J4 SIG
   pin1 │   │     │       │  pin4      2 = LED cathode   (−)    → J4 GND / board GND
        │   │     │       │            4 = collector            → 3.3 V via R2 + Teensy pin 9
   ─────┤2  │     │      3├─────       3 = emitter              → board GND
   pin2 │   └─────┘       │  pin3
        └─────────────────┘
```

The dot or dimple on the package also marks pin 1. If you have an SMD/optically-similar
part, check its datasheet before assuming this order.

### 3b. The circuit (simple version — what the SVG and README show)

```
                    3.3 V
                      │
                    [4.7 kΩ]  R2
                      │
 PC817                ├───────────────► Teensy pin 9
 ┌───────────┐        │                   │
 │ 1 (A) ──►|  (LED)  4 (C) ──────────────┘
 │                     │                [100 nF]  (optional, at pin 9)
 │ 2 (K)         3 (E) │                   │
 └──┬──────────┬───────┘                  GND
    │          │
    │        GND (board star)
    │
 J4 SIG ──[1 kΩ]──┬── pin 1            R1 = 1 kΩ ¼ W → ≈11 mA LED current at 12 V
                  │                    R2 = 4.7 kΩ (10 kΩ also fine) → pull-up, REQUIRED
               [D1 1N4148]  optional   D1 = cathode to pin 1, anode to pin 2 (reverse clamp)
 J4 GND ──────────┴── pin 2
```

| Part | Value | Why |
| --- | --- | --- |
| `R1` J4 SIG → pin 1 | **1 kΩ ¼ W** | 12 V pulse minus the LED drop ⇒ ~11 mA through the internal LED |
| `R2` 3.3 V → pin 9 | **4.7 kΩ** (10 kΩ fine) | the phototransistor is a bare transistor — the pull-up IS the signal |
| `C1` pin 9 → GND | 100 nF | optional, kills tach hash |
| `D1` across pins 1–2 | 1N4148, anti-parallel | optional; the PC817's reverse rating is only ~6 V |

Output is **inverted** (pin 9 pulled LOW while the tach pulses). Firmware doesn't care —
`FreqMeasureMulti` counts edges either way.

### 3c. ⚠️ If the tach line is a weak ECU/cluster/open-collector output

The simple version draws **~11 mA out of the car's tach wire at 12 V**. A healthy ECU
tach driver or instrument cluster usually tolerates that, but a factory
cluster output with a weak internal pull-up may not — and that's exactly the failure
mode the Rev C interface document forbids. If in doubt, don't load the line: add a
**2N3904 pre-driver** so the opto LED current comes from your board's 5 V instead.

```
 J4 SIG ── 100 kΩ ──┬── base  2N3904
                    │
                  10 kΩ  (to board GND)              collector ── 1 kΩ ── PC817 pin 1
                    │                                emitter   ── board GND
 J4 GND ────────────┴─ 2N3904 emitter/GND            5 V ───────────── PC817 pin 2 (return)
```

Input impedance is now ~110 kΩ, ~0.5 mA out of the car's line. The opto
output half is unchanged (4.7 kΩ to 3.3 V → pin 9). Either version gives the same
firmware behaviour; the pre-driver version is the one that matches the Rev C
electrical contract.

### 3d. Bench test before it goes near the car

1. Power off, ohm meter: `J4 SIG` → `J4 GND` should **not** read 0 Ω (that would be a short).
2. Power on, no pulses: pin 9 must sit at **~3.3 V** (pull-up high, opto off).
3. Feed `J4 SIG` with **12 V pulses** (bench supply through the 1 kΩ, or the car's signal)
   at a few hundred Hz: pin 9 must swing to **below 0.8 V** on each pulse.
   A pin that only dips to ~1.5 V is the classic "reads constant HIGH, RPM = 0" fault —
   check the LED current and the ground, not the firmware.
4. 8000 RPM at 2 pulses/rev = **267 Hz**. PC817 is fine to several kHz.
5. `RPM_PULSES_PER_REV` defaults to **2.0** in `src/main.cpp`; if idle reads 2× or ½×,
   change it (Settings → tach pulses/rev, NVS `rpmppr`, sent as `CFG,rpmppr,<x10>`).

---

## 4. J5 OIL — 10 kΩ / 20 kΩ divider → pin 16 (A2)

5 V output transducer, Teensy ADC is **not** 5 V tolerant. The divider is mandatory.

```
 J5 5V  ── Teensy 5V / J1 5V
 J5 GND ── board GND
 J5 SIG ── 10 kΩ ──●── Teensy pin 16 (A2)
                   │
                  20 kΩ
                   │
                  GND            (+ optional 10 nF from A2 to GND, right at the pin)
```

Firmware contract: `V_adc = V_sensor × 2/3`, `OIL_V_AT_ZERO_PSI = 0.5 V`,
`OIL_PSI_FULL_SCALE = 150 PSI`.

| Input at J5 SIG | Expect on A2 |
| --- | --- |
| 0.5 V (0 PSI at atmosphere) | ~0.33 V |
| 4.5 V (150 PSI) | ~3.00 V |

Meter both ends before trusting a reading. Note the Teensy has a ~50 kΩ **internal
pull-down** enabled on A2 by firmware (`INPUT_PULLDOWN`) which sits in parallel with the
20 kΩ leg — readings can come out roughly 10 % low; that's expected, do not "fix" it by
changing the resistors (it would break the firmware ratio). Wire colour check: a genuine
0.5–4.5 V sender reads **~0.5 V at atmosphere**; a 0–5 V variant reads ~0 V and needs a
firmware constant change instead.

---

## 5. J6 NTC — 150 Ω pull-up to 3.3 V → pin 17 (A3)

```
 3.3 V ── 150 Ω ──●── Teensy pin 17 (A3)
 J6 SIG ───────────┘
 J6 GND ────────────── board GND     <- dedicated wire, NOT the engine block
```

- Sender: VDO 1600–22 Ω curve (firmware is fitted to 100 F = 700 Ω, 180 F = 110 Ω,
  250 F = 22 Ω via Steinhart-Hart `COOLANT_SH_A/B/C`).
- A **different** sender needs a different pull-up (GM-style ~3.3 kΩ @ 100 F → ~2.2 kΩ)
  and new Steinhart-Hart coefficients.
- Single-terminal senders **must** get a dedicated return wire — grounding through the
  threads injects ignition noise straight into the ADC.
- Firmware also enables an internal ~50 kΩ pull-down on A3, so a disconnected sender
  reads ~0 V and reports a fault instead of a plausible temperature.

---

## 6. J7 AEM 30-0300 gauge output — 0.500 gain → pin 20 (A6)

The gauge's WHITE analogue line can be **5 V**; A6 dies at 3.6 V. This divider is not
optional, and its ratio is a firmware constant (`AFR = 2.3750·V + 7.3125`).

```
 J7 WHT ── 20 kΩ 0.1% ──●── 20 kΩ 0.1% ── J7 BRN ── board GND
                        │
                       1 kΩ
                        │
                    Teensy pin 20 (A6) ── 100 nF ── GND
```

- Use **both** AEM wires: the Power/IO harness's solid **WHITE = pin 9** (analogue +)
  and solid **BROWN = pin 10** (reference). BROWN is the divider's bottom — not a
  random chassis point.
- The gauge keeps its own 12 V supply and fuse; we take analogue only. No onboard
  heater/controller, no direct-sensor connector.
- Bench check: **5.00 V WHITE-to-BROWN must read ~2.50 V on pin 20.**
- Settings → **AEM 30-0300 AFR input** stays **OFF** until that measurement checks out.
- Valid range 0.50–4.50 V ⇒ 8.50–18.00 AFR. This is the gasoline-equivalent
  (14.65) AFR the firmware reports.
- The hand-built version has only the divider — it does **not** have the Rev C board's
  LM4040 shunt clamps, Schottky clamps or TMUX1511 powered-off isolation. Treat the
  gauge wiring and grounds with that in mind; see
  [AFR_INTERFACE.md](../teensy-integrated-revc/AFR_INTERFACE.md).

---

## 7. J2 SCREEN — CrowPanel UART, 921600

| From | To | Note |
| --- | --- | --- |
| J2 `GND` | CrowPanel **J10 GND** | shared ground is mandatory |
| J2 `5V` | CrowPanel **J10 +5V_IN** | or power the screen from its own USB-C |
| J2 `TX` (Teensy **pin 14**) | CrowPanel **J10 RXD0_H** | crossed |
| J2 `RX` (Teensy **pin 15**) | CrowPanel **J10 TXD0_H** | crossed |

- **Use J10 only — the larger XH2.54 4-pin header.** Its `+5V_IN` is the only
  power-capable pin; TX/RX sit behind level shifters.
- **NEVER the small HY2.0 header (J2 on the screen).** Its `3V3_OUT` is an *output*
  from the ESP32's 3.3 V rail — 5 V there kills the chip.
- **Disconnect J2 TX/RX before USB-flashing the CrowPanel** (UART0 is shared with the
  CH340) or the flash silently corrupts.
- Baud **921600 8N1**, line-oriented `\n`.

---

## 8. J3 VIDEO — Raspberry Pi 5, 115200, 3.3 V (v0.1.150)

| From | To | Note |
| --- | --- | --- |
| J3 `GND` | Pi header **pin 6** (GND) | |
| J3 `TX` → 1 kΩ → Teensy **pin 1** | Pi header **pin 10** (GPIO15 RXD) | crossed |
| J3 `RX` ← 1 kΩ ← Teensy **pin 0** | Pi header **pin 8** (GPIO14 TXD) | crossed |

- **3.3 V logic both ends. Never put 5 V on J3 or on a Pi GPIO.**
- Pi side: `dtparam=uart0=on`, `usb_max_current_enable=1`.
- Cameras and the USB stick live on the Pi, not on this board.
- Settings → **Video interconnect** only after this cable is verified. With it OFF the
  Teensy ignores Serial1, and a missing Pi does not block START.
- Firmware forwards `REC` / `TRACK` / `HUD` and relays `VID,...` status to the dash.

Two links, two voltages, two bauds — do not mix them up.

---

## 9. GPS — u-blox NEO-M9N (Serial2)

| Module pin | Teensy | Note |
| --- | --- | --- |
| `VCC` / `3V3` | **3.3V** | SparkFun RTK boards tolerate 3.3–5 V in; feed 3.3 V |
| `GND` | **GND rail** | star, not through the engine block |
| `TX` | **pin 7** (RX2) | module → Teensy |
| `RX` | **pin 8** (TX2) | **required** — boot config + baud change go over it |

- Boot scan: `230400 → 38400 → 9600` (SparkFun default 38400, bare module 9600), then
  the firmware raises the link to **230400** and calls `saveConfiguration()` so a module
  brownout reboots straight back into UBX auto-PVT.
- Settings → **GPS baud** can change it live; the firmware rescans if the pick fails.
- External active antenna on the SMA; the module body needs a clear sky view.
- Keep GPS wires short and away from the tach/oil harness — this module's resets are a
  known symptom of power/ground noise.

---

## 10. IMU — MPU-6050 on a GY-521 breakout (Wire)

| GY-521 pin | Teensy | Note |
| --- | --- | --- |
| `VCC` | **3.3V** | **not 5 V** — the module's pull-ups reference VCC |
| `GND` | **GND rail** | |
| `SDA` | **pin 18** | Wire (I2C0) |
| `SCL` | **pin 19** | Wire (I2C0) |
| `AD0` | **GND** | floating/high ⇒ address 0x69 ⇒ "MPU-6050 NOT found" |
| `XDA`, `XCL`, `INT` | leave open | not used |

- Address **0x68**, 400 kHz, raw 14-byte burst at 0x3B, ±2 g / ±250 °/s, DLPF 44 Hz.
- 3.3 V only; I2C0 (18/19) — not 16/17 (`Wire1`, those are the ADCs), not 24/25.
- Detection is **one-shot at boot**. If it isn't ACKed then, every `IMU` line is
  `0.00,...,0.0` for the whole session — that's "not detected", not a calibration
  artifact. Re-seat/solder the header before suspecting code.
- Boot auto-cal (`calibrateIMU()`) removes gyro bias and normalizes accel to 1.00 g,
  gated by a stillness check, and caches last-good values in Teensy EEPROM (addr 0).
  Boot the car **stationary** or the cal is rejected (the previous good cal stands).
- Mounting: board **flat, component side up, header pins toward the rear** ⇒
  +X forward, +Y right, +Z up.

---

## 11. CAN — MS3Pro via SN65HVD230 (optional, only for `sensor_type = 1`)

| Transceiver pin | Goes to |
| --- | --- |
| `3V3` / `VCC` | Teensy **3.3V** |
| `GND` | **GND rail** |
| `CTX` / `TXD` | Teensy **pin 22** (CAN1 TX) |
| `CRX` / `RXD` | Teensy **pin 23** (CAN1 RX) |
| `CANH` | MS3Pro CAN-H |
| `CANL` | MS3Pro CAN-L |

- Must be an **SN65HVD230 / VP230 (3.3 V)**. An MCP2551 is 5 V and will damage the Teensy.
- The blue breakout usually has the 120 Ω terminator onboard (`R2 = 121`); it must sit at
  the **end** of the bus (Teensy ↔ MS3Pro = correct).
- TunerStudio: `CAN-Bus / Testmodes → Dash Broadcasting → Enable`, Automatic mode,
  500 kbps, base ID 1512 (0x5E8).
- Not required if you only use Direct sensors (opto tach + ADCs). RPM still comes off the
  tach in Direct mode either way — both sources always run.

---

## 12. SD card

Nothing to wire — the Teensy 4.1's built-in **SDIO socket** on the back of the board.
FAT32 micro-SD. Settings → Format SD card if the card is blank. Leave the socket clear of
the protoboard so the card can be removed.

---

## 13. Passive netlist — what to actually solder

| Qty | Part | End A | End B |
| --- | --- | --- | --- |
| 1 | 1 kΩ | J4 `SIG` / pre-driver collector | PC817 pin 1 (anode) |
| 1 | 4.7 kΩ | 3.3 V rail | PC817 pin 4 **and** Teensy pin 9 |
| 1 | 100 nF | Teensy pin 9 | GND |
| 1 | 1N4148 (opt) | PC817 pin 2 (cathode, K side) | PC817 pin 1 (anode, A side) |
| 1 | 10 kΩ | J5 `SIG` | Teensy pin 16 (A2) |
| 1 | 20 kΩ | Teensy pin 16 (A2) | GND |
| 1 | 10 nF (opt) | Teensy pin 16 (A2) | GND |
| 1 | 150 Ω | 3.3 V rail | J6 `SIG` **and** Teensy pin 17 (A3) |
| 1 | 20 kΩ 0.1 % | J7 `WHT` | divider midpoint |
| 1 | 20 kΩ 0.1 % | divider midpoint | J7 `BRN` / GND |
| 1 | 1 kΩ | divider midpoint | Teensy pin 20 (A6) |
| 1 | 100 nF | Teensy pin 20 (A6) | GND |
| 2 | 1 kΩ | Teensy pin 1 / pin 0 | J3 `TX` / J3 `RX` (series, in the cable) |
| 1 each | 100 nF ×3, 10 µF (opt) | 5 V rail / 3.3 V rail | GND (decoupling, near the Teensy) |
| — | PC817 (DIP-4) | see §3 | |
| — | 2N3904 + 100 kΩ + 10 kΩ | only for the §3b high-impedance version | |

Bare-wire jumpers: J1 `5V` → Teensy 5V; J1 5V → J2 5V; J1 5V → J5 5V;
J1 `GND` → GND rail → Teensy GND ×2 + every terminal GND below.

---

## 14. Continuity / meter checks before first power-up

With the 5 V buck **off** and the Teensy **out of its socket** if possible:

- [ ] `5V` rail to `GND` rail is **not** a short (expect >1 kΩ, or your DMM's diode reading).
- [ ] `3.3V` rail to `GND` rail is **not** a short.
- [ ] J4 `SIG` to J4 `GND` is not a short.
- [ ] J6 `SIG` to 3.3 V = **150 Ω**; J6 `SIG` to Teensy pin 17 = **0 Ω**.
- [ ] J5 `SIG` to Teensy pin 16 = **10 kΩ**; pin 16 to GND = **20 kΩ**.
- [ ] J7 `WHT` to J7 `BRN` = **40 kΩ** (both 0.1 % legs); midpoint to pin 20 = **1 kΩ**.
- [ ] J2 `TX`/`RX` land on Teensy **14**/**15** (not swapped, not 43/44).
- [ ] J3 `TX`/`RX` land on Teensy **1**/**0**, with the 1 kΩ in series.

Then power up and check in order: 5.0 V on J1, 3.3 V on the Teensy rail, GPS fix,
`MPU-6050 ready` on the USB serial banner, `FreqMeasureMulti on pin 9: armed`,
and RPM/Coolant/Oil responding before turning on AEM or the video link.

---

## 15. Settings to enable, and only when the wire exists

| Setting | Enable when |
| --- | --- |
| Sensor data source | Direct (opto tach + ADCs) unless you wired CAN or BLE |
| Tach pulses/rev | after comparing tach RPM against a trusted reference |
| AEM 30-0300 AFR input | J7 soldered and **pin 20 meters 2.50 V at 5.00 V in** |
| Video interconnect | J3 wired and the Pi UART answers |
| Debug logging (SD) | only when chasing a problem (default OFF) |
| RPM spike filter / GPS drift filter | default Normal — leave alone until the car runs |

---

## 16. Not on this board (don't go looking for them)

These exist only on **Rev D** (`hardware/teensy-integrated-revd/`) and have **no
firmware**: the ESP32-S3 trunk WiFi coprocessor over SPI0 (pins 10/11/12/13 + 5/6),
the CR2032 VBAT cell, the ICM-42670-P IMU (this build uses the MPU-6050), the
THROTTLE/BRAKE analogue inputs, and the Rev D oil/coolant gain changes. Pin 13 stays the
heartbeat LED here. The screen's own WiFi remains the only network path, and the OTA
path is unchanged.
