# Rev C proposed assembled layout — NOT FOR MANUFACTURE

Open **`../REV-C-INTEGRATED-PREVIEW.png`** for the updated drawing. A copy is written
as `/home/chris/Downloads/Racecar-RevC-Integrated-Preview.png`.

The large angled image and top-view inset show the requested architecture:
1. Soldered NEO-M9N receiver with SMA antenna connector and RF-support area;
2. bare onboard six-axis IMU with local supporting components;
3. three-wire oil sender terminal and interface area;
4. two-wire coolant sender terminal and interface area;
5. ECU/cluster-only tach terminal, high-impedance front-end area and **onboard opto**;
6. AEM 30-0300 gauge-output-only terminal and input-conditioning area;
7. **Lower strip:** ESP32-S3-WROOM-1-N8R2 with INTERNAL PCB antenna (no WiFi jack),
   proposed local 3.3 V supply/service header, and **CR2032 RTC cell/holder**.
   The module transport/firmware and battery connection are not implemented by a render.
8. **Rev C terminal bank:** all six screw terminals at the LEFT edge, wire entries
   facing left: TACH / OIL / COOLANT / AEM AFR / POWER IN / SCREEN, top-to-bottom.
   Function and proposed pin labels are visible beside each block. Final wiring
   must be checked against the completed electrical netlist; this has no nets.

Teensy stays socketed; the existing main and auxiliary regulator modules and the
four-wire powered display UART are retained in the proposed placement. Networking
remains WiFi-only, now with **both trunk-module and screen endpoints** required.
The W5500 remains removed. See `../WIFI_RTC_ARCHITECTURE.md` for the SPI allocation,
unchanged OTA path, finite-life RTC backup and firmware/electrical work still needed.
An optional CAN module header remains. The AEM two-pin gauge-output terminal
and input conditioning moved to the left bank (WHITE signal / BROWN return).
No direct O2-sensor connector, heater/controller or selection jumper is included.
Protection/isolation parts and electrical layout still require review; see
`../AFR_INTERFACE.md`.

## Exact status

This is an **illustrated placement proposal**, enlarged from the former 130 x 110 mm
envelope to **130 x 140 mm** for WiFi + RTC cell/service clearance. It is NOT a revised, electrically completed PCB. Component
positions, counts, footprints and support ICs are representative; final selection,
values, routing, sensor calibration, power/RF design and validation are pending.
Do not use the picture as a connector pinout or assembly bill of materials.

- `PLACEMENT-ONLY-NOT-FOR-FAB.kicad_pcb` is a generated RENDERING SOURCE, not a PCB to
  order. It intentionally contains **no electrical nets, tracks or copper zones**;
  the WiFi footprint's RF KEEP-OUT rule area is retained (not a copper pour).
  The antenna overhangs a carrier notch. This does not validate an RF design.
- The legacy stock `ublox_NEO` footprint illustrates the GPS envelope only; it does
  NOT establish NEO-M9N land-pattern compatibility. The IMU QFN and optocoupler DIP
  similarly illustrate package types, not finalized parts.
- The NEO/SMA, WiFi-module and coin-cell geometry is simplified, as are the
  inherited Teensy/Pololu models. The battery holder is a stock illustration;
  contact/retention fit and the extra Teensy VBAT socket still need verification.
- `visual-inventory.json` is a visual placement inventory, **not a component BOM**.
- `../CONNECTIONS-CONCEPT.svg` is a functional interface drawing, not a schematic.
- No Rev C manufacturing ZIP, Gerbers, netlist, wiring pinout, or passed electrical
  checks are implied. Rev A sources, routing and fabrication outputs are untouched.

## Reproduce

With KiCad 9 Python bindings and Pillow:

```bash
/usr/bin/python3 hardware/teensy-integrated-revc/preview/draw.py
# Recompose captions/functional drawing without re-rendering:
/usr/bin/python3 hardware/teensy-integrated-revc/preview/draw.py --compose-only
```

The script overwrites ONLY these Rev C generated preview files and its Downloads
PNG copy. It hashes all Rev A `design/` and `gerbers/` files before/after and asserts
that they have not changed. It also verifies the rendering board contains no tracks
or nets and no leftover external GPS/IMU or Ethernet headers. The standard SMA footprint's
edge-placement hint is moved to F.Fab on the render copy so it cannot invalidate
the board outline. This is not a production modification to that footprint.

## Models / licensing

Existing stock models and illustrative module models are reused from
`../../_archive/teensy-carrier-reva/preview/models/` without modifying them. New stock models
in `models/` were fetched from:
`https://raw.githubusercontent.com/KiCad/kicad-packages3D/master/<path>`
with their embedded attribution/license headers preserved. See
`models/LICENSE-KICAD.md` and each individual model's header. Locally generated
`*-illustration.wrl` files (NEO, SMA, ESP32 module and coin cell) are original
approximate geometry produced by `draw.py`, not manufacturer mechanical data.
