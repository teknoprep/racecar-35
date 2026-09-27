# Flux.ai prompt — racecar-35 trunk data-logger (Rev C + video UART)

Copy everything below the line into Flux. This is a **self-contained design
brief**. Flux has no access to the repo. Do not invent extra connectors, do not
put the Raspberry Pi or the AEM gauge on this PCB, and do not change Teensy pin
numbers — firmware already exists.

---

You are an expert automotive-adjacent PCB designer. Prototype a **first-build
engineering PCB** for a **trunk-mounted race-car data logger**. Produce a
complete, manufacturable package, not a napkin sketch.

## Deliverables (required)

1. Hierarchical schematic with named nets.
2. 2-layer PCB, 130 × 140 mm, 1.6 mm FR-4, **2 oz / 70 µm copper both sides**,
   ENIG, green mask, white silkscreen.
3. Full **BOM with manufacturer + MPN + footprint + qty** (JLCPCB / Digi-Key /
   Mouser orderable parts where possible; modules called out as hand-solder).
4. **Gerbers** (all copper, both masks, both silkscreens, paste, Edge.Cuts).
5. Separate **PTH and NPTH Excellon drills** + drill map.
6. **CPL / pick-and-place** (refdes, mid-X, mid-Y, rotation, side) in mm,
   origin = board lower-left.
7. Assembly drawing + silkscreen that is actually readable at the terminals.
8. Netlist / pin report proving every Teensy pin below is wired exactly as specified.

Do **not** claim ISO 7637 / load-dump qualification. This is an unbuilt
prototype. Prefer conservative protection over cleverness.

---

## What this board is

Two-MCU race dash. **This PCB is the TRUNK logger only.**

```
Cabin (NOT on this board)                    Trunk (THIS board)
CrowPanel Advance 5"/7"  <== 5 V UART ==>    Socketed Teensy 4.1
  dash UI, WiFi OTA, BLE                     GPS, IMU, SD, sensors
                                             ESP32-S3 WiFi coprocessor
Pi 5 video box (NOT on this board) <== 3.3 V UART ==> Teensy Serial1
  own 12 V → 5 V 5 A USB-C
  cameras + USB stick live on the Pi
```

Firmware already exists. **Teensy pin numbers are locked.** If a part will not
fit a locked pin, stop and flag it — do not reassign pins.

---

## Mechanical

- Outline **130 × 140 mm**. Four **3.2 mm NPTH M3** holes, one near each corner,
  4 mm inset, keep 2 mm copper clearance.
- **ALL field wiring is Phoenix MKDS 5.08 mm screw terminals on the LEFT edge
  only** (top view). Wire entry faces LEFT. Screwdriver from above. Function
  name above each block; pin 1…n numbered **top → bottom**.
- Teensy 4.1 **socketed** on the RIGHT, USB / SD / Program button facing out
  for service. Two 24-pin 2.54 mm female sockets (e.g. Samtec SSW-124-01-G-S).
- GPS **SMA female** (Amphenol 901-143 or equivalent horizontal edge SMA) on an
  outer edge that is **not** the terminal bank. SMA is GPS only — never WiFi.
- ESP32-S3-WROOM-1 **PCB antenna** at the **bottom** edge with a **48 × 6 mm
  board notch** under the antenna tip and Espressif’s **all-layer RF keepout**.
  No copper, vias, parts, battery, screws or cable bundles in the keepout.
  Enclosure will be **plastic / RF-transparent** around the antenna.
- CR2032 holder (Keystone 1058) serviceable from the top. Mark `RTC 3V CR2032`,
  `+`, `DO NOT CHARGE`.
- Mixed SMT + THT + modules. Hand-assembly of Teensy, Pololu, TRACO, terminals,
  SMA, coin-cell is expected.

LEFT-EDGE TERMINAL ORDER, top → bottom (do not reorder):

