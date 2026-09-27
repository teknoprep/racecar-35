# Prototype fabrication / assembly order notes

**STATUS: unbuilt engineering prototype. Not released for permanent vehicle use.**
Obtain an independent schematic, power-protection, footprint and harness review
before placing even a prototype order. Do not order a production batch.

## Bare PCB specification

- Outline: **130.00 × 110.00 mm** rectangular, from Edge.Cuts only.
- Layers: **2** (F.Cu / B.Cu).
- Material: FR4, Tg >= 150 °C preferred, UL94 V-0.
- Finished board thickness: **1.6 mm**, normal manufacturer tolerance.
- **Finished copper: 2 oz / approximately 70 µm on BOTH sides. DO NOT substitute 1 oz.**
- Finish: ENIG preferred; lead-free HASL acceptable if assembler approves pads.
- Solder mask: both sides, any colour; silkscreen both sides.
- Min design track: 0.25 mm; routed signals normally 0.30 mm.
- Clearance: 0.20 mm board minimum; routing normally 0.25 mm.
- Vias: normally 0.80 mm pad / 0.40 mm finished drill.
- Through-hole module/header pads: generally 1.00 mm drill; use supplied drill files.
- Chassis holes: four **3.2 mm NPTH** for M3 insulating standoffs.
- Electrical net testing: REQUIRED. No intentional net ties or copper shorts.
- Main current paths: manually routed 3.0 mm tracks, 2.6 mm short module necks,
  wide GND return tracks plus both-side pours. Do not let fabrication software
  arbitrarily remove copper pours or narrow these paths.
- Prefer 2–5 prototypes initially, not a production order.

Gerbers do not encode a reliable purchase order for copper weight/material. Give
these notes to the fab explicitly. Confirm their 2 oz clearance/annular-ring rules
against the final files. The Gerber ZIP includes a front-paste layer for assembly;
it is not an extra copper layer. Plated and non-plated drill files are separate.

## Assembly

- Use `BOM.csv`, `BOM-extra.csv`, `assembly-positions.csv`, `assembly-top.pdf`, and
  `schematic.pdf` together. Position rotations are KiCad conventions, not guaranteed
  to match a particular assembler's zero-angle definition.
- The **Teensy is removable**, on two 1×24 female sockets, 2.54 mm pin pitch and
  15.24 mm row spacing. Top view: USB at the upper/short end shown in the drawing.
- **Do not populate a bare i.MX RT processor**: U1 is a complete PJRC Teensy 4.1.
- U2 is a complete **Pololu 4091 / D36V50F5** module, mounted above the PCB with its
  component side UP. Fit all twelve positions of a 2×6 male header. Solder both
  rows at both boards; use >=3 A/contact parts. Both output pins must carry current.
- The U2 outer and inner rows are NOT identical: final row is **EN outside / PG
  inside**. Working TOP view pin order is VOUT, GND, GND, VIN, VRP, EN/PG. Do not
  mirror a bottom-view photograph. The saved image explicitly labels the view.
- U2 underside has live components. Use a nonconductive support/spacer and clearance
  from the carrier; don't rest it on copper or metal screw heads. No guessed U2
  mechanical screw holes are drilled. The dual header supports the module; add
  nonconductive mechanical restraint appropriate for vibration after fit testing.
- U3 is a complete **Traco TSR 1-2433** module, pin 1 VIN, 2 GND, 3 3V3 OUT. The
  module is fed from U2 **VRP**, not raw reverse-polarity input or the 5 V rail.
- D1 **SMBJ20CA** is the bidirectional TVS. Do not replace with a unidirectional
  part without reviewing negative-input behaviour and fuse coordination.
- D2 SS14: pad 1 is cathode toward JP1/Teensy VIN; pad 2 anode toward main 5 V.
- C1 **EEU-FR1H101 (no B suffix)** is 8 mm diameter / **3.5 mm lead pitch**.
  The similar EEU-FR1H101B has **5 mm lead pitch** and is not a drop-in assembly
  substitution. C2 EEU-FR1A471B is 8 mm diameter / 3.5 mm pitch. Observe polarity.
- Direct GND pours need appropriate preheat/iron capability. Do not overheat modules
  trying to solder ground pins with an underpowered iron.
- Fit U4/U5 SN74LVC1T45**DBVR** and U6 SN74LVC2G17**DBVR** in SOT-23-6 packages.
  Other TI package suffixes are not footprint-compatible. Check pin-1 indicators.
- Leave JP1 **open** and the Teensy/GPS/IMU/screen disconnected for initial power
  testing. The shunt is fitted only at the stage described in BRINGUP.md.

## Mandatory pre-order checks

1. Open the KiCad project and PDFs; have another qualified person review power and
   MCU pin maps. A zero-DRC report does not verify the circuit will work.
2. Print the assembly drawing at **100% scale**, check with a ruler, and lay the
   purchased Teensy, sockets, Pololu and Traco modules on it. Confirm header pitch,
   row separation, module orientation, underside clearance and enclosure access.
3. Confirm the exact MPN/footprint match for every purchased connector and capacitor.
   Vendor substitutions require review, particularly electrolytic lead forming.
4. Confirm CrowPanel **clone** J10 power pin order and TTL levels against its own
   documentation/meter measurements. A USB MAC/chip ID is not evidence of connector
   compatibility. This is a release blocker until verified on that physical panel.
5. Read the bring-up plan. Use a current-limited supply and suitable loads; do not
   make an untested board's first power-up through an expensive screen or MCU.
