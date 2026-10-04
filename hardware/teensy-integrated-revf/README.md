# Rev F — four-layer integrated trunk logger

**UNBUILT ENGINEERING PROTOTYPE. NOT INDEPENDENTLY REVIEWED OR PHYSICALLY
VALIDATED.** A clean CAD report is not electrical approval, a thermal test, an
RF measurement, or evidence that the board works.

Produced to close three gaps in Rev C: the missing Raspberry Pi video UART and the
missing throttle-position and brake-pressure analog inputs.

**Rev E closed two more:** the 5 V rail is **our own TPS54560B converter** instead of a
Pololu module that could not be sourced, and the **car's own input voltage is measured on
the board** instead of being inferred from the ECU or a Bluetooth dongle.

**Rev F closes the CAN gap.** The external SN65HVD230 plug-in module and its `J7` header
are **deleted**; a **TCAN1042HGV-Q1** is soldered on with its **STB pin hard-tied to GND**,
and `J14` presents `CANH` / `CANL` / `GND` with switchable split termination. See
[Onboard CAN (Rev F)](#onboard-can-rev-f--no-plug-in-module).

## Deliverable

- **`Racecar-RevF-GERBERS-ENGINEERING-PROTOTYPE.zip`** — 11 Gerber layers
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
| Pin-contract / geometry / calculation assertions | **749 passed** |
| Schematic ↔ PCB connected pads | **538 agree** |
| Footprints / nets / routed items | 193 / 104 / 1,647 segments with 131 vias |

## Onboard CAN (Rev F) — no plug-in module

**Problem (measured on the bench, 2026-10-03).** The external SN65HVD230 module on `J7`
received frames but **never drove the bus**: with `TXD` held at 0 V, `CANH-CANL` measured
0 V. Two different Teensys, two modules of that product and a bare 30-line FlexCAN_T4
sketch all behaved identically. With no ACK from the logger the ECU retransmitted one frame
forever — telemetry updated 1.8 times a second instead of ~100, and the second frame id
never arrived. A receive-only node is worse than no node, so the module and its header are
gone.

**Fix: a soldered transceiver whose mode pin cannot be left in standby.**

* `U21` = **TI TCAN1042HGVDRQ1** (`TCAN1042HGV-Q1`), SOIC-8, AEC-Q100, ±70 V bus fault
  protection, **`VIO` on pin 5** for 3.3 V logic. Order of preference for alternates:
  `TCAN1042VDRQ1`, then NXP `TJA1051T/3/1J` (its pin 8 `S` must also go to GND).
  **Never** an SN65HVD230 (±4/+16 V bus fault — the failure being removed) and **never** a
  non-VIO TJA1051T/TJA1050 (5 V RXD would damage the Teensy).
* Pins: 1 `TXD`→`CAN_TX` (Teensy pin 22 = U1 pad 29), 2 `GND`, 3 `VCC`→`+5V_MAIN`,
  4 `RXD`→`CAN_RX` (Teensy pin 23 = U1 pad 28), 5 `VIO`→`+3V3_MCU`, 6 `CANL`, 7 `CANH`,
  **8 `STB` hard-tied to GND**. `STB` high is standby = receive-only with no ACK — exactly
  the state being designed out — so there is no pull-up, no resistor and no jumper on it.
* Decoupling `C49`/`C50` 100 nF at VCC/VIO and `C51` 1 µF on VCC. `U21` needs the 5 V rail,
  so **CAN does not work on Teensy-USB power alone**; the bench needs `J1` powered. That a
  powered `VIO` with `VCC` off (USB-only bench) leaves the bus passive must be confirmed
  against the TCAN1042 datasheet — this document does not claim it.
* `J14` "CAN" (Phoenix **1729021**, 3-position 5.08 mm, the same family as J4/J11/J12):
  **1 `CANH`, 2 `CANL`, 3 `GND`** (signal-ground reference to the ECU).
* **Switchable split termination**: `R58` + `R59` = two **60.4 Ω 1 %** in series (120.8 Ω
  across the pair), midpoint to GND through `C52` 4.7 nF (50 V), in series with **`JP2`**.
  **Fit the JP2 shunt only if this board is an end of the bus.**
* `D25` = **onsemi NUP2105LT1G** dual CAN TVS at `J14` (alternates: Nexperia `PESD2CAN,215`
  or TI `ESD2CAN24DBZRQ1`).
* Test points `TP6` (`CAN_TX`) and `TP7` (`CAN_RX`). `J7` and its BOM line are deleted —
  two RXD drivers would otherwise fight on U1 pad 28.
* **No common-mode choke.** The optional TDK `ACT45B-510-2P-TL003` with 0 Ω bypass links is
  **omitted**: there was no room for it and its links beside `U21` without displacing the
  analog front ends. Add it on a future revision only if conducted/radiated emissions
  measurements show it is needed.
* **No firmware change:** 500 kbit/s, normal (ACKing) mode. The TCAN1042's TXD dominant
  time-out (≈1 ms) will cut the firmware's `CANHOLD` diagnostic short — that is expected,
  real CAN traffic never holds dominant that long.
* **Not claimed:** automotive transient qualification or a working vehicle bus. The ESD
  and termination parts are selected but `CAN` has not been built, measured or reviewed.

**Also new in Rev F — the silkscreen was restored.** Rev C–E's generator moved *every*
footprint's silk to `F.Fab`, which left the fabricated board with **no component markings
at all** (0 of 193 footprints had any `F.SilkS`): no pin-1 dots, no diode cathode bands, no
electrolytic polarity marks, no outlines. Rev F keeps them (only the ESP32 module's
outline, which crosses the antenna-notch edge, stays on `F.Fab`) — so `U21` now carries a
pin-1 dot and every diode/electrolytic shows its polarity on the board.
* **Layout limitation of this pass:** the autorouter routed the logic pair
  `CAN_TX`/`CAN_RX` at roughly **80–90 mm** over two inner layers and `CANH`/`CANL` at
  roughly **30–45 mm** (`U21`↔`J14` ≈23 mm). Longer than a hand layout would give and
  harmless at 500 kbit/s — the lengths vary between autorouter runs and are quoted as a
  range — but a future pass should shorten the logic-side pair to U1 pads 29/28.

## Rev E — our own 5 V rail and the on-board input monitor

* **The 5 V rail is our own converter.** U2 is a **TI TPS54560B** 60 V / 5 A asynchronous
  buck at 400 kHz, built from TI's own 5 V/5 A design example (SLVSBN0C 8.2) with the input
  range narrowed to the car's real 10–20 V: `RT/CLK` 240 k (400 kHz), feedback 52.3 k/10 k
  (5.0 V), UVLO 680 k/100 k (**~9.4 V start** — car-appropriate, not TI's 6.5 V),
  compensation 16.9 k + 4.7 n + 47 p, output 3 × 47 µF, inductor 6.8 µH, catch diode `SS56`.
  The **Pololu D36V50F5 module and its 2×6 socket are gone**.
* **Reverse-polarity protection is explicit and ours.** The module provided it internally via
  `VRP`; Q3 (`AO4485` P-channel) now sits high-side with its **DRAIN on the raw input and
  SOURCE on the protected load** so the body diode blocks a reversed battery. R51 holds the
  gate low, D24 (`BZX84C15`) clamps |Vgs| against the 32 V TVS clamp. ⚠️ Wired the other way
  round the body diode conducts — asserted in `electrical/verify.py`.
* **Car input voltage monitor.** 180 k/20 k **0.1 %** divider (1:10) on the protected input
  with its own LM4040 3.0 V shunt, Schottky clamp, 1 k/100 nF filter, switched by the free
  4th channel of U15 (TMUX1511) onto **Teensy pin 40 = A16 = U1 pad 41** (an **ADC1** pin;
  A14/A15 are ADC2-only). 8 mV/LSB, saturating near 30 V.
* ⚠️ **The new buck dissipates ~1.5 W more** than the synchronous module it replaces (catch
  diode instead of a low-side FET; ≈87 % vs ≈90 % at 5 A). The continuous-5 A claim stays
  **gated on bench thermal testing**, and the PowerPAD depends on the copper it is given.

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
stays away from sensor routing. The Pi video UART is a **JST XH** rather than a
screw terminal, which saved ~16 mm of edge.

**`J14` (CAN) is the one field terminal that is NOT on the left edge.** The left edge
cannot take a ninth 5.08 mm screw terminal without ~1 mm body gaps, and no room would
remain beside it for the transceiver, termination and TVS. `J14` therefore sits
mid-board at the old `J7` position, where `U21` is within ~20 mm of it and `CANH`/`CANL`
stay short. The eight left-edge field terminals and their order are unchanged.

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
4. **Silkscreen** — Rev F keeps each footprint's own silk: **pin-1 dots, diode cathode
   bands, electrolytic polarity marks and outlines.** Rev C–E moved *all* footprint silk
   to `F.Fab`, so the board carried none of them (0 of 193 footprints had any `F.SilkS`).
   The only exception is the ESP32 module, whose outline crosses the antenna-notch board
   edge. Six terminal pin labels are still dropped where the dense left column cannot
   clear the solder-mask apertures; the `J14` pin labels and the `CAN TERM FIT ONLY AT BUS
   END` note are placed clear of every mask aperture. Full pinouts are in
   `design/ASSEMBLY.md`.

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

**U2 is a converter we build, not a module.** PCBWay sources and fits every part from stock:
TI `TPS54560BDDAR`, Vishay `IHLP4040DZER6R8M01`, `AO4505`-class `AO4485`, `SS56`, `BZX84C15`
and Murata ceramics (see `pcbway/Racecar-RevF-BOM-pcbway-assembly.csv`). No consignment and
no Pololu module. **The only sockets on the board are the two 1x24 headers at U1** — that
part of the BOM reads `SSW-124-01-G-S` because the **Teensy 4.1 is customer supplied**
(buy it with pins installed) and PCBWay must NOT fit one; they fit the two sockets and we
plug the Teensy in. Left to us: the CR2032 cell (never reflowed), the JP1 and JP2 shunts
(JP1 after unloaded rail tests; **JP2 only at a CAN bus end**), plugging in the Teensy, and
the 2-wire J9 RTC lead.

The files to send are `pcbway/Racecar-RevF-PCBWAY-GERBERS.zip` (Gerbers+drills),
`pcbway/Racecar-RevF-BOM-pcbway-assembly.csv` and `pcbway/Racecar-RevF-CPL.csv`;
`pcbway/make_package.py` regenerates all three from `components.json` and the routed PCB,
so they cannot drift from the exported design. Board settings, the U1 fitting rule and the
CAN notes are in `pcbway/PCBWAY-ORDER-GUIDE.md`.

## Reproducing


```sh
python3 electrical/make_icm_footprint.py     # derived LAND pattern
python3 electrical/generate.py               # overwrites the routed PCB
python3 electrical/schematic.py
python3 electrical/route.py                  # locked power/RF + DSN
java -jar freerouting-1.9.0.jar -de design/racecar-integrated-revf.dsn \
     -do design/racecar-integrated-revf.ses -mp 60
python3 electrical/route.py --finish         # import SES, fill the GND plane
python3 electrical/complete_routes.py        # last connections + cleanup
python3 electrical/verify.py
python3 fabrication/export.py --engineering-prototype
python3 pcbway/make_package.py               # PCBWay BOM + CPL + gerber-only ZIP
```

Rev C is retained unchanged at `../teensy-integrated-revc/`.