| Ref | Silkscreen | Pins top → bottom | Levels / notes |
|-----|------------|-------------------|----------------|
| J3  | TACH       | 1 SIGNAL, 2 RETURN | Conditioned ECU/cluster only. **NO COIL / IGNITION / INJECTOR.** |
| J4  | OIL        | 1 +5V OUT, 2 RETURN, 3 SIGNAL | 5 V 0.5–4.5 V transducer |
| J5  | NTC        | 1 NTC, 2 RETURN | Dedicated 2-wire coolant sender. Do not tee into an ECU sensor. |
| J6  | AEM        | 1 SIG+ (WHITE), 2 RETURN (BROWN) | AEM 30-0300 **gauge analogue only**. No O2 sensor, no heater. |
| J1  | POWER      | 1 +10–20V IN, 2 GND | Vehicle 12 V (design 10–20 V). Reverse-protected. |
| J2  | SCREEN     | 1 GND, 2 +5V OUT, 3 RX FROM SCREEN, 4 TX TO SCREEN | CrowPanel Advance **J10 only**. NEVER the screen’s HY2.0 3V3_OUT. |
| J_VID | VIDEO    | 1 GND, 2 TX (3.3 V), 3 RX (3.3 V) | Raspberry Pi 5 UART. **No 5 V on this connector.** |

Additional (NOT screw terminals; still need access):

| Ref | Function | Pins |
|-----|----------|------|
| J7  | CAN logic to external SN65HVD230 module | 1 3V3, 2 GND, 3 CTX (Teensy 22), 4 CRX (Teensy 23). **Not CANH/CANL.** |
| J8  | GPS SMA | RF + 3.3 V active-antenna bias |
| J9  | RTC lead | JST XH-2: 1 VBAT, 2 GND. Short removable lead to Teensy **VBAT and GND auxiliary pads**. Outer-row sockets do **not** carry VBAT. |
| J10 | NET service UART | 2×3 2.54 mm: 1 GND, 2 3V3 REF OUT (do not power from this), 3 TX from NET, 4 RX to NET, 5 BOOT0, 6 EN. 3.3 V only. |
| JP1 | MCU 5 V jumper | 1×2 2.54 mm. Open until bring-up passes. |

Silkscreen must say on TACH: `ECU/CLUSTER ONLY — NO COIL`.
Silkscreen must say on SCREEN: `J10 5V UART ONLY`.
Silkscreen must say on VIDEO: `3.3V UART — PI 5`.
Silkscreen must say on AEM: `GAUGE OUT ONLY`.

---

## Power tree (locked architecture)

Vehicle **12 V** (design **10–20 V**) at J1.

```
J1 +10-20V
   → bidirectional TVS SMBJ20CA to GND
   → 5 A input fuse (Littelfuse 0451005.MRL or equiv.)
   → reverse-polarity protection
   → VIN_PROTECTED (C1 100 µF/50 V bulk lives HERE, after RPP, not on raw input)
        ├─ Pololu D36V50F5 / 4091  → +5V_MAIN  (5 A TOTAL budget, not a proven rating)
        │     C2 470 µF/10 V at 5 V output
        │     ├─ 4 A fuse → J2 pin 2  (CrowPanel 5 V)
        │     ├─ 100 mA PTC → J4 pin 1  (oil-sensor 5 V)
        │     ├─ JP1 → Teensy VIN (after VUSB–VIN cut on the Teensy)
        │     └─ analogue 5 V bias (coolant excitation only, diode-isolated)
        ├─ TRACO TSR 1-2433 #1  (from VRP, NOT from 5 V) → +3V3_AUX
        │     GPS NEO-M9N + active-antenna bias
        └─ TRACO TSR 1-2433 #2  (from VRP, NOT from 5 V) → +3V3_NET
              ESP32-S3-WROOM-1-N8R2  (WiFi bursts ≥0.5 A; 1 A class rail)
```

Rules:

- Do **not** power GPS or WiFi from the Teensy’s 3.3 V pin (~250 mA).
- Do **not** feed a 3.3 V TSR from the 5 V rail.
- Do **not** tie +3V3_AUX, +3V3_NET and Teensy 3V3 together.
- Do **not** power the Raspberry Pi 5 from this board. Pi has its own 12 V→5 V 5 A USB-C.
- Do **not** power the AEM gauge or O2-sensor heater from this board.
- IMU and CAN-module logic **may** use Teensy 3V3.
- Provide test pads: GND, +5V_MAIN, +3V3_AUX, +3V3_NET, +3V3_MCU.
- Note on silkscreen / assembly: **cut Teensy VUSB–VIN bridge** before USB and
  car power are both present. JP1 is not a substitute.

Power budget to design for (peaks, not continuous proof):

| Load | Rail | Notes |
|------|------|--------|
| CrowPanel Advance RGB display | 5 V, fused 4 A | Worst-case cabin screen |
| Teensy 4.1 + SD | 5 V via JP1 | |
| Oil transducer | 5 V, PTC 100 mA | |
| GPS + active antenna | 3V3_AUX, antenna ≤100 mA via TPS2553 | |
| ESP32-S3 WiFi TX bursts | 3V3_NET, 1 A class | Local 10 µF + 100 nF at module |
| IMU / CAN logic / analog | MCU 3V3 / 5 V bias | Small |

