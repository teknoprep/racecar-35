# Racecar-35 Teensy carrier — Rev A prototype

**UNBUILT / NOT RELEASED FOR VEHICLE USE.** This is a complete, routed **prototype
carrier PCB**, not a validated automotive ECU and not a production safety sign-off.
Start with a small prototype order only after an electronics engineer reviews the
schematic, module orientations, connector pinouts, and protection scheme. Read
[BRINGUP.md](BRINGUP.md) before connecting a Teensy or display.

## What you get

- Editable **KiCad 9** project, hierarchical schematic and local libraries in `design/`.
- `schematic.pdf`: overview + four circuit/connectivity sheets.
- `BOM.csv`: per-reference manufacturer and part number; `BOM-grouped.csv`: grouped
  purchasing list (excludes bare-PCB test pads/mounting holes); `BOM-extra.csv`: sockets,
  mating connectors, harness fuse, standoffs and externally attached modules.
- `gerbers/`: two copper layers, solder masks, silkscreens, front paste, outline,
  plated/non-plated Excellon drill files and a drill map.
- `racecar-carrier-reva-GERBERS-PROTOTYPE.zip`: bare-PCB manufacturing files only.
- `assembly-positions.csv`, `assembly-top.pdf`, `board-top.png`, `board-bottom.png`.
- `reports/`: PCB DRC, limited schematic ERC, schematic/netlist cross-check and hashes.
- `export.sh`: fail-hard verification and export; does **not** regenerate or reroute.

A PCB fabricator needs the **Gerber ZIP plus FABRICATION.md**. Gerbers alone do not
specify copper weight. A PCB assembler additionally needs the BOM, positions, PDFs,
and hand-assembly notes. This is **not** a turnkey JLCPCB/LCSC assembly order: the
Teensy, Pololu and Traco modules, sockets and some connectors may require consignment
or hand assembly. Position-file rotations must be reviewed against the assembler's
package conventions. There is no claim that every MPN is currently stocked.

## Basic design and limits

| Item | Rev A |
|---|---|
| PCB | 130 × 110 mm; 2 layers; 1.6 mm FR4; **2 oz finished copper on BOTH sides** |
| Main input | **10–20 V DC**, common negative; not an unprotected battery connection |
| Main regulator U2 | **Pololu D36V50F5, product 4091**; published 5.5–50 V input, 5 V nominal, typical 5.5 A at 36 V; this carrier budgets **5 A total** on the main 5 V rail |
| Screen branch | 5 V output, **4 A fast fuse F2**; no adjustable output voltage |
| Auxiliary regulator U3 | **Traco TSR 1-2433**, 3.3 V, nominal 1 A; Rev A peripheral budget **500 mA combined** until thermal testing |
| MCU | Socketed **Teensy 4.1**, supplied at VIN through SS14 and JP1; never externally feed its 3.3 V pins |
| Screen link | Full-duplex **5 V TTL UART**, translated to Teensy's 3.3 V logic; 921600 baud firmware |
| Screen connector | Four-position 5.08 mm screw terminal, matching functions of Advance **J10** |
| Tach | External optocoupler output -> pull-up/filter/Schmitt buffer -> **Teensy 9** |
| GPS | Four-pin JST-XH custom UART connector, **3.3 V power and logic** |
| IMU | Four-pin JST-XH, 3.3 V, I²C on Teensy 18/19 |
| Optional CAN | Connector for an **external SN65HVD230 transceiver module**; not CANH/CANL |
| Optional Ethernet | Custom 2×4 header for a **3.3 V W5500 breakout**; not a plug-compatible W5500 footprint |

The regulator's published current is not a guarantee of **continuous 5 A in your
closed, hot trunk enclosure**. Verify at 10, 14.4 and 20 V, maximum brightness/radio
activity, and maximum intended ambient. Fuse values are not precise current limits.
The screen is not forced to draw 4 A or 5 A; it draws its own load current.
The screen fuse leaves margin for the MCU. U3 is fed separately from the protected
input, so its output is not drawn through the Teensy's small 3.3 V regulator.

