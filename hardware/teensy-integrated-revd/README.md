# Rev D — four-layer integrated trunk logger

**UNBUILT ENGINEERING PROTOTYPE. NOT INDEPENDENTLY REVIEWED OR PHYSICALLY
VALIDATED.** A clean CAD report is not electrical approval, a thermal test, an
RF measurement, or evidence that the board works.

Produced to close three gaps in Rev C: the missing Raspberry Pi video UART, and
the missing throttle-position and brake-pressure analog inputs.

## Deliverable

- **`Racecar-RevD-GERBERS-ENGINEERING-PROTOTYPE.zip`** — 11 Gerber layers
  (4 copper + both masks/silkscreens/pastes + Edge.Cuts), separate metric
  **PTH and NPTH** Excellon drills with maps, schematic PDF, BOM, assembly
  positions CSV, fabrication/assembly/bring-up notes, editable CAD, validation
  reports and a SHA-256 manifest. Verified CRC and per-file hashes.

## Status at export

| Check | Result |
|---|---|
| ERC | **0 violations** |
| DRC (full severity) | **0 violations** |
| Unconnected items | **0** |
| Pin-contract / geometry / calculation assertions | **531 passed** |
| Schematic ↔ PCB connected pads | **456 agree** |
| Footprints / nets / routed items | 158 / 89 / ~1,470 with 96 vias |

## What changed from Rev C

- **Four copper layers.** `In1.Cu` is a **continuous ground reference plane**
  (declared to the router as a plane layer so signals cannot consume it), with
  the WiFi antenna and GNSS RF rule areas cut out of it. `F.Cu`, `In2.Cu` and
  `B.Cu` carry routing. This is the single biggest electrical improvement: the
  Rev C two-layer board had to buy ground return out of the signal layers.
- **No long locked ground trunk.** Rev C required a hand-locked 4 mm `B.Cu`
  ground path. With a real plane that trunk became unnecessary — and as a
  **wall** it left `DASH_RX_5V`, `+5V_SCREEN→C33` and `NET_RESET_ASSERT`
  unroutable. Removing it is what allowed the autorouter to finish.
- **ICM-42670-P** replaces the MPU-6050 on I²C 18/19 at address 0x68
  (`AP_AD0`=GND, `AP_CS`=VDDIO selects I²C), polling only, INT1/INT2 unconnected.
  **Its land pattern is DERIVED** from DS-000451 (TDK publishes none) and has
  been checked against the datasheet: 0.50 mm pitch, 0.26 x 0.55 mm pads, 1.00 mm
  edge span, 0.14 mm mask web — all match and all are inside fab capability.
- **Throttle (J11) and brake (J12)** analog inputs on Teensy **A7 (pin 21)** and
  **A10 (pin 24)** — the last two free ADC-capable outer-row pins. Both use the
  same protected 0.500-gain front end as oil/AFR/NTC (20k/20k 0.1 %, LM4040 3.0 V
  shunt, Schottky clamp, 1 kΩ/100 nF, TMUX1511 isolation) and their own
  PTC-fused 5 V branch. Oil/coolant/AFR stay on A2/A3/A6.
- **Raspberry Pi 5 video UART (J13)** on Teensy pins 0/1, 3.3 V, with 1 kΩ
  series resistors and ESD. Deliberately separate from the 5 V screen UART.
- **Second TMUX1511** for the two new analog channels.

## Envelope and mechanical

**150 × 155 mm** (Rev C was 130 × 140 mm). The board had to grow: eight screw
terminals at 5.08 mm pitch plus three analog front ends do not fit the old
outline. All eight field terminals still face **one left edge**:

`J3` TACH · `J4` OIL · `J5` NTC · `J6` AEM AFR · **`J11` THROTTLE** ·
**`J12` BRAKE** · `J1` POWER · `J2` SCREEN

Power/tach terminals first, then the analog terminals, so high-current wiring
stays away from sensor routing. The Pi video UART is a **JST XH** (like the
existing CAN header) rather than a screw terminal, which saved ~16 mm of edge.

WiFi antenna notch is at the bottom edge (x 44–92), all-layer keepout retained.

## Manufacturing notes

1. **ICM-42670-P land pattern** — derived from DS-000451 rev 1.2 s10.2 and
   verified against the datasheet (pitch, pad size, edge spans, mask web). It is
   not a vendor-published pattern, but it is arithmetically correct and
   manufacturable. A pin-1 silkscreen marker is on the board.
