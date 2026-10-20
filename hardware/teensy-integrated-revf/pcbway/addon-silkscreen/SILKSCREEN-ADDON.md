# Rev F — SILKSCREEN ADD-ON (replacement `F.Silkscreen` layer only)

**What this is:** one replacement gerber file, `racecar-integrated-revf-F_Silkscreen.gbr`,
for the package that is already uploaded to PCBWay (`Racecar-RevF-PCBWAY-GERBERS.zip`,
2026-10-11). Drop it in place of the file of the same name; **nothing else changes** —
copper, solder mask, paste, outline and both drill files are geometry-identical
(re-proved by `verify_addon.py`, which now **exits 1** on any other difference).

**Why:** the uploaded silkscreen would have arrived with four problems:

| # | In the uploaded package | Fixed in this layer | Failure it prevents |
|---|---|---|---|
| 1 | **12 field-terminal labels missing.** A finishing step deleted silk texts by *string*, so every copy of `1 +5V`, `2 RET`, `NTC`, `1 NTC`, `2 +5V` went — including the ones that fitted. **J5 (coolant) had no name and no pin labels at all**; J3/J4/J11/J12 showed only their `3 SIG` pin; J2 had no `2 +5V`. | Every terminal has its **name centred just above the connector** and **one pin label per pad**, left-aligned in a single column beside the pad rows. Names now carry the designator the documents use: `J3 TACH`, `J4 OIL`, `J5 COOLANT`, `J6 AEM AFR`, `J11 THROTTLE`, `J12 BRAKE`, `J1 10-20V IN`, `J2 SCREEN`. | Field wiring into an unmarked 2/3-pin terminal (+5 V sensor supply on J4/J11/J12, NTC on J5). No reference designators are printed anywhere on this board, so before this the legend below referred to a "J2" nothing identified. |
| 2 | **Five parts unlabelled or labelled 28–44 mm away.** `GPS SMA`, `PI 5 VIDEO UART 3V3` and `RTC 3V NO CHARGE` were hard-coded coordinates from an older placement and drifted from J8, J13 and BT1; J9 (RTC lead), J10 (network programming header) and JP1 (Teensy power jumper) had nothing. `NO COIL / INJECTOR` (a tach warning) sat by the Teensy, not by J3. | Every connector, jumper and the cell is **named at the part**: `J8 GPS ANT`, `J9 RTC LEAD`, `J10 NET PROG 3V3`, `J13 PI 5 VIDEO 3V3`, `J14 CAN`, `JP1 TEENSY PWR`, `JP2 CAN TERM: BUS END ONLY`, `BT1 CR2032 RTC - NO CHARGER`. J13 gets one label under each pad (`1 GND`, `2 TX>PI 10`, `3 RX<PI 8`, `4 GND` — rotated, 2.5 mm pitch); J9 `1 VBAT` / `2 GND`; J10 odd pins left, even right; J14 `CANH` / `CANL` / `GND` over its terminals (its wires enter from below). `NO COIL / INJECTOR` now sits directly above `J3 TACH`. | Wiring the Pi lead, the RTC lead or a programming adapter to the wrong pin; looking for a GPS or Pi connector at a label with nothing under it. |
| 3 | No Teensy orientation marking. | `TEENSY` / `USB END` / `THIS EDGE`, a chevron toward the USB end, socket pin numbers `1` / `25`. | The 2×24 socket is symmetric, so a **180°-rotated or one-pin-off Teensy also fits**. Backwards, the board feeds **+5 V (via D2/JP1) onto Teensy pin 32**, shorts **TX2 to GND** and drives **3.3 V into A3**. |
| 4 | No warning that the screen cable is crossed. | A table under J2, digits in fixed columns so each J2 pin sits directly over its partner: `J2 -> SCREEN J10: CROSSED` / `J2 1 2 3 4` / `J10 4 3 2 1`. | J2 = 1 GND / 2 +5V / 3 RX / 4 TX, the CrowPanel Advance's J10 = 1 RXD0_H / 2 TXD0_H / 3 +5V_IN / 4 GND. A straight 1-to-1 cable puts **+5 V onto the panel's TXD0_H**. |

All board text is now **at least 0.90 mm high with a 0.16 mm stroke** (it was 0.80 mm /
0.13 mm; fab legend minimums are ~0.81 mm / ~0.153 mm). Teensy marks are 0.95–1.3 mm.

Nothing electrical was touched: no net, track, via, pad, footprint or BOM/CPL row. The
schematic and netlist are unchanged, so the assembly data already uploaded stays valid.

## Files

```
racecar-integrated-revf-F_Silkscreen.gbr    <- replaces the same-named file in the package
Racecar-RevF-SILKSCREEN-ADDON-preview.png   <- the whole layer + three detail tiles
addon_silk.py                               <- applies the legends (wrapper, idempotent)
verify_addon.py                             <- layer-by-layer proof vs the uploaded ZIP (exit 1 on failure)
render_gerber.py                            <- independent readback: rasterises the GERBER itself
make_preview.py                             <- rebuilds the preview PNG from the board
```