---

## MCU — socketed Teensy 4.1 (LOCKED pin map)

Do not reassign. Unused pins stay unconnected (or test pads), never “helpfully”
wired.

| Teensy pin | Net / function |
|-----------:|----------------|
| 0  | VIDEO_RX  ← Pi TX, Serial1, 115200, 3.3 V |
| 1  | VIDEO_TX  → Pi RX, Serial1, 115200, 3.3 V |
| 5  | NET_READY ← ESP32 GPIO14, active-high |
| 6  | NET_RESET → open-collector to ESP EN (not direct cross-rail drive) |
| 7  | GPS_RX    ← NEO-M9N TX, Serial2 |
| 8  | GPS_TX    → NEO-M9N RX, Serial2 |
| 9  | TACH_3V3  FreqMeasureMulti input. Clean 0–3.3 V only. |
| 10 | NET_CS    → ESP GPIO10 |
| 11 | NET_MOSI  → ESP GPIO11 |
| 12 | NET_MISO  ← ESP GPIO13 |
| 13 | NET_SCK   → ESP GPIO12  (no LED heartbeat on this pin) |
| 14 | DASH_TX   → screen RX, Serial3, 921600 |
| 15 | DASH_RX   ← screen TX, Serial3, 921600 |
| 16 / A2 | OIL_ADC   after 0.500 front end + isolation |
| 17 / A3 | NTC_ADC   after 0.500 front end + isolation |
| 18 | IMU SDA, I2C0, 4.7 kΩ to MCU 3V3 |
| 19 | IMU SCL, I2C0, 4.7 kΩ to MCU 3V3 |
| 20 / A6 | AFR_ADC   after 0.500 front end + isolation. NEVER raw 5 V. |
| 22 | CAN_TX to J7 |
| 23 | CAN_RX from J7 |
| VIN | 5 V via JP1 (after VUSB–VIN cut) |
| 3V3 | MCU rail: IMU, CAN header, local pull-ups only |
| GND | star to power GND |
| VBAT | **only** via J9 lead to CR2032 +. No charger. No LIR2032. |

Built-in Teensy **SDIO slot** is the logger storage. Do not add a second SD host.
Do not add Ethernet / W5500. Ethernet is permanently removed.

---

## Analog front ends (required, not bare ADC pins)

All three analogue channels share the same protection idea: divider → shunt to
GND → negative Schottky clamp → 1 kΩ / 100 nF → **TMUX1511** powered-off
isolation → Teensy ADC. TMUX1511 off-isolation is only rated to **3.6 V**, so
the LM4040 **3.0 V** shunts must keep the switch inputs under 3.6 V even on
fault. Never dump clamp current into an unpowered MCU pin. ADC GPIOs = INPUT,
no internal pulls.

### Oil (J4 → pin 16 / A2)

- Sender: 5 V, 0.5–4.5 V = 0–150 PSI (AUTEX-style). Board supplies 5 V via PTC.
- Gain **0.500**: 20.0 kΩ / 20.0 kΩ **0.1%**. 4.5 V → 2.25 V at ADC.
- LM4040AIM3-3.0 shunt to GND, PMEG2010ER (or equiv.) negative clamp.
- 1 kΩ + 100 nF to RETURN, then TMUX1511 ch1.

### AEM AFR (J6 → pin 20 / A6)

- External **AEM X-Series 30-0300** analogue output only.
  Gauge Power/IO pin 9 **solid WHITE** = SIG+, pin 10 **BROWN** = RETURN.
- Gauge stays powered from the car (AEM 5 A fuse, 10–18 V). **No gauge/heater
  power on this PCB.**
- Same 0.500 / 20k/20k 0.1% / LM4040 3.0 / Schottky / 1k+100n / TMUX ch2.
- Open input must read low (not_ready), never a fake AFR.

### Coolant NTC (J5 → pin 17 / A3)

- Dedicated 2-wire NTC. Do not share an ECU sensor.
- Excitation: diode-isolated 5 V → 150 Ω bias into **LM4040 4.096 V** shunt.
- Sensor pull-up **2.49 kΩ 0.1%** from 4.096 V to NTC node.
- Sense that node with another 20k/20k 0.1% (0.500) into the same protected
  path (TMUX ch3). Compensate 40 kΩ parallel loading in firmware later.
