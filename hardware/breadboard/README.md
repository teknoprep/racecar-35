# Solderable breadboard — Teensy 4.1 (no Rev C PCB)

This is the **build you solder**. Rev C Gerbers are parked. All car wires land on
**screw terminals along the LEFT edge**. The Teensy sits on the right, USB facing
out for flashing.

Open [BREADBOARD.svg](BREADBOARD.svg) in a browser for the picture. This file is
the soldering notes + parts list. **For the wire-by-wire pinout sheet see
[PINOUT.md](PINOUT.md)**, and for the tach front end see
[OPTO_WIRING.svg](OPTO_WIRING.svg) (PC817 pinout, schematic, bench check).

**Pi 5 is a separate box.** Only three 3.3 V UART wires come here (J3). Cameras
and the USB stick do **not** plug into this board.

## What goes on this board

| On the protoboard | Off the board |
| --- | --- |
| Teensy 4.1 | CrowPanel (cabin) via J2 |
| PC817 tach opto + 1 kΩ + 4.7 kΩ | Raspberry Pi 5 video box via J3 |
| Oil 10 kΩ / 20 kΩ divider | ELP cameras + USB stick (on the Pi) |
| Coolant 150 Ω pull-up | AEM 30-0300 **gauge** (powered from the car, not us) |
| AEM 20 kΩ / 20 kΩ + 1 kΩ / 100 nF | 12 V → 5 V buck (feeds J1, not raw 12 V) |
| Optional: GPS + IMU modules | |

## Screw terminals — LEFT edge, top to bottom

Same idea as Rev C: one edge, labelled, nothing on the other three sides.

| Block | Positions (left → right as you face the screws) | Car / cable |
| --- | --- | --- |
| **J1 POWER** | `5V` · `GND` | Output of a **5 V** buck (3 A+). **Never 12 V.** Also feeds J2 5V. Cut Teensy **VUSB–VIN** if USB and J1 are both live. |
| **J2 SCREEN** | `GND` · `5V` · `TX` · `RX` | CrowPanel **J10 only** (XH2.54, the large one). `TX` = Teensy pin 14 → screen RX. `RX` = Teensy pin 15 ← screen TX. **Never CrowPanel J2 / HY2.0** (5 V there kills the ESP32). |
| **J3 VIDEO** | `GND` · `TX` · `RX` | Pi 5 header 6 / 10 / 8. `TX` = Teensy pin 1 → Pi GPIO15. `RX` = Teensy pin 0 ← Pi GPIO14. **3.3 V only. No 5 V.** 1 kΩ in series on both data lines on this board. |
| **J4 TACH** | `SIG` · `GND` | Conditioned ECU / cluster tach **only**. Not coil-neg, spark, or injector. |
| **J5 OIL** | `5V` · `SIG` · `GND` | 5 V 0.5–4.5 V transducer (150 PSI typical). |
| **J6 NTC** | `SIG` · `GND` | Two-wire coolant sender. Dedicated GND — do not rely on the engine block. |
| **J7 AEM** | `WHT` · `BRN` | AEM 30-0300 Power/IO **pin 9 WHITE** and **pin 10 BROWN**. Gauge keeps its own 12 V / 5 A fuse. We take analogue only. |

## Circuits you solder (values are firmware contracts)

### Tach — PC817 on the board (J4 → pin 9)

Full diagram: **[OPTO_WIRING.svg](OPTO_WIRING.svg)**. The LED is inside the PC817
(1 = anode, 2 = cathode); you never add one.

```
J4 SIG ── 1 kΩ ── PC817 pin 1 (anode)
J4 GND ────────── PC817 pin 2 (cathode) ── board GND

3.3 V ── 4.7 kΩ ──┬── PC817 pin 4 (collector) ── Teensy pin 9
                  └── 100 nF to GND (optional, right at pin 9)
GND ─────────────── PC817 pin 3 (emitter)
```

DIP-4, notch up: pin 1 is top-left. Output is inverted; firmware does not care.
Tach at pin 9 must swing 0–3.3 V. LOW must go below ~0.8 V.

### Oil — 10 kΩ / 20 kΩ (J5 → pin 16 / A2)

Firmware expects `V_adc = V_sensor × 2/3`.

```
J5 5V  ──────── Teensy 5V (or J1 5V)
J5 GND ──────── board GND
J5 SIG ── 10 kΩ ── Teensy pin 16 (A2) ── 20 kΩ ── GND
                         └── 10 nF to GND (optional, at the pin)
```

At atmosphere a 0.5–4.5 V sender reads ~0.5 V on J5 SIG (~0.33 V on A2).

### Coolant — 150 Ω to 3.3 V (J6 → pin 17 / A3)

VDO 1600–22 Ω curve. Different sender = different pull-up (see WIRING.md).

```
3.3 V ── 150 Ω ──┬── Teensy pin 17 (A3)
J6 SIG ──────────┘
J6 GND ──────────── board GND
```

### AEM AFR — 0.500 gain, NEVER raw 5 V (J7 → pin 20 / A6)

WHITE can be 5 V. Teensy ADC dies at 3.6 V. This divider is not optional.