The layout itself lives in **`../../electrical/silk_legends.py`** — one table used by both
`generate.py` (fresh board) and `addon_silk.py` (routed board), so they cannot drift apart.
It places each label against the real board: a pin label sits exactly on its pad row unless
that would touch a solder-mask opening (0.15 mm) or other silk (0.35 mm), in which case it
slides the shortest distance along its column, never more than 1.8 mm (pad pitch is
5.08 mm, so it always stays far nearer its own pin). A label that cannot be placed
**raises** — deleting labels to get a clean DRC is exactly what caused problem 1.

Five pin labels have a part on their own row and therefore sit slightly off it:

| Label | Slide | Part on the row |
|---|---|---|
| J4 `1 +5V` | 1.65 mm up | R11 |
| J4 `2 RET` | 0.65 mm down | R12 |
| J5 `1 NTC` | 0.35 mm up | R16 |
| J12 `2 RET` | 1.40 mm down | F1 |
| J2 `2 +5V` | 1.80 mm down | D19 |

The other 19 pin labels and all 8 names are exactly on position.

## Proof

| Check | Result |
|---|---|
| `verify_addon.py` vs the uploaded ZIP | **PASS** — 12 layers + PTH/NPTH drills geometry-identical; only `F_Silkscreen` differs |
| KiCad DRC, `--severity-all --all-track-errors` | **0 violations, 0 unconnected** (incl. `silk_over_copper`, `silk_overlap`, `text_height`, `silk_edge_clearance`) |
| KiCad ERC, all severities | **0** |
| `electrical/verify.py` | **1,078 assertions pass** — now includes every J/JP/BT part named within 6 mm of itself, each J13/J9/J10/J14 pin label in line with its pad, J13's `TX>PI 10` / `RX<PI 8` checked against Teensy pins 1/0 through R1/R2, every terminal's name position, one left-aligned label per pad nearer its own pin than any other, no stray text in the column, the harness table checked **against the nets on J2's pads**, the Teensy `1`/`25` beside the GND/TEENSY_VIN pads at the USB end, and the 0.153 mm stroke minimum |
| Negative tests of those checks | a dropped label, a title 1.5 mm off, a straight-through harness table, a 0.13 mm stroke, a misaligned label, J8's name moved away and J13's name deleted are each caught |
| `render_gerber.py` on the shipped `.gbr` | every name, pin label, the Teensy marks and the harness table are present in the file itself |
| Idempotency | running `addon_silk.py` twice gives an identical layer |

(Raw SHA-256 of the unchanged layers differs from the ZIP only because every gerber
carries a `%TF.CreationDate` / `G04 … date` comment; `verify_addon.py` compares the
coordinates, apertures and flashes.)

## How to apply it

Either:

1. **Replace the file**: open `Racecar-RevF-PCBWAY-GERBERS.zip`, overwrite
   `racecar-integrated-revf-F_Silkscreen.gbr` with this one (keep the name), re-zip; or
2. **Send it alongside**: attach this file and ask the CAM engineer to substitute that
   layer for the one in the package they already have (`EMAIL-TO-PCBWAY.txt`).

`racecar-integrated-revf-job.gbrjob` carries a per-file checksum, so its entry for
`F_Silkscreen` is stale after the swap. The layer files are authoritative; the CAM tool can
regenerate the job file or drop it (it contains no geometry).

## Rebuilding it

`design/racecar-integrated-revf.kicad_pcb` already contains the final silk (so
`fabrication/export.py` and `pcbway/make_package.py` reproduce it). From the revision dir:

```bash
python3 pcbway/addon-silkscreen/addon_silk.py design/racecar-integrated-revf.kicad_pcb   # idempotent
python3 electrical/verify.py
kicad-cli pcb drc --format json --severity-all --all-track-errors --exit-code-violations \
  -o /tmp/drc.json design/racecar-integrated-revf.kicad_pcb
mkdir -p /tmp/out
kicad-cli pcb export gerbers --no-protel-ext \
  --layers F.Cu,In1.Cu,In2.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,F.Paste,B.Paste,Edge.Cuts \
  -o /tmp/out/ design/racecar-integrated-revf.kicad_pcb
kicad-cli pcb export drill --format excellon --drill-origin absolute --excellon-separate-th \
  -o /tmp/out/ design/racecar-integrated-revf.kicad_pcb
python3 pcbway/addon-silkscreen/verify_addon.py pcbway/Racecar-RevF-PCBWAY-GERBERS.zip /tmp/out
cp /tmp/out/racecar-integrated-revf-F_Silkscreen.gbr pcbway/addon-silkscreen/
python3 pcbway/addon-silkscreen/render_gerber.py pcbway/addon-silkscreen/racecar-integrated-revf-F_Silkscreen.gbr \
  /tmp/silk.png 20 50 24 172 --px-per-mm 20       # look at the FILE, not the board
python3 pcbway/addon-silkscreen/make_preview.py
```