2. **Copper weight** — 1 oz. The screen branch fuse is **3 A** (`F2`,
   Littelfuse `0451003.MRL`), so the 3.5 mm `+5V_MAIN` trunk is adequate at 1 oz.
3. **GNSS feed** — 0.38 mm microstrip over `In1.Cu`. The standard 4-layer 1.6 mm
   stackup (7628 x1 prepreg, H = 0.1955 mm, eps_r 4.2-4.6) gives **49.6-50.6 ohm**.
   Order with impedance control and state the 50 ohm target.
4. **Silkscreen** — six terminal pin labels were dropped where the denser left
   column could not clear the solder-mask apertures; function labels and pin-1
   marks remain, and full pinouts are in `design/ASSEMBLY.md`.

## Firmware is NOT done

This is hardware only. Still required:

- **ICM-42670-P driver** plus automatic identity detection (`0x67`) alongside
  legacy MPU-6050 (`0x68`). Tag the `ImuCalStore` EEPROM record with a sensor type
  so a removable Teensy cannot reuse the wrong offsets.
- **Throttle and brake channels** need conversions, plausibility limits and
  logging fields.
- Sensor axis mapping depends on final physical placement.
- Oil/coolant conversions must match the Rev C/D analog front end, not the
  legacy firmware values.
- The ESP32-S3 network coprocessor firmware/transport is still unimplemented;
  **Teensy pin 13's heartbeat must stop before SPI is enabled.**

## BOM revision 1 — procurement substitutions (2026-09-29)

Applied after the PCBWay quote **T-2SJ3W1113248A** (5 units) came back with five
out-of-stock lines. **No footprint, net, or routing change** — every substitution
below is the same package, so the Gerbers are unchanged; the shipped fab package
was re-exported so its `BOM.csv` matches. Details and sourcing:
`pcbway/PARTS-AVAILABILITY-REVIEW.md`; the reply sent to PCBWay:
`pcbway/PCBWAY-REPLY-2026-09-29.pdf`.

| Ref | Was | Now | Why |
|---|---|---|---|
| F3 F4 F5 | `1206L010/30YR` | `1206L010/60WR` | the old part number does not exist in the Littelfuse 1206L series |
| D5 | `PESD3V3U1UL,315` | `PESD5V0F1BL,315` | unobtainable; same SOD-882, 5.5 V standoff (antenna bias is 3.3 V) and 0.4 pF instead of 2.6 pF |
| D2 | `SS14` (Diodes Inc) | `SS14` (**onsemi**) | the bare MPN is not orderable from Diodes Inc |
| C10 | `GRM21BR71H103KA01L` | `CL21B103KBANNNC` (Samsung) | no stock; same 10 nF 50 V X7R 0805 |
| C11 | `GRM21BR71H225KA01L` | `CC0805KKX7R9BB225` (Yageo) | no stock; X7R (PCBWay's proposed X5R part is only rated to 85 °C) |
| C18 | `GRM21BR71H102KA01L` | `CC0805KRX7R9BB102` (Yageo) | no stock; same 1 nF 50 V X7R 0805 |

**U1 (Teensy 4.1) and U2 (Pololu D36V50F5) are customer-fitted**, not assembled by
PCBWay: they are socketed/headered modules, and Pololu's own store is rationed.
The turnkey BOM file to send is `pcbway/Racecar-RevD-BOM-pcbway-assembly-REV1.csv`
(the canonical full BOM stays in `pcbway/Racecar-RevD-BOM-pcbway-assembly.csv`).
Two parts are **not in the BOM** and must be bought separately: the **2× 1×24
2.54 mm female headers** for the Teensy socket, and the **CR2032 cell** itself.

## Reproducing


```sh
python3 electrical/make_icm_footprint.py     # derived LAND pattern
python3 electrical/generate.py               # overwrites the routed PCB
python3 electrical/schematic.py
python3 electrical/route.py                  # locked power/RF + DSN
java -jar freerouting-1.9.0.jar -de design/racecar-integrated-revd.dsn \
     -do design/racecar-integrated-revd.ses -mp 60
python3 electrical/route.py --finish         # import SES, fill the GND plane
python3 electrical/complete_routes.py        # last connections + cleanup
python3 electrical/verify.py
python3 fabrication/export.py --engineering-prototype
```

Rev C is retained unchanged at `../teensy-integrated-revc/`.