This first PCB deliberately uses an **assembled regulator module** instead of a
new discrete 25 W buck-converter design. U2 mounts above the carrier on a soldered
2×6 header. That makes the power design easier to build and review; it is not the
cheapest possible mass-production implementation.

### Not included

GPS receiver/antenna, MPU-6050, CAN transceiver and W5500 are external modules.
There is **no** SMA connector or GNSS chip on this carrier: the SMA stays on the GPS
breakout, away from switching regulators. The Teensy's own SD slot is used. No raw
coil/ignition input, no direct 12 V tach, and **no oil-pressure/NTC conditioning** are
implemented. Firmware ADC readings must not be trusted without the existing proper
external conditioning; pins 16/17 are not brought to sensor screw terminals here.

There is no firmware change, GPS-module auto-detection, or M10/F10 library migration
in this PCB package. The current firmware's tested target remains NEO-M9N.
**A compatible UART voltage does not imply software compatibility with every u-blox
generation.** Verify the exact GPS board and library support before purchasing.

## Power protection — what it does and does not do

```
J1 +10–20V -> F1 5A -> VIN_FUSED -> U2 VIN -> U2 reverse protection -> buck -> +5V_MAIN
                          |                       |
                     SMBJ20CA D1             U2 VRP -> U3 -> +3V3_AUX
                          |                                       (GPS/IMU/modules)
                         GND
+5V_MAIN -> F2 4A -> +5V_SCREEN -> J2 pin 2 and UART cable-side supplies
+5V_MAIN -> D2 SS14 -> JP1 -> Teensy VIN
Teensy 3V3 -> +3V3_MCU -> low-current logic buffers only
```

- U2 includes reverse-input protection (published up to 40 V reverse), current/short
  protection and thermal shutdown. U3 takes **VRP**, after U2's reverse protection,
  not the unprotected input. U2's EN/PG are unused; VRP must not be confused with VIN.
- D1 is **SMBJ20CA, bidirectional**, after F1. Its 20 V standoff and approximately
  32.4 V clamp at rated pulse current fit the nominal 10–20 V design. This does NOT
  establish an automotive transient rating: pulse energy, source impedance, wiring
  inductance and actual clamp voltage matter. The Traco input max is 36 V.
- **Not qualified for ISO 7637 or ISO 16750 load dump, jump-start or cranking.** Use a
  properly protected vehicle DC feed. Add a real surge-stopper/automotive front-end
  after reviewing the vehicle supply requirements, before permanent installation.
- Fit an **external 5 A harness fuse near the battery/source**. F1 only protects
  downstream of this board's input; it cannot protect the cable before the PCB.
- **Fuses do not protect a GPIO against a 5 V/12 V mistake**, and neither a fuse nor
  a current-limited supply guarantees a semiconductor will survive a fault.
- D2 blocks USB-powered Teensy VIN from feeding back into the carrier 5 V bus. It
  does **not** block carrier power from entering the computer through an intact
  Teensy VUSB/VIN bridge. **CUT THAT BRIDGE before simultaneous USB/carrier power.**
- JP1 is an initial bring-up isolation jumper, not a hot-switching control. Do not
  energize external sensors while a connected Teensy is unpowered: GPIO protection
  paths or pull-ups can phantom-power it. Make all wiring changes with all power off.
- The two 3.3 V rails must remain separate. Do not bridge AUX 3V3 to Teensy 3V3.

## Connector pinout — top view, square pad is pin 1

The words RX/TX on the carrier refer to the **Teensy/carrier**, not the screen.
Do not guess orientation from wire colours or a vendor's cable plug.

### J1 — 10–20 V input, Phoenix MKDS 1,5/2-5,08

1. Positive input, 10–20 V.
2. GND / negative return.

### J2 — SCREEN, Phoenix MKDS 1,5/4-5,08

| Pin | Carrier function | Connect to CrowPanel Advance |
|---|---|---|
| 1 | GND | J10 GND |
| 2 | **+5 V OUTPUT**, fused 4 A | J10 **+5V_IN** |
| 3 | RX: receives screen data, 5 V side | J10 **TXD0_H** |
| 4 | TX: transmits to screen, 5 V side | J10 **RXD0_H** |