- TMUX ch4 disabled.

---

## Tach (J3 → pin 9) — FULLY SPECIFIED, do not invent a reference

Conditioned ECU / instrument-cluster tach only (Mazda Miata 1990–2005 Spec
Miata initially). **Never** coil-negative, spark, injector, VR pickup.
**No vehicle-side pull-up. No trimpot. No DAC. No LM4040 as the comparator
reference.** The trigger is a fixed resistor divider from +5V_MAIN.

The car line must NOT drive the opto LED. High-Z sense → comparator → local
5 V transistor → opto LED. Shared ground: the opto is noise isolation and
level translation, **not** galvanic isolation of the whole logger.

### Nets

| Net | Meaning |
|-----|--------|
| TACH_IN | J3 pin 1 SIGNAL |
| GND | J3 pin 2 RETURN, board GND |
| TACH_SENSE | Divider mid / comparator IN+ |
| TACH_REF | Comparator IN−, **0.200 V** from 5 V |
| TACH_CMP | LM2903B OUT1, open-collector |
| TACH_BASE | NPN base |
| TACH_LED_A / TACH_LED_K | Opto LED |
| TACH_OPTO | Opto collector |
| TACH_MCU | Teensy pin 9 |

### 1) Input divider + clamps (high-Z, ~202 kΩ DC)

```
J3-1 TACH_IN ── R4 180 kΩ 1% ──● TACH_SENSE ── R5 22 kΩ 1% ── GND
                               ├─ C19 1 nF ── GND
                               ├─ D4 LM4040AIM3-2.5 cathode on TACH_SENSE, anode GND
                               └─ D5 PMEG2010ER  cathode on TACH_SENSE, anode GND
J3-2 ── GND
```

D4 clamps TACH_SENSE to 2.5 V (≈23 V at J3 before the shunt conducts).
D5 is the **negative** clamp (conducts if TACH_SENSE goes below GND).
At 12 V in, TACH_SENSE ≈ 1.31 V; at 20 V ≈ 2.18 V — under the 2.5 V shunt.
**No added pull-up on TACH_IN.**

### 2) Trigger reference — THIS IS THE ANSWER TO THE AUDIT

```
+5V_MAIN ── R6 240 kΩ 1% ──● TACH_REF ── R7 10 kΩ 1% ── GND
                           └─ C20 100 nF ── GND
```

**TACH_REF = 5.000 × 10 / (240+10) = 0.200 V**, tied to LM2903B pin 2 (IN1−).
Open-loop trip at J3 = 0.200 × (180+22)/22 = **1.836 V**.
Do not substitute a 1.65 V / 2.5 V / 3.3 V / Vcc/2 reference. 0.200 V at
IN− is what makes 3.3 V, 5 V **and** 12 V conditioned pulses all look HIGH
while rejecting ~1.5 V of noise / weak idle.

### 3) Comparator + hysteresis (LM2903B, VCC = +5V_MAIN)

SOIC-8 LM2903BIDR:

| Pin | Name | Net |
|----:|------|-----|
| 1 | OUT1 | TACH_CMP |
| 2 | IN1− | TACH_REF |
| 3 | IN1+ | TACH_SENSE |
| 4 | GND | GND |
| 5 | IN2+ | GND |
| 6 | IN2− | TACH_REF |
| 7 | OUT2 | NC |
| 8 | VCC | +5V_MAIN |

C21 100 nF from pin 8 to GND.

R9 4.7 kΩ from +5V_MAIN to TACH_CMP (open-collector pull-up).
R8 2.2 MΩ from TACH_CMP to TACH_SENSE (positive feedback on IN+).

Polarity: vehicle HIGH → TACH_SENSE > 0.200 V → OUT1 high-Z → TACH_CMP ≈ 5 V.
Vehicle LOW / open → TACH_CMP ≈ 0 V.

Calculated J3 thresholds with R8, +5V_MAIN = 5.0 V, V_OL ≈ 0 V:
- **rising (0→1) ≈ 1.85 V**
- **falling (1→0) ≈ 1.44 V**
- band ≈ 0.41 V at J3

(DESIGN_NOTES rounded this as 1.84 V rise / 1.56 V fall before tolerance.)

Truth table at J3 (after hysteresis):

