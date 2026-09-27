# Rev C layout and required Gerber release

**Active revision: C.** A/B are retired. The current C artwork remains a placement
concept, not an electrical PCB. **The user requires Gerber + drill files as the
final deliverable. A preview/review ZIP is NOT a substitute.**

## Connector bank — one edge, labelled

All six screw-terminal blocks are along the **LEFT edge in the top view**, with
wire entries facing LEFT and screwdriver access from above. Function names sit
above each block; proposed numbered pin labels sit alongside, not underneath it.
Reserve wire bend radius, tool access and enclosure openings in the mechanical design.

**Proposed order, top-to-bottom (NOT released wiring):**

| Block | Proposed pad 1 → last pad, top-to-bottom |
|---|---|
| TACH | 1 SIGNAL / 2 RETURN — conditioned ECU/cluster only, never coil/injector |
| OIL | 1 +5V OUT / 2 RETURN / 3 SIGNAL |
| COOLANT | 1 NTC / 2 RETURN |
| AEM AFR | 1 SIG+ (solid WHITE) / 2 RETURN (BROWN), gauge output only |
| POWER IN | 1 +10–20V IN / 2 GND |
| SCREEN | 1 GND / 2 +5V OUT / 3 RX FROM SCREEN / 4 TX TO SCREEN |

Final schematic nets, connector pad numbering and purchased part drawings must
agree before this becomes a wiring diagram. The earlier Rev A tach connector
used a different order and only accepted an external opto output; do NOT copy its
netlist or harness wiring into this proposal. Screen connection is verified
Advance J10 ONLY, never HY2.0/J2 3V3_OUT; verify the particular clone's pinout.

GPS SMA remains external. Service headers, optional CAN logic header and Teensy
USB/SD are NOT screw terminals; they still need enclosure/service access.

## Internal WiFi antenna

Use **ESP32-S3-WROOM-1-N8R2**, not the 1U module. Built-in PCB antenna; no WiFi
antenna jack, pigtail or external antenna. The antenna points down in the top
view and overhangs a proposed **48 x 6 mm carrier notch**. Its stock RF rule area
is retained. This changes the carrier outline but not its nominal **130 x 140 mm**
envelope. The notch/clearance remains subject to module-manufacturer RF/mechanical
review, not a proven layout recommendation for every enclosure.

Keep ALL host copper/layers, components, metal fixtures, battery and wire bundles
out of the antenna clearance. Use a plastic enclosure/RF-transparent window and
verify installed performance; short distance does not defeat metal shielding.
GPS RF, dual-WiFi upload ownership, screen-led OTA and RTC backup remain as specified
in `WIFI_RTC_ARCHITECTURE.md`.

## What still blocks actual Gerbers

1. **Electrical design:** no Rev C schematic/netlist exists. Complete GPS power/RF/
   antenna bias, selected IMU support, sensor protection/calibration, opto front
   end, AEM power-off isolation, new WiFi supply/SPI/reset and VBAT contact circuitry.
2. **Exact components/footprints:** representative packages and candidate BOM are
   not final. Verify pad numbering, genuine ordering codes, current/voltage/thermal
   budgets, source loading, power sequencing, USB backfeed and connector ratings.
3. **Actual layout/routing:** place final parts, define stackup/impedance/copper
   weights, route all nets, size power paths, pour planes and enforce RF keepouts.
4. **Checks/review:** electrical review plus ERC; compare every connected PCB pad
   with the exported schematic; DRC including unconnected items; footprint and
   silkscreen accessibility inspection. Passive-block ERC alone is insufficient.
5. **Export and inspect:** generate all used copper layers, both solder masks,
   silkscreens, appropriate paste, board outline and separate plated/non-plated
   Excellon drills. Inspect them in a Gerber viewer, verify outline/drill alignment
   and include fabrication specifications, approved BOM and assembly data.

`fabrication/export.py` is the fail-closed export pipeline for that future board.
It accepts only `design/racecar-integrated-revc.kicad_pcb` and its matching schematic,
not `preview/PLACEMENT-ONLY-NOT-FOR-FAB.kicad_pcb`. It requires a hash-pinned design
review, genuine nets/tracks, clean ERC/DRC and schematic/PCB net agreement. Its
current expected result is **BLOCKED: missing electrical design**, not a Gerber ZIP.
The exporter itself has not been validated against a completed Rev C board.

**No routing, electrical approval or Gerbers have been created by the preview
regeneration.** Do not rename the netless preview PCB or the archived Rev A Gerbers
to bypass these missing engineering steps. Even a future clean CAD export will
still be an unvalidated prototype requiring current-limited bring-up and physical
RF/thermal/load/vehicle testing before use.