**Use the larger XH2.54 / J10 power-capable header on the genuine Advance. NEVER
connect pin 2 to the small HY2.0 / J2 `3V3_OUT` header.** A clone's connector labels,
pin order and circuitry must be checked against its schematic and measured with a
meter first; its USB identity cannot establish its power-header pinout. Do not use
a same-looking cable without a pin-to-pin continuity test.

U4 is SN74LVC1T45: A=3.3 V Teensy TX, B=5 V cable TX, DIR high.
U5 reverses direction: B=5 V cable RX, A=3.3 V Teensy RX, DIR low.
Both support power-off isolation; their cable-side supply is after the screen fuse.
100 Ω series resistors and PESD5V0S1BA ESD clamps are fitted at the cable paths.
They are **not RS-232 converters** and not a 12 V miswire-protection system.

### J3 — external optocoupler, two-position screw terminal

1. GND, optocoupler output-side emitter/ground.
2. Optocoupler collector/output signal; goes to **Teensy pin 9 through U6**.

Default: **open collector**, with R6 4.7 kΩ pull-up to MCU 3.3 V. R5=1 kΩ and
C11=1 nF form a small input filter; U6 gives a 3.3 V Schmitt output. No extra onboard
opto is included. For a verified push-pull 3.3/5 V source, review/remove R6 so the
external source cannot feed the MCU supply through the pull-up, especially when
the carrier is off. Do not connect an independently powered tach module without
checking its output topology. Never attach a coil or 12 V tach signal directly.

### J4 — GPS, JST B4B-XH-A (2.50 mm)

1. +3V3_AUX **output**.
2. GND.
3. Carrier TX (Teensy 8, via R8) -> GPS RX.
4. Carrier RX (U6 input, Teensy 7) <- GPS TX.

This is a **custom harness**, not a GPS vendor standard. Choose a breakout whose
specified supply pin accepts 3.3 V. A board that requires 5 V into an onboard
regulator is not powered correctly by blindly connecting J4 pin 1 to its VIN.
Do not connect USB to the GPS while it is powered/connected here unless its vendor
explicitly permits those supply combinations and its USB-UART interface is isolated.
GPS firmware configuration and antenna requirements remain module-specific.

### J5 — IMU, JST-XH

1. +3V3_AUX; 2. GND; 3. SDA (Teensy 18); 4. SCL (Teensy 19).
MPU-6050 AD0 -> GND. R12/R13 are 4.7 kΩ I²C pull-ups; account for parallel pull-ups
already on the breakout. Keep I²C wires short. Do not allow pull-ups to 5 V.

### J6 — CAN transceiver module LOGIC, JST-XH

1. +3V3_AUX; 2. GND; 3. Teensy 22 TX -> module CTX/D; 4. Teensy 23 RX <- module CRX/R.
Use a 3.3 V **SN65HVD230** module. CANH/CANL are on that external module, not J6.
CAN termination: only at the two physical bus ends; include existing module resistors
when checking resistance. A powered-off correctly terminated bus is typically ~60 Ω
across CANH/CANL. No transceiver or terminator is fitted on this carrier.

### J7 — optional W5500, 2×4, 2.54 mm

| Pin | Function | Pin | Function |
|---|---|---|---|
| 1 | +3V3_AUX | 2 | GND |
| 3 | CS, Teensy 10 | 4 | MOSI, Teensy 11 |
| 5 | MISO, Teensy 12 | 6 | SCK, Teensy 13 |
| 7 | RESET, Teensy 6 | 8 | INT, Teensy 5 |

Custom wiring only; most W5500 modules have a different pin order. Must be powered
from 3.3 V at the specified input and use 3.3 V I/O. R10 pulls CS high and R11 pulls
RESET high. INT/RESET are wired for future use; current firmware does not promise
those optional GPIO functions are implemented. Keep SPI short and bench-test at
the firmware's clock rate. All external module current counts against the AUX budget.

## Cable reality