| J3 SIGNAL | Result |
|-----------|--------|
| open / 0 V | idle, no pulses, RPM=0 |
| HIGH 3.3 V | HIGH (0.36 V at IN+) |
| HIGH 5 V | HIGH (0.55 V) |
| HIGH 12–14.4 V | HIGH (1.31–1.57 V) |
| HIGH 20 V | HIGH (2.18 V, still under 2.5 V clamp) |
| stuck 1.5 V | LOW — rejected |

Unused comparator: inputs biased as above, OUT2 no-connect. Do not float them.

### 4) Local LED driver (car line never supplies LED current)

```
TACH_CMP ── R10 10 kΩ ──● TACH_BASE ── Q1 MMBT3904 base
                        └─ R11 100 kΩ ── GND     emitter ── GND
                                         collector ── TACH_LED_K
+5V_MAIN ── R12 680 Ω ── TACH_LED_A
```

MMBT3904 (Nexperia 215): pin1=B, pin2=E, pin3=C.
I_LED ≈ (5 − 1.35 − 0.1)/680 ≈ **5.2 mA** when ON.

### 5) Optocoupler + 3.3 V logic (fixes the old ~1.5 V LOW bug)

VO617A-3 (Vishay, CTR grade 100–200%, SMDIP-4):

| Opto pin | Net |
|---------:|-----|
| 1 LED A | TACH_LED_A |
| 2 LED K | TACH_LED_K |
| 3 E | GND |
| 4 C | TACH_OPTO |

R13 4.7 kΩ from +3V3_MCU to TACH_OPTO.
LED ON → transistor ON → TACH_OPTO **LOW** (≪ 0.8 V; 0.70 mA through R13,
CTR margin >7×). LED OFF → TACH_OPTO = 3.3 V.

### 6) Schmitt to Teensy pin 9

SN74LVC2G17DBVR (dual non-inverting Schmitt, VCC = +3V3_MCU, C22 100 nF):

| Pin | Name | Net |
|----:|------|-----|
| 1 | 1A | TACH_OPTO |
| 2 | GND | GND |
| 3 | 2A | GPS_TX_AUX  (other half buffers GPS) |
| 4 | 2Y | GPS_RX_MCU |
| 5 | VCC | +3V3_MCU |
| 6 | 1Y | TACH_MCU → **Teensy pin 9 only** |

Do **not** use FreqMeasure (that is pin 22 = CAN TX). Firmware is
FreqMeasureMulti on pin 9. Edge polarity does not matter; inverted is fine.
Pin 9 must be a clean 0–3.3 V swing: LOW < 0.8 V, HIGH > 2.3 V.

Chain polarity (for the record, not a firmware change):
vehicle HIGH → TACH_CMP HIGH → Q1 ON → LED ON → TACH_OPTO LOW → pin 9 LOW.

Frequency: ≥500 Hz design margin (2 ppr × 8000 RPM ≈ 267 Hz). C19 = 1 nF on
~20 kΩ Thevenin is ~20 µs — fine at 500 Hz. Do not raise C19 to 100 nF.

Silkscreen at J3: `ECU/CLUSTER ONLY — NO COIL`.

---

## Screen UART (J2, Teensy 14/15)

CrowPanel Advance **J10** (large XH2.54): GND / +5V_IN / TXD0_H / RXD0_H.
J10 is 5 V-tolerant (BSS138 shifters). **Connecting 5 V to the screen’s small
HY2.0 J2 3V3_OUT destroys the ESP32-S3.**

- J2 pin1 GND, pin2 fused +5V_MAIN, pin3 RX FROM SCREEN, pin4 TX TO SCREEN.
- TX/RX **crossed** relative to the screen.
- Fixed-direction **SN74LVC1T45** both ways, 100 Ω series, PESD5V0S1BA (or
  equiv.) ESD, idle pull-ups. 921600 8N1. Not RS-232, not RS-485.

---

## Video UART (J_VID, Teensy 0/1) — extra port vs earlier CAD

Separate Raspberry Pi 5 box. 115200 8N1, **3.3 V both ends**.

```
J_VID pin1 GND  ────────────  Pi header 6  GND
J_VID pin2 TX   Teensy pin 1 ── 1 kΩ ──> Pi header 10 GPIO15 RX
J_VID pin3 RX   Teensy pin 0 <── 1 kΩ ──  Pi header 8  GPIO14 TX
```

No 5 V, no 12 V on this header. Series 1 kΩ on both data lines on **this**
board. Optional ESD to 3.3 V. Do not share this net with the screen UART.
Do not put cameras, USB, or Pi power on this PCB.