```
J7 WHT ── 20 kΩ 0.1% ──●── 20 kΩ 0.1% ── J7 BRN ── board GND
                       │
                       └── 1 kΩ ── Teensy pin 20 (A6) ── 100 nF ── GND
```

Settings → **AEM 30-0300 AFR input** stays OFF until this is soldered and
metered: 5.00 V on WHT-BRN must read ~2.50 V on pin 20.

## UART (two different links — do not mix)

| Link | Teensy | Other end | Baud | Levels |
| --- | --- | --- | --- | --- |
| Dash | 14 TX, 15 RX, GND | CrowPanel **J10** RX / TX / GND (+5 V if the screen is powered from J1) | 921600 | Screen J10 is 5 V-tolerant via its shifters |
| Video | 1 TX, 0 RX, GND | Pi 5 GPIO15 / GPIO14 / GND | 115200 | **3.3 V both ends** |

TX/RX are **crossed** on both cables.

## Optional modules on the protoboard (no extra terminals)

| Module | Teensy | Power |
| --- | --- | --- |
| u-blox NEO-M9N | pin 7 RX ← GPS TX, pin 8 TX → GPS RX | 3.3 V / GND |
| GY-521 MPU-6050 | pin 18 SDA, pin 19 SCL | **3.3 V** (not 5 V), AD0 to GND |
| CAN transceiver module | pin 22 TX, pin 23 RX | **5 V VCC + 3.3 V VIO — NOT 3.3 V on VCC. See the warning below.** |

⚠️ **The Amazon "3-Pack SN65HVD230 CAN Transceiver Module" (ASIN B0FDLDXCK9) is
MISLABELLED — the SOIC-8 on it behaves as a TJA1051T/3-class chip, not an SN65HVD230.**
The two parts share the footprint but not the pin meanings, so wire it as a TJA1051T/3:

| Module pin | SN65HVD230 would be | What the fitted chip actually needs |
| --- | --- | --- |
| 3 (VCC) | 3.3 V | **5 V** (needs 4.5–5.5 V; the driver is locked out below that) |
| 5 | Vref — leave floating | **VIO = 3.3 V** — floating it lets TXD phantom-feed RXD (measures ~2.1 V) |
| 8 | Rs | **S / standby → GND** (high = receive-only, never ACKs) |

Wired the SN65HVD230 way (VCC = 3.3 V, pin 5 floating, pin 8 floating) the logger
**receives but never transmits a dominant bit**, so it never ACKs; the sender then
retransmits ~3,800 identical frames/s and values update only a few times a second.
With VCC 5 V / pin 5 3.3 V / pin 8 GND it runs at exactly 200 frames/s, both IDs, 0
errors. Full analysis and the un-foolable acceptance tests:
`../CAN-TRANSCEIVER-FINDINGS-2026-10-04.md`.

## Parts to buy for this board

| Qty | Part | Why |
| ---:| --- | --- |
| 1 | Solderable protoboard ≥ 100 × 150 mm (plated holes, 2.54 mm) | Everything lives here |
| 1 | Teensy 4.1 + 2× 24-pin headers (socket if you want it removable) | |
| 7 | Screw-terminal blocks, 5.08 mm, mix of 2- and 3-position (J2 is 4-pos) | Left-edge harness |
| 1 | PC817 (or 4N35) DIP-4 | Tach |
| 1 | 1 kΩ ¼ W (tach LED) | |
| 1 | 4.7 kΩ ¼ W (tach pull-up) | 10 kΩ is also fine |
| 1 | 150 Ω ¼ W (coolant) | |
| 1 | 10 kΩ ¼ W (oil series) | |
| 1 | 20 kΩ ¼ W (oil bottom) | |
| 2 | 20 kΩ **0.1%** (AEM divider) | Gain is a firmware constant |
| 1 | 1 kΩ (AEM filter) | |
| 2 | 1 kΩ (video UART series) | |
| 3 | 100 nF ceramic | AEM filter + decoupling |
| 1 | 10 nF ceramic | Oil (optional) |
| 1 | 5 V ≥ 3 A buck, 12 V in, screw terminals out | Feeds J1. Not the Pi 5 supply. |

## Solder order

1. Headers / socket for the Teensy. Confirm pin 0 is the corner next to GND/USB.
2. Seven terminal blocks on the **left** edge. Label them **before** wires go in.
3. Star GND: one fat jumper from J1 GND to a ground rail, then every circuit GND to that rail. Do not daisy-chain sensor grounds through each other.
4. Tach PC817 + 1 kΩ + 4.7 kΩ. Bench: pulse J4 SIG with 12 V through the 1 kΩ, pin 9 should toggle.
5. Oil divider. Meter A2 < 3.3 V with the sender at 5 V full-scale.
6. Coolant 150 Ω.
7. AEM divider. Meter pin 20 with a 5 V bench supply on J7 **before** enabling `afraem`.
8. J2 four wires to CrowPanel J10. Disconnect them whenever you USB-flash the screen.
9. J3 three wires + 1 kΩ series to the Pi 5. Power the Pi from its own 5 V 5 A USB-C.

Firmware v0.1.150: Settings → **Video interconnect** ON only after J3 is wired.
Settings → **AEM 30-0300 AFR input** ON only after J7 is metered.