A four-terminal connector does not make trunk-length TTL UART noise-immune.
**Rev A has not been verified across the car at 921600 baud.** Test under engine,
alternator and radio activity with error/retry counts and a scope. If it is not
reliable, use differential transceivers at **both ends**; that needs a screen-side
adapter and potentially protocol/wiring changes, not just a different cable.

For power, calculate the **round-trip** drop. Example at 3 m one-way / 4 A:
- 18 AWG copper: approximately 0.50 V drop before connectors/fuses/temperature.
- 16 AWG: approximately 0.32 V.
- 14 AWG: approximately 0.20 V.

Select wire by measured screen current and allowable drop; don't assume a thin
four-core serial cable can carry 4–5 A. The screen's input diode adds more loss.
Use strain relief and ferrules compatible with the screw terminals, not loose
solder-tinned stranded wire under screws. A harness can have heavy power conductors
plus separately shielded/twisted signal/ground pairs while terminating at four
functions. Do not tie a shield to positive power. For now, begin with short bench
wires and measure voltage **at the screen** at maximum load.

## Verification performed and limits

`export.sh` requires:
1. KiCad PCB DRC: **zero violations, zero unconnected items**.
2. Schematic ERC: no violations. **Symbols are pin-labelled passive connectivity
   blocks**, so this ERC does not verify driver conflicts, supply sequencing or
   semiconductor voltage/current limits. Do not treat it as full electrical validation.
3. Independent assertions for key pin maps, power separation, polarity and fixed
   translator direction. Exported schematic netlist must match all 161 connected
   PCB pads from `components.json`.
4. Wide manually routed power trunks survive routing. No high-current trunk uses
   a single signal via. Ground uses wide back-layer trunks and pours on both sides.

Not done: independent engineer review, physical footprint fit, board fabrication,
measured regulator startup/ripple/fault response, thermal load testing, sustained
5 A testing, EMI, automotive transients, enclosure fit or long-cable UART tests.
These are release blockers for permanent vehicle use. Current capability cannot be
proven by Gerbers or DRC alone. Replacement modules must match the exact footprints.

## Rebuilding / editing

Tools used: KiCad 9.0.8, Python `pcbnew` from the system KiCad package, Freerouting
1.9.0 / Java 17 (under Xvfb), and CairoSVG for previews. Libraries are project-local.
Firmware builds, binaries and frozen OTA bridge are untouched.

**`generate.py` overwrites the routed PCB.** To intentionally regenerate:

```sh
/usr/bin/python3 generate.py
/usr/bin/python3 schematic.py
# Run in a scratch directory so Freerouting logs don't enter the repo:
xvfb-run -a java -jar /path/to/freerouting-1.9.0.jar \
  -de /absolute/path/design/racecar-carrier-reva.dsn \
  -do /absolute/path/design/racecar-carrier-reva.ses -mp 12 -mt 1 -da
/usr/bin/python3 finish_board.py
./export.sh
```

Delete the prior `.ses` before a new routing run so failure cannot reuse stale routes.
Source and routed `.dsn/.ses` are retained. Alternatively edit the final KiCad files
in the GUI, then update the generated source/manifest or stop using generation; never
silently overwrite manual edits. `export.sh` exports the existing final board only.

## Reference sources

- PJRC Teensy 4.1 pinout: https://www.pjrc.com/teensy/card11a_rev4_web.pdf
- Pololu module, current curves and connection diagrams: https://www.pololu.com/product/4091
- Traco TSR 1 datasheet: https://www.tracopower.com/products/tsr1.pdf
- TI translator: https://www.ti.com/lit/ds/symlink/sn74lvc1t45.pdf
- TI Schmitt buffer: https://www.ti.com/lit/ds/symlink/sn74lvc2g17.pdf
- `references/` contains the retrieved PJRC card and Pololu diagrams. Pololu's
  dimension picture is a **BOTTOM** view; the labelled power-pin picture is **TOP**.
- Stock footprint geometry: KiCad 9 footprint library, copied into `design/Racecar.pretty`.

Manufacturer documents govern. Some supplier sites could not be fetched from this
host; part selection and the current stock library are not a substitute for reviewing
the latest purchased-part datasheets before placement of an assembly order.