---

## On-board modules

### GPS — u-blox NEO-M9N-00B soldered

- UART to Teensy 7/8 through Ioff-capable buffers so AUX cannot back-power MCU.
- USB on the module **disabled** (V_USB to GND, D+/D− open).
- VCC from +3V3_AUX. V_BCKP = VCC (do **not** hang GPS backup on the RTC cell).
- RF: 50 Ω grounded coplanar waveguide to SMA. 100 pF C0G DC block, 27 nH bias
  choke, low-C RF ESD (PESD3V3U1UL or better GNSS-rated).
- Active-antenna 3.3 V bias from AUX through **TPS2553** (ILIM tied to IN →
  50/75/100 mA). VCC_RF unused.

### IMU — MPU-6050 QFN-24 4×4 mm

- Address 0x68 (AD0 = GND). Datasheet caps. Genuine authorized stock only —
  do not substitute ICM/BMI without a firmware revision.
- Power from MCU 3V3.

### WiFi coprocessor — ESP32-S3-WROOM-1-N8R2 (internal antenna, 8 MB / 2 MB)

- Independent of screen WiFi. 2.4 GHz only.
- SPI slave: CS GPIO10, MOSI GPIO11, SCK GPIO12, MISO GPIO13, READY GPIO14.
  Teensy is master. 33 Ω series on CS/MOSI/SCK. Idle pull-ups.
- Buffered with SN74LVC125A / SN74LVC2G125 (Ioff). Open-collector reset from
  Teensy pin 6 into EN. EN/BOOT RC per Espressif.
- Service header J10 as specified. Isolate service RX so a USB-UART cannot
  back-feed an unpowered module (SN74LVC1G125).
- Do **not** add a WiFi SMA, pigtail, or u.FL. Antenna is the module PCB antenna.

### RTC

- CR2032 primary cell in Keystone 1058. **No charger.** Not a UPS.
- J9 → Teensy VBAT / GND only. Install cell after soldering.

### CAN

- Logic-level header J7 only. External SN65HVD230. 120 Ω lives on that module.
- Do not put CANH/CANL screw terminals on this board unless you also put a
  3.3 V transceiver + TVS + 120 Ω and then you must still keep J7 for debug.

---

## Explicitly out of scope (do not add)

- Raspberry Pi, USB cameras, USB SSD, HDMI.
- AEM gauge body, O2 sensor, heater drive, RS-232, CAN from the gauge.
- Ethernet / W5500 / RJ45.
- External WiFi antenna connector.
- Coil / ignition / injector input mode or jumper.
- Second SD card.
- Battery charger / LiPo / whole-system backup.
- Live-stream radio features.
- CrowPanel itself.

---

## DFM / CAD rules

- 2 layer, 2 oz both sides. Min 0.20 mm signal, 0.15 mm clearance. Power trunks
  for 5 V and GND must stay wide; do not let the autorouter neck them to signal
  width. 5 A is a **total** design target.
- GND plane on the bottom. Keep analogue returns with their signals to the
  divider, then single-point to GND.
- GNSS RF: start 0.80 mm / 0.15 mm GCPW; no B-side traces under the RF path;
  stitching vias. Flag that impedance is unverified until stackup Dk is known.
- ESP32 antenna keepout is all-layer. Notch the board under the antenna tip.
- Pololu D36V50F5 is a **raised 2×6 header module** — respect underside
  clearance and use both output/ground columns.
- TRACO TSR-1 footprint is THT SIP.
- Phoenix MKDS-1,5 pitch 5.08 mm, horizontal wire entry.
- Fine-pitch: MPU 0.5 mm, TMUX TSSOP-14, ESP32 land pattern per Espressif
  (0.30 mm drills / 0.60 mm pads on the ground pad; tent/plug so paste does
  not drain).
- Every connector pin numbered on silk to match this brief.
- ERC + DRC clean, zero unconnected pads, schematic netlist = PCB pads.

When a chosen MPN is obsolete, substitute a **pin-compatible** part and list
the substitution. Do **not** substitute the IMU, GPS module, Teensy, or WROOM
antenna variant (no 1U / external-antenna module).

After layout, generate BOM, Gerbers, drills, CPL, and a one-page bring-up
order: (1) no Teensy, power rails only (2) JP1 (3) analogue injection
(4) UARTs disconnected during USB flash.

---

End of prompt.
